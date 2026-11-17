#!/usr/bin/env python3
"""Draw in Silkie with --mode tiles, vectors, or ink; q or Escape exits."""
from __future__ import annotations

# std imports
import math
import argparse
import itertools
import contextlib

# local
from hamingjur import Terminal, graphics
from hamingjur.silkie import TabletMode

# Graphics image and vector element IDs are unsigned 32-bit protocol fields.
MAX_ID = 0xffffffff
CURSOR_IMAGE = 1
VECTOR_IMAGE = 2
INK_IMAGE = 3
COLOR = '#44aaff'
RGB = bytes((0x44, 0xaa, 0xff))


def pen_pixels():
    """Make a small pen pointer in pure Python; its nib is at pixel (3, 28)."""
    # This is fixed artwork geometry, not a storage or input limit.
    polygons = [
        (((3, 28), (6, 18), (22, 2), (30, 10), (14, 26)), (24, 28, 36, 255)),
        (((6, 25), (8, 19), (13, 24)), (255, 238, 198, 255)),
        (((9, 18), (22, 5), (27, 10), (14, 23)), (240, 246, 255, 255)),
        (((11, 19), (23, 7), (25, 9), (13, 21)), (68, 170, 255, 255)),
    ]
    pixels = bytearray(32 * 32 * 4)
    # Raster artwork has nested polygon/row/pixel/edge iteration.
    # pylint: disable=too-many-nested-blocks
    for polygon, color in polygons:
        for y in range(32):
            for x in range(32):
                inside = False
                for start, end in zip(polygon, polygon[1:] + polygon[:1]):
                    if (start[1] > y + 0.5) != (end[1] > y + 0.5):
                        edge = start[0] + (y + 0.5 - start[1]) * (
                            end[0] - start[0]) / (end[1] - start[1])
                        if x + 0.5 < edge:
                            inside = not inside
                if inside:
                    offset = (y * 32 + x) * 4
                    pixels[offset:offset + 4] = bytes(color)
    return pixels


@contextlib.contextmanager
def pen_pointer(term):
    """Push an image-backed pen pointer, then pop it before releasing its image."""
    try:
        term.graphics.send(graphics.image(pen_pixels(), image_id=CURSOR_IMAGE, format=32,
                                          width=32, height=32, display=False, q=2))
        with term.kitty.pointer(f'image:{CURSOR_IMAGE}:3:28'):
            yield
    finally:
        term.graphics.send(graphics.delete(CURSOR_IMAGE, q=2))


class Vectors:
    """Append application-driven strokes to a vector image."""

    def __init__(self, term, width, height):
        self.term = term
        self.size = width, height
        self.ids = itertools.count(1)

    def open(self):
        """Place the canvas at the origin, at its natural pixel size."""
        self.term.graphics.send(itertools.chain(
            [self.term.move_yx(0, 0)],
            graphics.scene([], image_id=VECTOR_IMAGE, width=self.size[0],
                           height=self.size[1], C=1, q=2)))

    def start(self, point):
        """
        Allocate a stroke.

        :raises OverflowError: When the protocol's element ID space is exhausted.
        """
        stroke = next(self.ids)
        if stroke > MAX_ID:
            raise OverflowError('Vector element ID space exhausted')
        self.edit('k', stroke, pts=point, fill=COLOR)
        return stroke

    def edit(self, kind, stroke, **fields):
        """Send one scene edit."""
        self.term.graphics.send(graphics.patch(
            VECTOR_IMAGE, [graphics.vector_record(kind, id=stroke, **fields)], q=2))

    def append(self, stroke, point):
        """Append a sample, including the final pen-up sample."""
        self.edit('a', stroke, pts=point)

    def finish(self, stroke, cancel=False):
        """Keep completed strokes and delete cancelled ones."""
        if cancel:
            self.edit('x', stroke)

    def close(self):
        """Release the canvas and its placement."""
        self.term.graphics.send(graphics.delete(VECTOR_IMAGE, q=2))


