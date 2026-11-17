#!/usr/bin/env python3
"""Show Silkie extensions together; q exits, Tab selects details, h plays haptics.

Run ``python bin/silkie_showcase.py`` after installing this checkout. No Pillow
or native graphics library is required. The optional --font and --gpu-surface
arguments exercise application-owned font and GPU resources as well.
"""
from __future__ import annotations

# std imports
import argparse
import contextlib
import json
import math
import re
import signal
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# local
from hamingjur import Terminal, graphics, kitty, silkie
from hamingjur.protocols import parse_event


# These are protocol field ranges, not limits on retained input or drawing time.
MAX_ID = 0xffffffff
TABLET_BITS = (
    ('pen', 'Pen / pressure / tilt', 1, silkie.TabletMode.TOOLS),
    ('touch', 'Touch / multitouch', 6, silkie.TabletMode.TOUCH),
    ('pad', 'Tablet pads', 16, silkie.TabletMode.PAD),
    ('actions', 'Tool actions', 32, silkie.TabletMode.TOOL_ACTIONS),
    ('hardware', 'Hardware IDs', 64, silkie.TabletMode.HARDWARE_IDS),
    ('estimates', 'Predicted samples', 128, silkie.TabletMode.ESTIMATES),
    ('accidental', 'Accidental touches', 256, silkie.TabletMode.ACCIDENTAL_TOUCH),
)
FEATURES = (
    ('caps', 'Discovery'),
    *((key, name) for key, name, _, _ in TABLET_BITS),
    ('scroll', 'Smooth scrolling'), ('gesture', 'Gestures'),
    ('haptic', 'Haptic feedback'), ('ink', 'Terminal-local ink'),
    ('font', 'Font roles / styles'), ('display', 'Display information'),
    ('latency', 'Low latency'), ('frame', 'Frame presentation'),
    ('depth', 'Stereo depth / phase'), ('hdr', 'HDR text'),
    ('gamut', 'Wide gamut'), ('alpha', 'Cell transparency'),
    *((f'f{300 + fmt}', f'Grayscale {fmt}-bit') for fmt in (1, 2, 4, 8)),
    ('f516', 'RGB565 images'), ('f64', 'HDR float images'),
    ('vectors', 'Vector scenes'), ('text', 'Shaped vector text'),
    ('fonts', 'Font resources'), ('pointer', 'Image pointer'),
    ('gpu', 'GPU surface'), ('ssh-agent', 'SSH-agent transport'),
    ('notifications', 'Notification routes'),
)


@dataclass
class Feature:
    """Keep a status and latest observation; event counts cover the full session."""

    name: str
    status: str = 'not supported'
    detail: str = 'No capability reply.'
    count: int = 0


def describe(fields):
    """Render untrusted reply text without allowing terminal control injection."""
    return ' '.join(f'{key}={value!r}' for key, value in fields.items()
                    if key not in ('v', 'op', 'id', 'data'))


def pixels(fmt, width, height):
    """Generate a resolution-sized gradient, with protocol-defined row padding."""
    data = bytearray()
    for y in range(height):
        packed = 0
        bits = 0
        for x in range(width):
            level = x / max(1, width - 1)
            if fmt in {301, 302, 304, 308}:
                depth = fmt - 300
                packed = (packed << depth) | round(level * ((1 << depth) - 1))
                bits += depth
                if bits == 8:
                    data.append(packed)
                    packed = bits = 0
            elif fmt == 516:
                data.extend(struct.pack('<H', (round(31 * level) << 11)
                                        | (round(63 * (1 - level)) << 5) | 15))
            elif fmt == 64:
                data.extend(struct.pack('<eeee', 4 * level, y / max(1, height - 1), 0.2, 1))
            else:
                data.extend((round(255 * level), round(255 * (1 - level)), 160, 255))
        if bits:
            data.append(packed << (8 - bits))
    return data


