"""Wire examples, streaming, input preservation and lifecycle tests for Hamingjur."""
# std imports
import io
import re
import json
import zlib
import base64
import asyncio
import os
import sys
import time
from collections import deque
from unittest import mock

# 3rd party
import pytest

# local
from hamingjur import Terminal
from hamingjur import graphics, kitty, silkie
from hamingjur.protocols import AgentEvent, TabletEvent, parse_event
from hamingjur.keyboard import _read_until, resolve_sequence
from hamingjur.line_editor import LineEditor, LineHistory
from .accessories import TestTerminal, pty_test, SEMAPHORE, read_until_semaphore
from .conftest import IS_WINDOWS

ST = '\x1b\\'


@pytest.fixture(name='term')
def terminal_fixture(monkeypatch):
    """A forced terminal with no real input/output descriptor."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    terminal = TestTerminal(stream=io.StringIO(), force_styling=True)
    terminal._keyboard_fd = None
    return terminal


def graphics_payload(frames):
    """Reassemble packets as a terminal does, checking the documented wire limits."""
    events = [parse_event(frame) for frame in frames]
    assert events[-1].fields['m'] == '0'
    assert all(event.fields['m'] == '1' for event in events[:-1])
    assert all(len(event.payload) <= 4096 for event in events)
    payload = base64.b64decode(''.join(event.payload for event in events), validate=True)
    return zlib.decompress(payload) if events[0].fields.get('o') == 'z' else payload


@pytest.mark.parametrize('size', [0, 1, 2, 3, 3071, 3072, 3073, 65536, 200003])
@pytest.mark.parametrize('compress', [False, True])
def test_graphics_streaming(size, compress):
    """Boundary sizes and transfers beyond 64 KiB round trip exactly."""
    data = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    blocks = (data[offset:offset + 7] for offset in range(0, size, 7))
    frames = list(graphics.transmit(blocks, a='T', f=100, i=9, compress=compress))
    assert graphics_payload(frames) == data
    assert all(set(parse_event(frame).fields) <= {'m'} for frame in frames[1:])


def test_short_reads_and_animation_continuations():
    """Short reads are not EOF; animation action/quiet fields survive continuation."""
    class ShortReader(io.BytesIO):
        """A stream that returns data in smaller blocks than requested."""

        def read(self, size=-1):
            return super().read(min(size, 17))

    data = b'\x00\xff' * 50000
    frames = list(graphics.transmit(ShortReader(data), a='f', i=1, q=2))
    assert graphics_payload(frames) == data
    assert all(parse_event(frame).fields['a'] == 'f' for frame in frames)
    assert all(parse_event(frame).fields['q'] == '2' for frame in frames)


def test_vector_text_and_records():
    """Text retains Unicode/control characters through JSON, base64 and scene transport."""
    layout = {'text': 'Hello\n世界\x1b', 'fill': 'p3:1/0.2/0*250', 'size': 24}
    record = graphics.text_record(1, layout)
    assert json.loads(base64.b64decode(record.split(' text=')[1])) == layout
    scene = graphics.scene([record, graphics.vector_record('g', id=0, box=(0, 0, 20, 30))],
                           image_id=2, width=200, height=100)
    assert record.encode() in graphics_payload(scene)
    patch = list(graphics.patch(2, ['a id=3 pts=1,2,3', 'x id=1']))
    assert parse_event(patch[0]).fields['a'] == 'e'
    assert graphics_payload(patch) == b'a id=3 pts=1,2,3\nx id=1\n'


def test_graphics_all_control_actions():
    """Animation/composition, virtual/relative placements, GPU refresh and deletion encode."""
    for action in ('a', 'c', 'r'):
        assert graphics.command(a=action, i=1).startswith(f'\x1b_Ga={action},i=1')
    assert graphics.place(1, U=1, c=5, r=3, P=2, Q=4, z=-1).endswith('z=-1' + ST)
    assert graphics.delete(1) == '\x1b_Ga=d,d=I,i=1' + ST
    assert graphics.delete(free=False) == '\x1b_Ga=d,d=a' + ST


@pytest.mark.parametrize('medium', ['f', 't', 's', 'g'])
def test_graphics_resource_transports(medium):
    """Resource transports encode their names once, without illegal continuation fields."""
    frame, = graphics.transmit(b'/resource', t=medium, i=1)
    event = parse_event(frame)
    assert 'm' not in event.fields
    assert event.data == b'/resource'


@pytest.mark.parametrize('pixel_format,pixel', [(24, b'123'), (32, b'1234'), (64, b'12345678')])
def test_raster_formats(pixel_format, pixel):
    """RGB/RGBA/half-float formats enforce dimensions without truncating pixel data."""
    assert graphics_payload(graphics.image(pixel, image_id=1, format=pixel_format,
                                           width=1, height=1)) == pixel
    with pytest.raises(ValueError):
        list(graphics.image(pixel + b'!', image_id=1, format=pixel_format, width=1, height=1))


TABLET = ('\x1b]777;tablet-pointer;v=1:op=event:kind=1:phase=2:device=3:'
          'contact=0:t=408211:tu=73:x=25600:y=-512:valid=3:p=65535:tx=-1200:ty=600') + ST


def test_tablet_event(term):
    """Tablet reports retain raw units and expose calibrated convenience values."""
    term.ungetch(TABLET + 'a')
    key = term.inkey(timeout=0)
    assert isinstance(key.tablet, TabletEvent)
    assert key.tablet.position == (100, -2)
    assert key.tablet.pressure == 1
    assert key.tablet.tilt == (-12, 6)
    assert key.tablet.timestamp == pytest.approx(408.211073)
    assert key.tablet.integer('device') == 3
    assert key.tablet.integer('unknown') is None
    assert term.inkey(timeout=0) == 'a'


@pytest.mark.parametrize('selector', [
    'tablet-pointer', 'scroll', 'gesture', 'haptic', 'ink',
    'font', 'display', 'latency', 'frame', 'future-extension',
])
@pytest.mark.parametrize('terminator', [ST, '\x07'])
def test_all_silkie_selectors(selector, terminator):
    """All version-one channels, including future ones, preserve extension fields."""
    sequence = silkie.command(selector, 'event', t=3, future='preserved')[:-2] + terminator
    event = parse_event(sequence)
    assert event.protocol == 'silkie:' + selector
    assert event.fields['future'] == 'preserved'


@pytest.mark.parametrize('async_read', [False, True])
def test_fragmented_messages_survive_timeout(term, async_read):
    """Incomplete reports survive a timeout and can exceed the former query buffer size."""
    sequence = silkie.command('tablet-pointer', 'device', name='A' * 100000)
    if async_read:
        term._async_read_byte = mock.AsyncMock(return_value=None)
    read = (lambda: asyncio.run(term.async_inkey(timeout=0))) if async_read else (
        lambda: term.inkey(timeout=0))
    term.ungetch(sequence[:-2])
    assert not read()
    # Simulate newly arriving bytes, after the buffered prefix, not pushback order.
    term._keyboard_buf.appendleft(ST + 'z')
    event = read().tablet
    assert event is not None and len(event.fields['name']) == 100000
    assert read() == 'z'


def test_many_reports_are_not_dropped(term):
    """Sustained mixed input preserves every tablet report and keyboard byte."""
    term.ungetch((TABLET + 'x') * 1000)
    for _ in range(1000):
        assert term.inkey(timeout=0).tablet is not None
        assert term.inkey(timeout=0) == 'x'


def test_query_larger_than_old_limit():
    """A valid reply arriving after more than 65536 characters still completes."""
    terminal = mock.Mock()
    terminal._keyboard_buf = deque()
    terminal._read_available.side_effect = ['x' * 70000, 'DONE']
    terminal.kbhit.return_value = True
    match, text = _read_until(terminal, 'DONE', timeout=1)
    assert match is not None and len(text) == 70004


def test_query_preserves_other_input(term):
    """Matching tolerates field order and consumes only the requested ID and selector."""
    term._is_a_tty = True
    other = silkie.command('scroll', 'event', dy=250, t=1)
    reply = '\x1b]777;caps;op=cap:format=24,32,64,100,200:media=d,g:ext=caps.1,font.1:'
    reply += 'id=1:v=1:caps=3' + ST
    term.ungetch('x' + other + reply + '\x1b[1;1R')
    caps = term.silkie.capabilities(timeout=0)
    assert caps.supports('font') and 200 in caps.formats and 'g' in caps.media
    assert term.silkie.capabilities(timeout=0) is caps
    assert term.inkey(timeout=0) == 'x'
    assert term.inkey(timeout=0).protocol_event.protocol == 'silkie:scroll'


def test_query_boundary_timeout_preserves_data(term):
    """A multi-response query must not discard keyboard input when its CPR times out."""
    term._is_a_tty = True
    term.ungetch('still here')
    assert term._query_boundary_multiple('query', re.compile('reply'), 0) is None
    assert term.flushinp() == 'still here'


def test_modes_restore_when_body_raises(term):
    """Read-back exposes refused bits and exceptions restore the original mode."""
    # The deliberately raised exception is caught by pytest.raises.
    # pylint: disable=missing-raises-doc
    with mock.patch.object(term.silkie.tablet, 'read_mode', side_effect=[4, 3]):
        with pytest.raises(RuntimeError, match='application'):
            with term.silkie.tablet.enabled(11) as granted:
                assert granted == 3
                raise RuntimeError('application')
    assert term.stream.getvalue() == (
        silkie.command('tablet-pointer', 'mode', mode=11) +
        silkie.command('tablet-pointer', 'mode', mode=4))


def test_modes_restore_when_readback_times_out(term):
    """Failure during context entry still restores a mode that was changed."""
    with mock.patch.object(term.silkie.gesture, 'read_mode', side_effect=[0, None]):
        with pytest.raises(RuntimeError, match='timed out'):
            with term.silkie.gesture.enabled():
                pytest.fail('must not enter')
    assert term.stream.getvalue().endswith(silkie.command('gesture', 'mode', mode=0))


def test_clipboard_notification_dnd_and_files():
    """Large binary/Unicode transfers round trip through every chunked OSC protocol."""
    data = bytes(range(256)) * 400
    clipboard = [parse_event(frame) for frame in kitty.clipboard_write({'image/png': data})]
    assert b''.join(event.data for event in clipboard[1:-1]) == data
    assert all(len(event.data) <= 4096 for event in clipboard)
    text = 'Hello, 世界!\n' * 10000
    notices = [parse_event(frame) for frame in kitty.notify(text, identifier='notice')]
    assert b''.join(event.data for event in notices).decode() == text
    assert all(event.fields['d'] == '0' for event in notices[:-1])
    assert notices[-1].fields['d'] == '1'
    dropped = [parse_event(frame) for frame in kitty.dnd_data('r', data, x=1)]
    assert base64.b64decode(''.join(event.payload for event in dropped)) == data
    assert all(len(event.payload) <= 4096 for event in dropped)
    assert dropped[0].protocol == 'dnd'
    files = [parse_event(frame) for frame in kitty.file_data(data, id='transfer', file_id='one')]
    assert b''.join(base64.b64decode(event.fields.get('d', '')) for event in files) == data
    assert files[-1].operation == 'end_data'


def test_styling_and_no_styling(term):
    """Extended styles remain callable and respect output without styling."""
    assert str(term.background_alpha(30)) == '\x1b[70:30m'
    assert term.color_gamut(255, 0, 0, gamut=1)('text').startswith('\x1b[38:2:1:255:0:0mtext')
    assert str(term.foreground_brightness(250)) == '\x1b[56:250m'
    term._does_styling = False
    assert term.background_alpha(30)('text') == 'text'
    term.graphics.send(graphics.image(b'png', image_id=1))
    assert term.stream.getvalue() == ''


def test_validation_rejects_injection():
    """Unsafe control fields fail before they become terminal commands."""
    for bad in ('x:y', 'x;y', 'x\x1b\\', 'x\x07', 'x\n'):
        with pytest.raises(ValueError):
            silkie.command('font', 'select', role=bad)
    with pytest.raises(ValueError):
        list(graphics.transmit(b'a', i=2**32))
    with pytest.raises(ValueError):
        list(graphics.transmit(b'a', t='g', compress=True))
    with pytest.raises(ValueError):
        parse_event('\x1b]777;font;v=1:op=cap:v=1' + ST)
    assert resolve_sequence('\x1b]777;font;v=2:op=cap' + ST, {}, {}).name == 'PROTOCOL_UNKNOWN'


def test_unlimited_editor_and_history(term):
    """Default retention exceeds every previous arbitrary editor limit."""
    editor = LineEditor()
    editor.insert_text('x' * 3000)
    assert len(editor.line) == 3000
    for _ in range(150):
        editor.insert_text('y')
    for _ in range(150):
        editor.feed_key(resolve_sequence('\x1a', {}, {}))
    assert editor.line == 'x' * 3000
    history = LineHistory()
    for i in range(6000):
        history.add(str(i))
    assert len(history.entries) == 6000 and history.entries[0] == '0'
    assert 'x' * 10000 in term.link('https://example.com/' + 'x' * 10000, 'link', 'i' * 300)


def test_hamingjur_namespace():
    """The public classes belong to the fork's namespace."""
    assert Terminal.__module__.startswith("hamingjur.")
    assert LineEditor.__module__ == "hamingjur.line_editor"