def coverage(point, start, end):
    """Return antialiased coverage of a linearly varying circular brush sweep."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    radius_delta = (end[2] - start[2]) / 2
    if length <= abs(radius_delta):
        fraction = float(end[2] > start[2])
    else:
        px, py = point[0] - start[0], point[1] - start[1]
        along = (px * dx + py * dy) / length
        across = abs(px * dy - py * dx) / length
        fraction = (along + radius_delta * across / math.sqrt(
            length * length - radius_delta * radius_delta)) / length
        fraction = min(1.0, max(0.0, fraction))
    radius = (start[2] + fraction * (end[2] - start[2])) / 2
    distance = math.hypot(point[0] - start[0] - fraction * dx,
                          point[1] - start[1] - fraction * dy)
    return round(255 * min(1.0, max(0.0, radius + 0.5 - distance)))


class Tiles:
    """Rasterize into lazily allocated, one-cell RGBA images using only Python."""

    # Geometry, contact layers, and terminal resource ownership are independent state.
    # pylint: disable=too-many-instance-attributes

    def __init__(self, term, width, height, cell_width, cell_height):
        self.term = term
        self.size = width, height
        self.cell = cell_width, cell_height
        self.columns = math.ceil(width / cell_width)
        if self.columns * math.ceil(height / cell_height) > MAX_ID - INK_IMAGE:
            raise OverflowError('Canvas exceeds the graphics image ID space')
        self.committed = {}
        self.active = {}
        self.ids = itertools.count(1)
        self.images = set()

    def open(self):
        """Tiles are allocated only when painted."""

    def start(self, point):
        """Start an independent contact layer so cancellation preserves other ink."""
        stroke = next(self.ids)
        self.active[stroke] = (point, {})
        self.append(stroke, point)
        return stroke

    def append(self, stroke, point):
        """Rasterize the visible segment and transmit only cells it changes."""
        # Pixel bounds and per-cell coordinates are kept explicit for readability.
        # pylint: disable=too-many-locals
        previous, layers = self.active[stroke]
        radius = max(previous[2], point[2]) / 2 + 0.5
        left = max(0, math.floor(min(previous[0], point[0]) - radius))
        top = max(0, math.floor(min(previous[1], point[1]) - radius))
        right = min(self.size[0], math.ceil(max(previous[0], point[0]) + radius))
        bottom = min(self.size[1], math.ceil(max(previous[1], point[1]) + radius))
        dirty = set()
        for y in range(top, bottom):
            for x in range(left, right):
                alpha = coverage((x + 0.5, y + 0.5), previous, point)
                if not alpha:
                    continue
                tile = x // self.cell[0], y // self.cell[1]
                if tile not in layers:
                    layers[tile] = bytearray(self.cell[0] * self.cell[1])
                offset = (y % self.cell[1]) * self.cell[0] + x % self.cell[0]
                if alpha > layers[tile][offset]:
                    layers[tile][offset] = alpha
                    dirty.add(tile)
        self.active[stroke] = point, layers
        self.flush(dirty)

    def finish(self, stroke, cancel=False):
        """Merge finished ink, or repaint cancelled cells without that contact."""
        _, layers = self.active.pop(stroke)
        if cancel:
            self.flush(layers)
        else:
            for tile, pixels in layers.items():
                if tile not in self.committed:
                    self.committed[tile] = pixels
                else:
                    target = self.committed[tile]
                    for index, alpha in enumerate(pixels):
                        target[index] = max(target[index], alpha)

    def pixels(self, tile):
        """Composite same-color coverage without darkening overlapping segments."""
        alpha = bytearray(self.cell[0] * self.cell[1])
        layers = [self.committed] + [layer for _, layer in self.active.values()]
        for layer in layers:
            if tile in layer:
                for index, value in enumerate(layer[tile]):
                    alpha[index] = max(alpha[index], value)
        pixels = bytearray(len(alpha) * 4)
        for index, value in enumerate(alpha):
            if value:
                pixels[index * 4:index * 4 + 4] = RGB + bytes((value,))
        return pixels

    def flush(self, dirty):
        """Replace each dirty cell at its original placement without scrolling."""
        for column, row in sorted(dirty):
            image_id = INK_IMAGE + 1 + row * self.columns + column
            self.images.add(image_id)
            self.term.graphics.send(itertools.chain(
                [self.term.move_yx(row, column)],
                graphics.image(self.pixels((column, row)), image_id=image_id, format=32,
                               width=self.cell[0], height=self.cell[1], C=1, q=2)))

    def close(self):
        """Free every transmitted tile, including tiles cleared by cancellation."""
        for image_id in sorted(self.images):
            self.term.graphics.send(graphics.delete(image_id, q=2))


class Canvas:
    """Dispatch contacts, leaving strokes marked with ink IDs to Silkie."""

    def __init__(self, renderer, local_ink=False):
        self.renderer = renderer
        self.local_ink = local_ink
        self.contacts = {}

    def handle(self, event):
        """Handle down/move/up/cancel independently for each tool or finger."""
        if event is None or event.operation != 'event' or event.integer('kind') not in (1, 2):
            return
        contact = tuple(event.integer(key) for key in ('kind', 'device', 'contact'))
        phase = event.integer('phase')
        point = None
        if event.position is not None:
            pressure = event.pressure if event.pressure is not None else 1.0
            point = (*event.position, 1 + 7 * pressure)
        if phase == 1:
            if contact in self.contacts:
                self.finish(contact, cancel=True)
            if self.local_ink and event.integer('kind') == 1 and event.integer('ink', 0) > 0:
                self.contacts[contact] = None
            elif point is not None:
                self.contacts[contact] = self.renderer.start(point)
        elif contact in self.contacts:
            stroke = self.contacts[contact]
            if phase in {2, 3} and point is not None and stroke is not None:
                self.renderer.append(stroke, point)
            if phase in {3, 4, 5}:
                self.finish(contact, cancel=phase == 4)

    def finish(self, contact, cancel=False):
        """End a contact, without modifying terminal-managed ink."""
        stroke = self.contacts.pop(contact)
        if stroke is not None:
            self.renderer.finish(stroke, cancel=cancel)


@contextlib.contextmanager
def drawing(term, renderer, mode, caps):
    """
    Own canvas resources and disable terminal ink on every exit path.

    :raises SystemExit: If terminal-side ink is unavailable or refused.
    """
    has_ink = caps.supports('ink')
    try:
        if has_ink:
            term.ink.send('mode', mode=0)
        renderer.open()
        if mode == 'ink':
            ink = term.ink.query() if has_ink else None
            if not ink or not ink.integer('caps', 0) & 1:
                raise SystemExit('This mode needs Silkie terminal-side ink support.')
            term.graphics.send(itertools.chain(
                [term.move_yx(0, 0)],
                graphics.scene([], image_id=INK_IMAGE, width=renderer.size[0],
                               height=renderer.size[1], C=1, q=2)))
            # Separate images give terminal and application strokes independent ID spaces.
            term.ink.send('brush', i=INK_IMAGE, k=1, n=MAX_ID, fill=COLOR, w0=1, w1=8)
            if term.ink.read_mode() != 1:
                raise SystemExit('The terminal did not grant terminal-side ink.')
        yield Canvas(renderer, local_ink=mode == 'ink')
    finally:
        if has_ink:
            term.ink.send('mode', mode=0)
        if mode == 'ink':
            term.graphics.send(graphics.delete(INK_IMAGE, q=2))
        renderer.close()


def main(argv=None):
    """
    Require an explicit drawing mode before opening the terminal.

    :raises SystemExit: If arguments or the terminal's capabilities are insufficient.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('tiles', 'vectors', 'ink'),
                        help='cell-sized raster images, application vectors, or terminal-local ink')
    args = parser.parse_args(argv)
    term = Terminal()
    with term.cbreak():
        caps = term.silkie.capabilities()
        required_format = 32 if args.mode == 'tiles' else 200
        if (not caps or not caps.supports('tablet-pointer')
                or required_format not in caps.formats or 32 not in caps.formats):
            raise SystemExit(
                'This mode needs Silkie tablet input and the requested graphics format.')
        tablet = term.tablet.query()
        width = tablet.integer('w', 0) if tablet else 0
        height = tablet.integer('h', 0) if tablet else 0
        if width <= 0 or height <= 0:
            raise SystemExit('The terminal did not report the canvas pixel dimensions.')
        if args.mode == 'tiles':
            cell_width, cell_height = tablet.integer('cw', 0), tablet.integer('ch', 0)
            if cell_width <= 0 or cell_height <= 0:
                raise SystemExit('Tile mode needs the terminal cell pixel dimensions.')
            renderer = Tiles(term, width, height, cell_width, cell_height)
        else:
            renderer = Vectors(term, width, height)
        with term.fullscreen(), term.hidden_cursor(), term.tablet.enabled(
                TabletMode.TOOLS | TabletMode.TOUCH) as granted:
            if not granted & (TabletMode.TOOLS | TabletMode.TOUCH):
                raise SystemExit('The terminal did not grant pen or touch reporting.')
            if args.mode == 'ink' and not granted & TabletMode.TOOLS:
                raise SystemExit('Terminal-side ink needs pen reporting.')
            with drawing(term, renderer, args.mode, caps) as canvas, pen_pointer(term):
                while True:
                    key = term.inkey()
                    if key == 'q' or key.name == 'KEY_ESCAPE':
                        break
                    canvas.handle(key.tablet)


if __name__ == '__main__':
    main()