class Showcase:
    """Own the dashboard, negotiated modes, images and live input observations."""
    # Capabilities, UI state and independently owned terminal resources live for this session.
    # pylint: disable=too-many-instance-attributes

    def __init__(self, term, args, event_log=None):
        self.term, self.args, self.event_log = term, args, event_log
        self.features = {key: Feature(name) for key, name in FEATURES}
        self.reports = {}
        self.granted = {}
        self.formats = set()
        self.images = set()
        self.selected = 0
        self.offset = 0
        self.geometry = None
        self.art = None
        self.cell = None
        self.started = time.monotonic()
        self.tick = 0
        self.ink_brush = None
        self.ink_owned = False
        self.clear_ink = False
        self.sample_lines = []
        self.font_id = 'showcase-' + uuid.uuid4().hex
        self.gpu_deadline = None
        self.text_layout = {'text': 'ffi  A\u0301  العربية', 'x': 8, 'y': 36,
                            'size': 24, 'fill': '#ffffff'}

    def mark(self, key, supported, detail='', *, status=None):
        """Distinguish unavailable features, accepted requests and unverified tests."""
        feature = self.features[key]
        feature.status = status or ('OK' if supported else 'not supported')
        feature.detail = detail or (
            'Capability confirmed.' if supported else 'No capability reply.')

    def query_channels(self):
        """Use aggregate discovery when available; fall back to individual queries."""
        caps = self.term.silkie.capabilities(timeout=self.args.timeout)
        self.mark('caps', caps is not None, describe(caps.event.fields) if caps else '')
        selectors = ('tablet-pointer', 'scroll', 'gesture', 'haptic', 'ink', 'font',
                     'display', 'latency', 'frame')
        for selector in selectors:
            if caps is not None and not caps.supports(selector):
                continue
            report = self.term.silkie.channel(selector).query(timeout=self.args.timeout)
            if report is not None:
                self.reports[selector] = report
        # Agent IDs have a different alphabet and use the specialized public channel.
        if caps is None or caps.supports('ssh-agent'):
            report = self.term.ssh_agent.query(timeout=self.args.timeout)
            if report:
                self.reports['ssh-agent'] = report
        self.mark('depth', bool(caps and caps.supports('depth')),
                  'Stereo attributes; physical depth needs spatial rendering enabled in Silkie.')
        for key in ('scroll', 'gesture', 'haptic', 'ink', 'font', 'latency', 'frame'):
            report = self.reports.get(key)
            self.mark(key, bool(report and report.integer('caps', 0)),
                      describe(report.fields) if report else '')
        self.update_display(self.reports.get('display'))
        return caps

    def update_display(self, report):
        """Refresh hardware-dependent annotations when the window changes displays."""
        self.mark('display', report is not None, describe(report.fields) if report else '')
        if report is None:
            return
        self.reports['display'] = report
        headroom = report.number('headroom')
        self.features['hdr'].detail = (
            f'Display headroom={headroom}; <=1 renders as SDR. SGR support probed separately.')
        self.features['gamut'].detail = (
            f"Display gamut={report.fields.get('gamut', 'unknown')}. "
            'SGR support probed separately.')

    def enable_modes(self, stack):
        """Read back every requested mode and restore previous modes on all exits."""
        tablet = self.reports.get('tablet-pointer')
        requested = 0
        for key, _, capability, mode in TABLET_BITS:
            supported = bool(tablet and tablet.integer('caps', 0) & capability)
            self.mark(key, supported, describe(tablet.fields) if tablet else '')
            if supported:
                requested |= int(mode)
        wanted = {'tablet-pointer': requested, 'scroll': 1, 'gesture': 3,
                  'display': 1, 'latency': 1, 'frame': 1}
        for selector, mode in wanted.items():
            report = self.reports.get(selector)
            if not report or not mode or (selector != 'display' and not report.integer('caps', 0)):
                continue
            try:
                self.granted[selector] = stack.enter_context(
                    self.term.silkie.channel(selector).enabled(mode, timeout=self.args.timeout))
            except RuntimeError as error:
                self.granted[selector] = 0
                if selector in self.features:
                    self.mark(selector, False, str(error))
            if selector in self.features and not self.granted[selector]:
                self.mark(selector, False, 'Reporting mode refused or read-back timed out.')
        for key, _, _, mode in TABLET_BITS:
            if (self.features[key].status == 'OK'
                    and not self.granted.get('tablet-pointer', 0) & mode):
                self.mark(key, False, 'Requested tablet reporting bit was refused.')

    def probe_sgr(self, key, sequence, parameter):
        """Verify retained SGR attributes rather than guessing from the terminal name."""
        self.term.send(sequence)
        try:
            state = self.term.get_decrqss(timeout=self.args.timeout)
        finally:
            self.term.send(self.term.normal)
        supported = state is not None and parameter in state.split(';')
        self.mark(key, supported, f'SGR read-back: {state!r}')

    def discover(self, stack):
        """Probe capabilities and configure independently testable demonstrations."""
        caps = self.query_channels()
        if self.features['haptic'].status == 'OK':
            stack.callback(self.term.haptic.send, 'stop')
        self.enable_modes(stack)
        self._probe_graphics(caps)
        self.probe_sgr('hdr', self.term.foreground_brightness(200), '56:200')
        self.probe_sgr('alpha', self.term.background_alpha(40), '70:40')
        self.probe_sgr('gamut', self.term.color_gamut(255, 0, 0, gamut=1), '38:2:1:255:0:0')
        if 200 in self.formats:
            self._probe_fonts(stack)
        self._probe_pointer(stack)
        self._probe_notifications()
        self.mark('gpu', bool(caps and 'g' in caps.media),
                  'Requires an existing local IOSurface/dma-buf descriptor.',
                  status='needs surface' if caps and 'g' in caps.media else None)
        self.probe_agent()
        self._prepare_ink(stack)
        if 'display' in self.reports:
            self.update_display(self.reports['display'])

    def _probe_graphics(self, caps):
        """Confirm each format using a real one-pixel query."""
        for fmt in (301, 302, 304, 308, 516, 32, 64, 200):
            if caps is not None and fmt not in caps.formats:
                continue
            if self.term.graphics.supports(fmt, timeout=self.args.timeout):
                self.formats.add(fmt)
        for fmt in (301, 302, 304, 308, 516, 64):
            self.mark(f'f{fmt}', fmt in self.formats,
                      'Live image query accepted. Format 64 is linear half-float HDR RGBA.'
                      if fmt == 64 else 'Live image query accepted; see the gradient strip.')
        self.mark('vectors', 200 in self.formats,
                  'Animated paths, gradients, nested groups, clips, symbols and pressure strokes.')

    def _probe_fonts(self, stack):
        """Exercise measurement and font resources independently."""
        try:
            measured = self.term.graphics.measure_text(self.text_layout, timeout=self.args.timeout)
            self.mark('text', measured is not None, repr(measured))
        except ValueError as error:
            self.mark('text', False, str(error), status='error')
        try:
            fonts = self.term.graphics.service('fonts', timeout=self.args.timeout)
            self.mark('fonts', fonts is not None, 'Font enumeration queried.',
                      status='needs --font' if fonts is not None and not self.args.font else None)
            if fonts is not None and self.args.font:
                data = self.args.font.read_bytes()
                stack.callback(self.term.graphics.service, 'release', id=self.font_id,
                               timeout=self.args.timeout)
                font = self.term.graphics.upload_font(self.font_id, data, timeout=self.args.timeout)
                if font is None:
                    self.mark('fonts', False, 'Font upload timed out.')
                else:
                    self.text_layout['font'] = '@' + self.font_id
                    self.mark('fonts', True, 'Uploaded font used by vector text; released on exit.')
        except (ValueError, OSError) as error:
            self.mark('fonts', False, str(error), status='error')

    def _probe_pointer(self, stack):
        """Use the OSC 22 support query before pushing an image shape."""
        reply = self.term.query(kitty.pointer('image:1:0:0', action='?'),
                                r'\x1b\]22;([01])(?:\x07|\x1b\\)', timeout=self.args.timeout)
        event = parse_event(reply) if reply is not None else None
        self.mark('pointer', event is not None and event.payload == '1' and 32 in self.formats,
                  'Move the pointer over this window to see the gradient image cursor.')
        if self.features['pointer'].status == 'OK':
            self.images.add(1)
            # Fixed dimensions here are cursor artwork geometry, not an input/storage limit.
            self.term.graphics.send(graphics.image(pixels(32, 24, 24), image_id=1, format=32,
                                                   width=24, height=24, display=False, q=1))
            stack.enter_context(self.term.kitty.pointer('image:1:0:0'))

    def _prepare_ink(self, stack):
        """Save any existing brush before claiming a private vector image."""
        ink = self.reports.get('ink')
        if ink and ink.integer('caps', 0) & 1 and 200 in self.formats and self.granted.get(
                'tablet-pointer', 0) & silkie.TabletMode.TOOLS:
            # A previous brush is state too: restore it, including its remaining ID range.
            self.ink_brush = {key: value for key, value in ink.fields.items()
                              if key not in ('v', 'op', 'id', 'caps', 'mode')}
            stack.callback(self.restore_ink, ink.integer('mode', 0))
            self.term.ink.send('mode', mode=0)
            self.ink_owned = True
        else:
            self.mark('ink', False,
                      'Needs vector graphics and granted pen reporting, as well as ink.')

    def _probe_notifications(self):
        """Round-trip a return route with a capability query, without a desktop popup."""
        identifier = 'showcase-' + uuid.uuid4().hex
        hops = (('showcase', identifier, 0),)
        sequence = kitty.notification_packet(metadata=[
            ('i', identifier), ('p', '?'), ('r', kitty.notification_route(hops))])
        pattern = (r'\x1b\]99;(?=[^\x1b\x07]*\bi=' + re.escape(identifier)
                   + r'(?=[:;]))[^\x1b\x07]*(?:\x07|\x1b\\)')
        reply = self.term.query(sequence, pattern, timeout=self.args.timeout)
        try:
            event = parse_event(reply) if reply is not None else None
            supported = event is not None and event.notification_route == hops
            self.mark('notifications', supported,
                      'Capability reply preserved the return route; no notification was displayed.')
        except ValueError as error:
            self.mark('notifications', False, str(error))

    def restore_ink(self, mode):
        """Stop our brush before deleting images, then restore the previous brush."""
        self.term.ink.send('mode', mode=0)
        if self.ink_brush and all(key in self.ink_brush for key in ('i', 'k', 'w1')):
            self.term.ink.send('brush', **self.ink_brush)
        self.term.ink.send('mode', mode=mode)

    def probe_agent(self):
        """Exchange an identity-list request, without signing or displaying key material."""
        report = self.reports.get('ssh-agent')
        if not report or report.integer('caps', 0) != 1 or not report.fields.get('g'):
            self.mark('ssh-agent', False, 'No permitted SSH-agent provider.')
            return
        connection, generation = uuid.uuid4().hex, report.fields['g']
        try:
            # SSH2_AGENTC_REQUEST_IDENTITIES is message 11, a complete five-byte packet.
            reply = self.term.ssh_agent.exchange(
                b'\0\0\0\1\x0b', connection=connection, generation=generation,
                expires=int((time.time() + self.args.timeout) * 1000), timeout=self.args.timeout)
            ok = (reply is not None and 'error' not in reply.fields and len(reply.data) >= 9
                  and reply.data[4] == 12)
            count = int.from_bytes(reply.data[5:9], 'big') if ok and len(reply.data) >= 9 else None
            self.mark('ssh-agent', ok,
                      f'Identity-list exchange: {count} identities; no signing requests.')
        finally:
            self.term.ssh_agent.close(connection, generation)

    def release_images(self):
        """Release only images owned by this invocation."""
        for image_id in sorted(self.images):
            self.term.graphics.send(graphics.delete(image_id, q=2))
        self.images.clear()

    def layout(self):
        """Fit all status cells when possible; expose scrolling on physically smaller screens."""
        width, height = max(1, self.term.width), max(1, self.term.height)
        columns = max(1, width // 38)
        rows = math.ceil(len(self.features) / columns)
        reserved = 4 + len(self.sample_lines)
        available = max(0, height - reserved)
        visible = min(rows, available)
        self.offset = min(self.offset, max(0, rows - visible))
        art_height = max(0, height - (visible + reserved))
        return width, height, columns, rows, visible, art_height

    def line(self, row, text):
        """Paint within the viewport without writing the bottom-right scrolling cell."""
        if 0 <= row < self.term.height:
            usable = max(0, self.term.width - 1)
            self.term.stream.write(self.term.move_yx(row, 0)
                                   + self.term.truncate(text, usable) + self.term.clear_eol)

    def samples(self):
        """Display independent SGR and font samples side by side on one line."""
        term = self.term
        parts = []
        if self.features['font'].status == 'OK':
            roles = self.reports['font'].fields.get('roles', 'default').split(',')
            role = 'serif' if 'serif' in roles else roles[-1]
            parts.append(term.silkie.select_font(role=role, weight=300, slant=0, italic=True)
                         + 'Serif italic' + term.normal)
            parts.append(term.silkie.select_font(weight=800, slant=-1000)
                         + 'Heavy slant' + term.normal)
        if self.features['hdr'].status == 'OK':
            parts.append(term.foreground_brightness(200)(term.white('HDR 200%')))
            parts.append(term.background_brightness(200)(term.on_white(term.black('HDR bg'))))
        if self.features['gamut'].status == 'OK':
            parts.extend(term.color_gamut(255, 60, 0, gamut=g)(label)
                         for g, label in ((0, 'sRGB'), (1, 'P3'), (2, '2020')))
        if self.features['alpha'].status == 'OK':
            parts.extend(term.background_alpha(a)(term.on_blue(f'{a}%')) for a in (0, 40, 100))
        if self.features['depth'].status == 'OK':
            parts.extend((term.depth(-150)('Near'), term.depth(150)('Far'),
                          term.depth(pixels=-0.25, phase=0.125)('Phase')))
        return ' | '.join(parts) or 'Visual text extensions: not supported'

    def render(self):
        """Redraw statuses without clearing retained graphics or terminal-side ink."""
        # Positions, viewport geometry and text columns are explicit for this screen renderer.
        # pylint: disable=too-many-locals
        self.sample_lines = self.term.wrap(self.samples(), max(1, self.term.width - 1))
        width, height, columns, rows, visible, art_height = self.layout()
        geometry = (width, height, columns, visible, art_height)
        if geometry != self.geometry:
            self.geometry = geometry
            self.term.stream.write(self.term.clear)
            self.art = None
        self.line(0, self.term.bold('Silkie feature showcase')
                  + f'  {time.monotonic() - self.started:.1f}s'
                  + '  q quit | Tab details | h haptic | c clear ink')
        self.line(1, 'Use pen, touch, wheel and gestures anywhere. '
                  'OK = negotiated; inspect details with Tab.')
        items = list(self.features.items())
        cell_width = width // columns
        for row in range(visible):
            cells = []
            for col in range(columns):
                index = col * rows + row + self.offset
                if index >= len(items):
                    continue
                _, feature = items[index]
                status = feature.status + (f' #{feature.count}' if feature.count else '')
                color = self.term.green if feature.status == 'OK' else self.term.yellow
                name_width = max(1, cell_width - len(status) - 4)
                name = self.term.truncate(feature.name, name_width)
                name = self.term.ljust(name, name_width)
                marker = '>' if index == self.selected else ' '
                cells.append(marker + name + ' ' + color(status) + '  ')
            self.line(row + 2, ''.join(cells))
        selected = items[self.selected][1]
        self.line(visible + 2, f'{selected.name}: {selected.detail} (events: {selected.count})')
        for index, sample in enumerate(self.sample_lines):
            self.line(visible + 3 + index, sample)
        footer = ('Resize for more artwork.' if art_height < 2 else
                  'Gradient formats | animated vector/text scene | draw with pen in the ink area')
        if visible < rows:
            footer = (f'j/k scroll ({self.offset + 1}/{rows - visible + 1}); '
                      'enlarge for all features.')
        self.line(height - 1, footer)
        if art_height and self.art is None:
            self.draw_art(visible + 3 + len(self.sample_lines), art_height)
        self.animate()
        self.term.stream.flush()

    def draw_art(self, top, rows):
        """Place all supported image formats and vector/ink canvases simultaneously."""
        self.cell = self.term.get_cell_size(timeout=self.args.timeout, force=True)
        self.art = (top, rows)
        if not self.cell or self.cell.width <= 0 or self.cell.height <= 0:
            self.line(top, 'Pixel geometry unavailable: image samples need a cell-size reply.')
            return
        cw, ch = self.cell.width, self.cell.height
        # Artwork is sized to its screen region; storage grows with actual pixel dimensions.
        columns = max(1, (self.term.width - 1) // (4 if self.args.gpu_surface else 3))
        formats = [fmt for fmt in (301, 302, 304, 308, 516, 64) if fmt in self.formats]
        sw = max(1, columns // max(1, len(formats)))
        for index, fmt in enumerate(formats):
            image_id = 100 + fmt
            self.images.add(image_id)
            self.term.send(self.term.move_yx(top, index * sw))
            self.term.graphics.send(graphics.image(
                pixels(fmt, sw * cw, rows * ch), image_id=image_id, format=fmt,
                width=sw * cw, height=rows * ch, c=sw, r=rows, p=1, C=1, q=1))
        if 200 in self.formats:
            has_ink_image = 3 in self.images and not self.clear_ink
            self.images.update((2, 3))
            records = [
                graphics.vector_record('l', id=1, x1=0, y1=0, x2=240, y2=0,
                                       stops='0,#2266ff,1,#ff3388'),
                graphics.vector_record('g', id=2, box=(0, 0, 240, 100)),
                graphics.vector_record('p', id=3, g=2, d='M0,0H240V100H0Z', fill='@1'),
                graphics.vector_record('s', id=4, d='M0,-12L12,12H-12Z'),
                graphics.vector_record('g', id=5, p=2),
                graphics.vector_record('u', id=6, g=5, s=4, fill='#ffff44'),
                graphics.vector_record('k', id=7, g=2, pts='5,75,2,60,65,6,120,85,12',
                                       fill='#44ffdd'),
            ]
            if self.features['text'].status == 'OK':
                records.append(graphics.text_record(8, self.text_layout, group=2))
            self.term.send(self.term.move_yx(top, columns))
            self.term.graphics.send(graphics.scene(records, image_id=2, width=240,
                                                   height=100, c=columns, r=rows, p=1, C=1, q=1))
            self.term.send(self.term.move_yx(top, 2 * columns))
            if has_ink_image:
                self.term.graphics.send(graphics.place(3, p=1, c=columns, r=rows, C=1, q=1))
            else:
                self.term.graphics.send(graphics.scene([], image_id=3, width=columns * cw,
                                                       height=rows * ch, c=columns, r=rows,
                                                       p=1, C=1, q=1))
            if self.ink_owned:
                if not has_ink_image:
                    self.term.ink.send('brush', i=3, k=1, n=MAX_ID,
                                       fill='#44ffaa', w0=1, w1=8)
                granted = self.term.ink.read_mode(timeout=self.args.timeout)
                self.mark('ink', granted == 1, 'Draw with a pen over the right-hand artwork area.')
            self.clear_ink = False
        if self.args.gpu_surface and self.features['gpu'].status != 'not supported':
            self.images.add(4)
            self.term.send(self.term.move_yx(top, 3 * columns))
            self.term.graphics.send(self.term.graphics.gpu_surface(
                self.args.gpu_surface, image_id=4, s=self.args.gpu_width, v=self.args.gpu_height,
                c=columns, r=rows, p=1, C=1, q=0))
            self.mark('gpu', True, 'Surface submitted; awaiting terminal acknowledgement.',
                      status='pending')
            self.gpu_deadline = time.monotonic() + self.args.timeout

    def animate(self):
        """Replace one existing transform, so animation does not accumulate resources."""
        self.tick += 1
        if self.art and self.cell and self.features['vectors'].status == 'OK':
            angle = (time.monotonic() - self.started) * 1.5
            c, s = math.cos(angle), math.sin(angle)
            self.term.graphics.send(graphics.patch(2, [graphics.vector_record(
                'g', id=5, p=2, t=(c, s, -s, c, 120 + 80 * s, 55))], q=1))
        if 4 in self.images and self.features['gpu'].status == 'OK':
            self.term.graphics.send(graphics.command(a='r', i=4, q=2))
        if (self.features['gpu'].status == 'pending' and self.gpu_deadline is not None
                and time.monotonic() >= self.gpu_deadline):
            self.mark('gpu', False, 'GPU upload acknowledgement timed out.', status='error')

    def haptics(self):
        """Play one supported target on a user keypress, rather than buzzing continuously."""
        report = self.reports.get('haptic')
        caps = report.integer('caps', 0) if report else 0
        target = next((name for bit, name in ((1, 'trackpad'), (2, 'pen'), (4, 'dial'))
                       if caps & bit), None)
        if target:
            self.term.haptic.send('play', pattern='alignment', target=target)
            self.features['haptic'].detail = (
                f'Alignment tap requested on {target}; no acknowledgement is defined.')

    def handle(self, key):
        """Process every event, keeping counts and optionally streaming complete JSONL."""
        event = key.protocol_event
        if event is None:
            return self._handle_key(key)
        if self.event_log:
            record = {'protocol': event.protocol, 'fields': dict(event.fields),
                      'payload': event.payload}
            self.event_log.write(json.dumps(record, ensure_ascii=True) + '\n')
            self.event_log.flush()
        selector = event.protocol.removeprefix('silkie:')
        keys = [selector] if selector in self.features else []
        if selector == 'tablet-pointer':
            keys = self._observe_tablet(event)
        if event.protocol == 'graphics':
            image_id = event.integer('i')
            default = f'f{image_id - 100}' if image_id is not None else ''
            key = {1: 'pointer', 2: 'vectors', 3: 'ink', 4: 'gpu'}.get(image_id, default)
            if key in self.features:
                self.mark(key, event.payload == 'OK', event.payload,
                          status=None if event.payload == 'OK' else 'error')
        for feature_key in keys:
            feature = self.features[feature_key]
            feature.count += 1
            feature.detail = describe(event.fields)
        if selector == 'display':
            self.update_display(event)
        return True

    def _handle_key(self, key):
        """Navigate details and explicitly trigger demonstrations."""
        if key in {'q', '\x03'} or key.name == 'KEY_ESCAPE':
            return False
        if key == '\t':
            self.selected = (self.selected + 1) % len(self.features)
            _, _, _, rows, visible, _ = self.layout()
            self.offset = min(max(0, self.selected % rows - visible + 1),
                              max(0, rows - visible))
        elif key == 'h':
            self.haptics()
        elif key == 'c' and self.art:
            self.clear_ink = True
            self.art = None
        elif key in {'j', 'k'}:
            self.offset = max(0, self.offset + (1 if key == 'j' else -1))
        return True

    def _observe_tablet(self, event):
        """Route measurements without assuming a touchpad uses screen pixels."""
        kind = event.integer('kind')
        keys = (['hardware'] if event.operation == 'device' else
                [{1: 'pen', 2: 'touch', 3: 'pad', 4: 'actions'}.get(kind, 'hardware')])
        if 'ink' in event.fields:
            keys.append('ink')
        if event.integer('est', 0) or event.integer('pred', 0) or event.integer('phase') == 7:
            keys.append('estimates')
        if event.integer('conf') is not None:
            keys.append('accidental')
        haptic = self.reports.get('haptic')
        if kind == 1 and haptic and haptic.integer('caps', 0) & 8:
            if event.integer('phase') in {1, 2}:
                self.term.haptic.send('start', target='pen', pattern='pencil', intensity=50)
            elif event.integer('phase') in {3, 4, 5}:
                self.term.haptic.send('stop', target='pen')
        return keys


@contextlib.contextmanager
def exit_signals():
    """Unwind terminal/resource contexts on hangup, termination and job-control stop."""
    previous = {}

    def stop(signum, _frame):
        raise KeyboardInterrupt(signum)

    try:
        for name in ('SIGHUP', 'SIGTERM', 'SIGTSTP'):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def positive(value):
    """Accept a finite positive timeout or frame rate.

    :raises argparse.ArgumentTypeError: If the value is not finite and positive.
    """
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return result


def main(argv=None):
    """Open one screen, exercise all available extensions, and restore on exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=positive, default=0.3,
                        help='seconds per capability probe')
    parser.add_argument('--fps', type=positive, default=10, help='dashboard redraws per second')
    parser.add_argument('--events', type=Path,
                        help='append every complete input event to a JSONL file')
    parser.add_argument('--font', type=Path,
                        help='outline font file to upload and use in vector text')
    parser.add_argument('--gpu-surface', help='existing local iosurface: or dmabuf: descriptor')
    parser.add_argument('--gpu-width', type=int, help='GPU surface pixel width')
    parser.add_argument('--gpu-height', type=int, help='GPU surface pixel height')
    args = parser.parse_args(argv)
    dimensions = (args.gpu_width, args.gpu_height)
    if args.gpu_surface and (not args.gpu_surface.startswith(('iosurface:', 'dmabuf:'))
                             or any(dim is None or not 1 <= dim <= MAX_ID for dim in dimensions)):
        parser.error('--gpu-surface requires an iosurface:/dmabuf: descriptor '
                     'and positive pixel dimensions')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('Run this interactive showcase with terminal input and output.')
    term = Terminal()
    if not term.does_styling:
        parser.error('Terminal styling is disabled; unset NO_COLOR to run the visual showcase.')
    try:
        with exit_signals(), contextlib.ExitStack() as resources:
            log = (resources.enter_context(args.events.open('a', encoding='utf-8'))
                   if args.events else None)
            resources.enter_context(term.cbreak())
            resources.enter_context(term.fullscreen())
            resources.enter_context(term.hidden_cursor())
            demo = Showcase(term, args, log)
            resources.callback(demo.release_images)
            with contextlib.ExitStack() as modes:
                modes.callback(term.send, term.normal)
                demo.line(0, 'Probing Silkie extensions... '
                          'unsupported probes time out independently.')
                term.stream.flush()
                demo.discover(modes)
                with term.notify_on_resize(timeout=args.timeout):
                    running = True
                    interval = 1 / args.fps
                    deadline = time.monotonic()
                    while running:
                        now = time.monotonic()
                        if now >= deadline:
                            demo.render()
                            deadline = time.monotonic() + interval
                        key = term.inkey(timeout=max(0, deadline - time.monotonic()))
                        if key or key.name:
                            running = demo.handle(key)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