@pytest.mark.skipif(IS_WINDOWS, reason='PTY testing requires Unix')
@pytest.mark.parametrize('async_read', [False, True])
def test_fragmented_protocol_over_pty(async_read):
    """Split header/body writes produce one event, even beyond the ESC ambiguity delay."""
    def child(terminal):
        with terminal.cbreak():
            os.write(sys.__stdout__.fileno(), SEMAPHORE)
            key = (asyncio.run(terminal.async_inkey(timeout=1, esc_delay=0.1)) if async_read
                   else terminal.inkey(timeout=1, esc_delay=0.1))
            assert key.tablet is not None
            assert str(key) == TABLET
            assert terminal.inkey(timeout=0.1) == 'z'
        return b'OK'

    def parent(fd):
        read_until_semaphore(fd)
        os.write(fd, TABLET[:4].encode())
        time.sleep(0.01)
        os.write(fd, TABLET[4:-2].encode())
        time.sleep(0.15)
        os.write(fd, (ST + 'z').encode())

    assert pty_test(child, parent, 'fragmented_protocol') == 'OK'


def test_vector_service_large_result_and_errors(term):
    """Font listings are neither truncated nor mistaken for keyboard input."""
    term._is_a_tty = True
    result = {'fonts': ['Font ' + str(i) for i in range(10000)]}
    payload = base64.b64encode(json.dumps(result).encode()).decode()
    term.ungetch('\x1b_Gi=31;OK;' + payload + ST + '\x1b[1;1R' + 'z')
    assert term.graphics.service('fonts', timeout=0) == result
    assert term.inkey(timeout=0) == 'z'
    error = base64.b64encode(b'{"error":"missing font"}').decode()
    with pytest.raises(ValueError, match='missing font'):
        graphics.service_result('\x1b_Gi=31;EINVAL:' + error + ST)


def test_query_exact_id_and_negative_response(term):
    """An unrelated graphics response remains input; error text containing OK is not success."""
    term._is_a_tty = True
    term.ungetch('\x1b_Gi=310;OK' + ST + '\x1b_Gi=31;EINVAL:not OK' + ST + '\x1b[1;1R')
    with mock.patch.object(term, '_is_apple_terminal', return_value=False):
        assert not term.graphics.supports(timeout=0)
    assert term.inkey(timeout=0).protocol_event.fields['i'] == '310'


def test_touchpad_units_and_optional_axes():
    """Touchpad coordinates are not interpreted as screen pixels or missing pressure as zero."""
    event = parse_event(silkie.command(
        'tablet-pointer', 'event', kind=2, src=1, x=65535, y=0))
    assert event.position is None and event.touchpad_position == (1, 0)
    assert event.pressure is None and event.tilt is None


def test_other_protocol_wire_examples(term):
    """Match specified sequences for fonts, colors, pointers, cursors and file metadata."""
    assert term.silkie.select_font(role='serif', weight=300, slant=0, italic=True) == (
        '\x1b]777;font;v=1:op=select:role=serif:weight=300:slant=0:italic=1' + ST)
    assert kitty.pointer('image:9:24:24') == '\x1b]22;=image:9:24:24' + ST
    assert kitty.colors({0: '#123456', 'foreground': '?'}) == (
        '\x1b]21;0=#123456;foreground=?' + ST)
    assert kitty.cursors(29, [(2, 4, 5)]) == '\x1b[>29;2:4:5 q'
    assert kitty.file_transfer('send', id='test', name='somefile', size=3, data=b'\1\2\3') == (
        '\x1b]5113;ac=send;id=test;n=c29tZWZpbGU=;sz=3;d=AQID' + ST)
    assert kitty.clipboard_read() == '\x1b]5522;type=read;dGV4dC9wbGFpbg==' + ST


def test_notification_repeated_metadata():
    """Multiple icons/types, supported by OSC 99, remain accessible in received messages."""
    frame = kitty.notification_packet(metadata=[('n', 'one'), ('n', 'two'), ('p', '?')])
    event = parse_event(frame)
    assert event.values('n') == ('one', 'two')


def test_placeholder_and_signed_relative_offsets():
    """Placeholder ID bits use the specified diacritics and relative offsets allow negatives."""
    rows = list(graphics.placeholder(33554474, 2, 2))
    assert '\U0010eeee\u0305\u0305\u030e' in rows[0]
    assert '\U0010eeee\u030d\u0305\u030e\U0010eeee' in rows[1]
    assert next(graphics.placeholder(42, 1, 10000)).count('\U0010eeee') == 10000
    assert graphics.place(1, P=2, H=-3, V=-4).endswith('H=-3,V=-4' + ST)


def test_mouse_leave_event():
    """The Kitty mouse-leave bit takes precedence over unreliable buttons and modifiers."""
    from hamingjur.mouse import MouseSGREvent, RE_PATTERN_MOUSE_SGR
    match = RE_PATTERN_MOUSE_SGR.match('\x1b[<288;1;1M')
    event = MouseSGREvent.from_sgr_match(match)
    assert event.is_leave and event.button == 'LEAVE'


@pytest.mark.parametrize('pixel_format', [301, 302, 304, 308, 516])
@pytest.mark.parametrize('width', [1, 9, 65537])
@pytest.mark.parametrize('compress', [False, True])
def test_packed_images(pixel_format, width, compress):
    """Packed rows align to bytes and large payloads survive streaming and compression."""
    data = bytes(range(256)) * (((width * (pixel_format % 100) + 7) // 8 * 3 + 255) // 256)
    data = data[:((width * (pixel_format % 100) + 7) // 8) * 3]
    options = dict(image_id=7, format=pixel_format, width=width, height=3, compress=compress)
    assert graphics_payload(graphics.image(data, **options)) == data
    assert graphics_payload(graphics.image(io.BytesIO(data), **options)) == data
    with pytest.raises(ValueError, match='dimensions'):
        list(graphics.image(data + b'!', **options))
    with pytest.raises(ValueError, match='dimensions'):
        list(graphics.image(data[:-1], **options))


def test_packed_wire_examples_and_resource_names():
    """Examples retain MSB grayscale padding and little-endian RGB565 channel order."""
    frame, = graphics.image(b'\x55', image_id=42, format=301, width=8, height=1)
    assert parse_event(frame).payload == 'VQ=='
    assert parse_event(frame).fields['f'] == '301'
    for data, width, height, pixel_format in [
            (b'\x1b', 4, 1, 302), (b'\x00\x80\x00\xff', 9, 2, 301),
            (b'\x00\xf8\xe0\x07\x1f\x00', 3, 1, 516)]:
        assert graphics_payload(graphics.image(data, image_id=1, format=pixel_format,
                                               width=width, height=height)) == data
    for medium in ('f', 't', 's'):
        frame, = graphics.image(b'/packed-image', image_id=1, format=301,
                                width=9, height=2, t=medium)
        assert parse_event(frame).data == b'/packed-image'
    with pytest.raises(ValueError, match='width and height'):
        list(graphics.image(b'\0', image_id=1, format=301))


@pytest.mark.parametrize('pixel_format,size', [
    (301, 1), (302, 1), (304, 1), (308, 1), (516, 2),
    (8, 1), (16, 2), (316, 2), (632, 4), (664, 8),
    (401, 9), (402, 17), (404, 65), (408, 1025),
])
def test_packed_format_probe(term, pixel_format, size):
    """Discovery sends a complete one-pixel payload, including every palette slot."""
    with mock.patch.object(term.graphics, 'query', return_value=parse_event(
            '\x1b_Gi=31;OK' + ST)) as query:
        with mock.patch.object(term, '_is_apple_terminal', return_value=False):
            assert term.graphics.supports(pixel_format)
    assert query.call_args.args == (b'\0' * size,)
    assert query.call_args.kwargs['f'] == pixel_format


@pytest.mark.parametrize('pixel_format,bits,palette_size', [
    (401, 1, 8), (402, 2, 16), (404, 4, 64), (408, 8, 1024),
])
@pytest.mark.parametrize('width', [1, 9, 65537])
@pytest.mark.parametrize('compress', [False, True])
def test_palette_images(pixel_format, bits, palette_size, width, compress):
    """A full RGBA palette precedes separately padded rows through every transport."""
    palette = bytes(range(256)) * ((palette_size + 255) // 256)
    data = palette[:palette_size] + b'\x55' * (((width * bits + 7) // 8) * 3)
    options = dict(image_id=7, format=pixel_format, width=width, height=3, compress=compress)
    assert graphics_payload(graphics.image(data, **options)) == data
    assert graphics_payload(graphics.image(io.BytesIO(data), **options)) == data
    for invalid in (data[:-1], data + b'!', data[palette_size:]):
        with pytest.raises(ValueError, match='dimensions'):
            list(graphics.image(invalid, **options))
    for medium in ('f', 't', 's'):
        frame, = graphics.image(b'/palette-image', image_id=7, format=pixel_format,
                                width=width, height=3, t=medium)
        assert parse_event(frame).data == b'/palette-image'
    with pytest.raises(ValueError, match='width and height'):
        list(graphics.image(data, image_id=7, format=pixel_format))


def test_palette_wire_example():
    """Silkie's documented two-bit palette example is emitted unchanged."""
    data = bytes.fromhex('00000000 ff0000ff 0000ff80 ffffffff 6c 34')
    frame, = graphics.image(data, image_id=7, format=402, width=3, height=2)
    assert frame == '\x1b_Ga=T,i=7,f=402,s=3,v=2,m=0;AAAAAP8AAP8AAP+A/////2w0' + ST


@pytest.mark.parametrize('pixel_format,pixel', [
    (8, b'\x01'), (16, b'\x01\x80'), (316, b'\x00\x80'),
    (632, b'\xff\x03\x00\xc0'), (664, b'\xff\xff\0\0\0\0\0\x80'),
])
def test_indexed_and_high_precision_images(pixel_format, pixel):
    """Indexed, gray16, RGB10A2 and RGBA16 use their actual pixel sizes."""
    options = dict(image_id=1, format=pixel_format, width=3, height=2)
    data = pixel * 6
    assert graphics_payload(graphics.image(data, **options)) == data
    for invalid in (data[:-1], data + b'!'):
        with pytest.raises(ValueError, match='dimensions'):
            list(graphics.image(invalid, **options))


@pytest.mark.parametrize('pixel_format', [1, 2, 4, 303, 403, 416])
def test_unknown_image_formats(pixel_format):
    """Obsolete grayscale identifiers and gaps in format families are rejected."""
    with pytest.raises(ValueError, match='unknown image format'):
        list(graphics.image(b'\0', image_id=1, format=pixel_format, width=1, height=1))


def test_depth_styles_and_vector_attributes(term):
    """Depth styles reset spatial attributes and leave text layout and vector fields intact."""
    assert silkie.depth(-150) == '\x1b]777;depth;v=1:op=set:d=-150' + ST
    assert silkie.depth(pixels=-0.25, phase=0.125) == (
        '\x1b]777;depth;v=1:op=set:p=-0.25:y=0.125' + ST)
    assert silkie.depth(phase=0) == '\x1b]777;depth;v=1:op=set:y=0' + ST
    reset = '\x1b]777;depth;v=1:op=reset' + ST
    assert silkie.depth() == reset
    styled = term.depth(250)('hello')
    assert styled == silkie.depth(250) + 'hello' + reset
    assert term.length(styled) == 5 and term.strip_seqs(styled) == 'hello'
    assert graphics.vector_record('g', id=1, depth=-150, phase=0.125) == (
        'g id=1 depth=-150 phase=0.125')
    assert graphics.vector_record('p', id=2, g=1, depth_px=-0.25) == (
        'p id=2 g=1 depth_px=-0.25')
    term._does_styling = False
    assert term.depth(pixels=-0.25)('hello') == 'hello'
    assert str(term.depth()) == ''


@pytest.mark.parametrize('options', [
    {'millidegrees': 1, 'pixels': 0}, {'millidegrees': 2**31},
    {'millidegrees': -2**31 - 1}, {'millidegrees': True},
    {'millidegrees': 0.5}, {'pixels': float('inf')}, {'phase': float('nan')},
    {'pixels': 1e39}, {'phase': 'x:y'}, {'pixels': True},
])
def test_depth_rejects_invalid_attributes(options):
    """Conflicting units and unrepresentable attributes fail before output."""
    with pytest.raises(ValueError):
        silkie.depth(**options)


def test_agent_request_and_reply():
    """Agent packets include their wire length and use base64 in a field, not a payload."""
    packet = b'\0\0\0\1\x0b'
    sequence = silkie.agent_request(packet, identifier='request-1', connection='connection-1',
                                    generation='providerA', expires=1794787200000)
    assert sequence == ('\x1b]777;ssh-agent;v=1:op=request:id=request-1:c=connection-1:'
                        'g=providerA:expires=1794787200000:data=AAAAAQs=' + ST)
    event = parse_event(sequence)
    assert isinstance(event, AgentEvent) and event.data == packet
    failure = parse_event(silkie.command('ssh-agent', 'reply', id='r', c='c', g='g',
                                         error='changed', data='AAAAAQU='))
    assert failure.data == b'\0\0\0\1\5' and failure.fields['error'] == 'changed'


def test_agent_queries_and_exact_correlation(term):
    """Token IDs are escaped; connection and generation mismatches remain separate input."""
    term._is_a_tty = True
    identifier = '.probe-1'
    wrong_id = silkie.command('ssh-agent', 'cap', id='Xprobe-1', caps=0)
    cap = silkie.command('ssh-agent', 'cap', id=identifier, caps=1, g='providerA')
    term.ungetch('x' + wrong_id + cap + '\x1b[1;1R')
    result = term.ssh_agent.query(timeout=0, identifier=identifier)
    assert result.fields['g'] == 'providerA'
    assert term.inkey(timeout=0) == 'x'
    assert term.inkey(timeout=0).protocol_event.raw == wrong_id
    wrong_generation = silkie.command('ssh-agent', 'reply', id=identifier, c='1', g='old',
                                      data='AAAAAQU=')
    wrong_connection = silkie.command('ssh-agent', 'reply', id=identifier, c='2', g='providerA',
                                      data='AAAAAQU=')
    reply = silkie.command('ssh-agent', 'reply', id=identifier, c='1', g='providerA',
                           data='AAAAAQU=', error='unavailable')
    term.ungetch(wrong_generation + wrong_connection + reply + '\x1b[1;1R')
    result = term.ssh_agent.exchange(b'\0\0\0\1\x0b', identifier=identifier, connection='1',
                                     generation='providerA', expires=2**64 - 1, timeout=0)
    assert result.data == b'\0\0\0\1\5' and result.fields['error'] == 'unavailable'
    assert term.inkey(timeout=0).protocol_event.raw == wrong_generation
    assert term.inkey(timeout=0).protocol_event.raw == wrong_connection
    term.ssh_agent.close('1', 'providerA', identifier='close-1')
    assert term.stream.getvalue().endswith(silkie.command(
        'ssh-agent', 'close', id='close-1', c='1', g='providerA'))


@pytest.mark.parametrize('async_read', [False, True])
def test_large_agent_packet_and_tokens(term, async_read):
    """Unfinished agent replies survive timeouts without a payload or token length limit."""
    body = b'\x0c' + b'k' * 100000
    packet = len(body).to_bytes(4, 'big') + body
    sequence = silkie.command('ssh-agent', 'reply', id='1' * 10000, c='c', g='g',
                              data=base64.b64encode(packet).decode())
    if async_read:
        term._async_read_byte = mock.AsyncMock(return_value=None)
    read = (lambda: asyncio.run(term.async_inkey(timeout=0))) if async_read else (
        lambda: term.inkey(timeout=0))
    term.ungetch(sequence[:-2])
    assert not read()
    term._keyboard_buf.appendleft(ST + 'z')
    event = read().protocol_event
    assert isinstance(event, AgentEvent) and event.data == packet
    assert len(event.fields['id']) == 10000 and read() == 'z'


@pytest.mark.parametrize('packet', [
    b'', b'\0\0\0\0', b'\0\0\0\2\x0b', b'\0\0\0\1\x0bextra'])
def test_agent_packet_validation(packet):
    """Truncated packets, extra packets and missing message types are refused."""
    with pytest.raises(ValueError, match='complete SSH agent packet'):
        silkie.agent_request(packet, identifier='r', connection='c', generation='g', expires=0)
    sequence = silkie.command('ssh-agent', 'reply', data=base64.b64encode(packet).decode())
    event = parse_event(sequence)
    with pytest.raises(ValueError):
        _ = event.data


def test_agent_deadlines_tokens_and_no_retry(term):
    """Invalid wire fields fail locally; timeout performs exactly one request."""
    fields = dict(identifier='1', connection='.c', generation='-g', expires=0)
    for key, value in [('identifier', ''), ('connection', 'x:y'), ('generation', 'é'),
                       ('expires', -1), ('expires', 2**64), ('expires', True)]:
        with pytest.raises(ValueError):
            silkie.agent_request(b'\0\0\0\1\x0b', **{**fields, key: value})
    with mock.patch.object(term.silkie, 'request', return_value=None) as request:
        assert term.ssh_agent.exchange(b'\0\0\0\1\x0b', **fields, timeout=0) is None
    assert request.call_count == 1
    term._does_styling = False
    term.ssh_agent.close('c', 'g')
    assert term.stream.getvalue() == ''


def test_routed_multipart_notifications(term):
    """Every fragment retains the route and replies expose complete hops and effective actions."""
    hops = tuple(('odin-seat', f'session{i}', i) for i in range(1000))
    route = kitty.notification_route(hops)
    frames = list(kitty.notify('hello' * 1000, 'body' * 1000, identifier='notice',
                               r=route, a='focus,report'))
    assert len(frames) > 2
    assert all(parse_event(frame).notification_route == hops for frame in frames)
    route += ',silkie/terminalC/12'
    metadata = [('i', 'notice'), ('r', route), ('a', 'focus,report')]
    term.ungetch(kitty.notification_packet('2', metadata=metadata))
    event = term.inkey(timeout=0).protocol_event
    assert event.payload == '2' and event.fields['a'] == 'focus,report'
    assert event.notification_route == hops + (('silkie', 'terminalC', 12),)
    plain = parse_event(kitty.notification_packet('0', metadata=[('i', 'plain')]))
    assert plain.notification_route == ()


@pytest.mark.parametrize('route', ['a/b', '/b/1', 'a/b/-1', 'a/b/1,', 'a.b/c/1', 'a/b/x'])
def test_invalid_notification_route(route):
    """Routes reject malformed hop delimiters, alphabets and window IDs."""
    event = parse_event(kitty.notification_packet(metadata=[('r', route)]))
    with pytest.raises(ValueError):
        _ = event.notification_route


def test_new_discovery_and_display_fields(term):
    """Packed formats, depth, agent support and unequal display widths remain independent."""
    caps = silkie.Capabilities(parse_event(silkie.command(
        'caps', 'cap', caps=3, ext='caps.1,depth.1,ssh-agent.1,display.1', media='d,f,t,s',
        format='8,16,24,32,64,100,200,301,302,304,308,316,401,402,404,408,516,632,664')))
    assert caps.supports('depth') and caps.supports('ssh-agent')
    assert {301, 302, 304, 308, 401, 402, 404, 408, 516} <= caps.formats
    term.ungetch(silkie.command('display', 'event', color='rgb', bits=0, red_bits=5,
                                green_bits=6, blue_bits=5, gray_bits=0, precision='framebuffer'))
    event = term.inkey(timeout=0).protocol_event
    assert [event.integer(channel + '_bits') for channel in ('red', 'green', 'blue')] == [5, 6, 5]
    assert event.fields['precision'] == 'framebuffer' and event.integer('bits') == 0
