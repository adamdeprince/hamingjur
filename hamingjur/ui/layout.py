"""Cell geometry, semantic styles and clipped terminal drawing."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from itertools import groupby
from typing import TYPE_CHECKING, Any, Callable, Mapping

from wcwidth import iter_graphemes, width

from ..color import rgb_to_xyz
from ..colorspace import hex_to_rgb, rgb_to_hex

if TYPE_CHECKING:
    from ..terminal import Terminal
    from .images import ImageView


@dataclass(frozen=True)
class Rect:
    """A zero-based rectangle in terminal cells."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """Return the first column beyond the rectangle."""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """Return the first row beyond the rectangle."""
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        """Test whether a cell is inside the rectangle."""
        return self.x <= x < self.right and self.y <= y < self.bottom

    def intersection(self, other: Rect) -> Rect:
        """Return the area common to both rectangles."""
        x, y = max(self.x, other.x), max(self.y, other.y)
        return Rect(x, y, max(0, min(self.right, other.right) - x),
                    max(0, min(self.bottom, other.bottom) - y))

    def inset(self, columns: int = 1, rows: int = 1) -> Rect:
        """Remove padding from both sides of each axis."""
        return Rect(self.x + columns, self.y + rows, max(0, self.width - 2 * columns),
                    max(0, self.height - 2 * rows))

    def fit(self, area: Rect, minimum: tuple[int, int] = (1, 1)) -> Rect:
        """Clamp size and position to the available area."""
        cols = min(area.width, max(minimum[0], self.width))
        rows = min(area.height, max(minimum[1], self.height))
        return Rect(max(area.x, min(area.right - cols, self.x)),
                    max(area.y, min(area.bottom - rows, self.y)), cols, rows)


@dataclass(frozen=True)
class Style:
    """Foreground and background hex colors, with optional text emphasis."""

    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    reverse: bool = False

    def __post_init__(self) -> None:
        """Validate six-digit RGB strings at configuration time."""
        for value in (self.fg, self.bg):
            if value is not None and not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                raise ValueError(f'Expected a color in #RRGGBB form, got {value!r}')


_DARK = {
    'desktop': Style('#64748B', '#101720'),
    'window': Style('#E1EAF1', '#1C2733'),
    'border': Style('#7D93A7', '#1C2733'),
    'title': Style('#8EF0D2', '#1C2733', bold=True),
    'bar': Style('#E1EAF1', '#26374A', bold=True),
    'menu': Style('#E1EAF1', '#26374A'),
    'text': Style('#E1EAF1', '#1C2733'),
    'muted': Style('#A9B9C8', '#1C2733'),
    'control': Style('#E1EAF1', '#304257'),
    'focus': Style('#111D23', '#8EF0D2', bold=True),
    'selection': Style('#E9FBF7', '#31695F'),
    'disabled': Style('#A0ADBB', '#1C2733'),
    'accent': Style('#8EF0D2', '#1C2733', bold=True),
    'error': Style('#FFC4BD', '#552F36', bold=True),
    'shadow': Style('#101720', '#0B1016'),
}

_LIGHT = {
    'desktop': Style('#596D79', '#DCE6EC'),
    'window': Style('#1A2935', '#F8FAFC'),
    'border': Style('#547084', '#F8FAFC'),
    'title': Style('#155B52', '#F8FAFC', bold=True),
    'bar': Style('#1A2935', '#C7DBE6', bold=True),
    'menu': Style('#1A2935', '#E0EBF0'),
    'text': Style('#1A2935', '#F8FAFC'),
    'muted': Style('#4B6070', '#F8FAFC'),
    'control': Style('#172D3C', '#DCEAF0'),
    'focus': Style('#FFFFFF', '#226256', bold=True),
    'selection': Style('#12362F', '#BFDFD5'),
    'disabled': Style('#627786', '#F8FAFC'),
    'accent': Style('#155B52', '#F8FAFC', bold=True),
    'error': Style('#642220', '#FFE0DA', bold=True),
    'shadow': Style('#8397A2', '#B4C4CD'),
}


class Theme:
    """Semantic color roles. Override any role while inheriting the others."""

    def __init__(self, styles: Mapping[str, Style] | None = None, *, light: bool = False):
        """Create a palette based on the dark or light defaults."""
        self._styles = dict(_LIGHT if light else _DARK)
        if styles:
            unknown = styles.keys() - self._styles.keys()
            if unknown:
                raise ValueError(f'Unknown theme roles: {sorted(unknown)}')
            self._styles.update(styles)

    def __getitem__(self, role: str) -> Style:
        """Get a role's style."""
        return self._styles[role]

    @property
    def roles(self) -> tuple[str, ...]:
        """Return the names of the configurable roles."""
        return tuple(self._styles)

    def with_styles(self, **styles: Style) -> Theme:
        """Return this theme with the supplied role replacements."""
        return Theme({**self._styles, **styles})

    @classmethod
    def from_mapping(cls, config: Mapping[str, Mapping[str, Any]], *,
                     light: bool = False) -> Theme:
        """Apply partial styles from a JSON-compatible mapping keyed by role."""
        theme = cls(light=light)
        return theme.with_styles(**{role: replace(theme[role], **fields)
                                    for role, fields in config.items()})

    @classmethod
    def high_contrast(cls, *, light: bool = False) -> Theme:
        """Create a high-contrast palette with a dark or light background."""
        base = cls()
        fg, bg = ('#000000', '#FFFFFF') if light else ('#FFFFFF', '#000000')
        roles = {role: Style(fg, bg, bold=base[role].bold) for role in base.roles}
        roles['focus'] = Style(bg, fg, bold=True)
        if light:
            roles.update(selection=Style(bg, '#005E4B'),
                         accent=Style('#004D3C', bg, bold=True),
                         bar=Style(fg, '#DDDDDD', bold=True),
                         menu=Style(fg, '#DDDDDD'),
                         control=Style(fg, '#EEEEEE'),
                         error=Style('#7F160B', '#FFD5CB', bold=True))
        else:
            roles.update(selection=Style(bg, '#66FFDC'),
                         accent=Style('#66FFDC', bg, bold=True),
                         bar=Style(fg, '#333333', bold=True),
                         menu=Style(fg, '#333333'),
                         control=Style(fg, '#292929'),
                         error=Style(bg, '#FFD5CB', bold=True))
        return cls(roles)

    def grayscale(self) -> Theme:
        """Return a palette with the same luminance and emphasis for every role."""
        return self.with_styles(**{
            role: replace(style, fg=_gray(style.fg), bg=_gray(style.bg))
            for role, style in self._styles.items()
        })


def _gray(color: str | None) -> str | None:
    if color is None:
        return None
    _, luminance, _ = rgb_to_xyz(*hex_to_rgb(color))
    linear = luminance / 100
    srgb = 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055
    channel = round(srgb * 255)
    return rgb_to_hex(channel, channel, channel).upper()


@dataclass(frozen=True)
class Appearance:
    """Terminal drawing characters and visual treatment, independent of color."""

    frame: tuple[str, str, str, str, str, str]
    close: str
    grip: str
    arrow: str
    check: str
    track: str
    thumb: str
    shadow: bool = False
    ascii_only: bool = False

    @classmethod
    def for_terminal(cls, term: Terminal) -> Appearance:
        """Choose rounded single-cell Unicode frames and enable shadows on Kitty and Silkie."""
        kind = term.kind.lower()
        program = term.term_program.lower()
        if kind.split('-', 1)[0] in {'vt100', 'vt101', 'vt102', 'vt125', 'dumb', 'unknown'}:
            return ASCII
        if term.ambiguous_width != 1:
            return replace(ASCII, ascii_only=False)
        if any(name in kind or name in program for name in ('kitty', 'silkie')):
            return ROUNDED
        return replace(ROUNDED, shadow=False)


ASCII = Appearance(('+', '+', '+', '+', '-', '|'), 'x', '+', '>', 'x', '.', '#',
                   ascii_only=True)
UNICODE = Appearance(('┌', '┐', '└', '┘', '─', '│'), '×', '◢', '›', '✓', '░', '█')
ROUNDED = Appearance(('╭', '╮', '╰', '╯', '─', '│'), '×', '◢', '›', '✓', '░', '█',
                     shadow=True)


def plain(text: str) -> str:
    """Remove characters that can execute terminal controls."""
    return ''.join(char for char in text if ord(char) >= 32 and not 127 <= ord(char) <= 159)


# A continuation cell holds an empty string and the absolute start column of its grapheme.
Cell = tuple[str, str, int]
Row = list[Cell]


class GraphicLayer:
    """A retained graphics producer composited with ordinary cell drawing."""

    def present(self, canvas: Canvas) -> None:
        """Present placements after all windows have drawn."""
        raise NotImplementedError

    def graphics_error(self, image_id: int, reason: str) -> None:
        """Handle a terminal error addressed to an image this producer owns."""

    def release(self) -> None:
        """Release this producer's terminal resources."""
        raise NotImplementedError


class Canvas:
    """One screen of cells, composited back to front and presented by changed row."""

    def __init__(self, size: Rect, appearance: Appearance, term: Terminal, *,
                 translate: Callable[[str], str] = str, accessibility: bool = False):
        """Allocate cells to match the current physical terminal dimensions."""
        self.size = size
        self.appearance = appearance
        self.term = term
        self.translate = translate
        self.accessible_cells: dict[tuple[int, int], str] | None = {} if accessibility else None
        self.rows: list[Row] = [
            [(' ', 'desktop', x) for x in range(size.width)] for _ in range(size.height)]
        self.layers: dict[GraphicLayer, None] = {}
        self.stamp = 0
        self.cover: dict[tuple[int, int], int] = {}
        self.images: dict[ImageView, Rect] = {}
        self._image_cells: dict[tuple[int, int], ImageView] = {}

    def text(self, text: str) -> str:
        """Prepare safe terminal text using the selected character set."""
        text = plain(self.translate(text))
        if self.appearance.ascii_only:
            text = unicodedata.normalize('NFKD', text)
            text = ''.join(char for char in text if not unicodedata.combining(char))
            return text.encode('ascii', errors='replace').decode('ascii')
        return text

    def width(self, text: str) -> int:
        """Measure columns with Hamingjur's configured terminal width rules."""
        return width(text, ambiguous_width=self.term.ambiguous_width, term_program=(
            self.term.term_program or False))

    def put(self, x: int, y: int, text: str, span: int, role: str,
            accessibility_role: str | None = None) -> None:
        """Replace a span, clearing complete graphemes when layers overlap."""
        row = self.rows[y]
        end = x + span
        accessible = self.accessible_cells
        for start in {row[col][2] for col in range(x, end)}:
            col = start
            while col < len(row) and row[col][2] == start:
                if self.stamp:
                    self.cover[col, y] = self.stamp
                if accessible:
                    accessible.pop((y, col), None)
                if self._image_cells:
                    owner = self._image_cells.pop((col, y), None)
                    if owner is not None:
                        self.images.pop(owner, None)
                row[col] = (' ', row[col][1], col)
                col += 1
        row[x] = (text, role, x)
        for col in range(x + 1, end):
            row[col] = ('', role, x)
        if accessible is not None and accessibility_role is not None:
            for col in range(x, end):
                accessible[y, col] = accessibility_role

    def surface(self, bounds: Rect) -> Surface:
        """Create a drawing surface clipped to the visible screen."""
        return Surface(self, bounds, bounds.intersection(self.size))

    def present(self, theme: Theme, previous: list[Row] | None, *,
                cursor: tuple[int, int] | None = None,
                previous_cursor: tuple[int, int] | None = None) -> None:
        """Write changed rows with the terminal's native color capabilities."""
        term = self.term
        colors = not self.appearance.ascii_only and term.number_of_colors > 0
        prefixes = {}
        for role in theme.roles:
            style = theme[role]
            prefix = term.normal
            if colors and style.fg:
                prefix += term.color_hex(style.fg)
            if colors and style.bg:
                prefix += term.on_color_rgb(*bytes.fromhex(style.bg[1:]))
            if style.bold:
                prefix += term.bold
            if style.reverse or (not colors and role in {'bar', 'focus', 'selection', 'error'}):
                prefix += term.reverse
            prefixes[role] = str(prefix)
        output = []
        for y, row in enumerate(self.rows):
            if previous is not None and y < len(previous) and previous[y] == row:
                continue
            output.append(term.move_xy(0, y))
            for role, cells in groupby(row, key=lambda cell: cell[1]):
                output.extend((prefixes[role], ''.join(cell[0] for cell in cells)))
        if output:
            output.append(term.normal)
        if cursor is not None and (output or cursor != previous_cursor):
            output.append(term.move_xy(*cursor))
        if output:
            term.send(output)


class Surface:
    """A clipped area with local, zero-based drawing coordinates."""

    def __init__(self, canvas: Canvas, bounds: Rect, clip: Rect):
        """Bind local coordinates to the compositor."""
        self._canvas = canvas
        self.bounds = bounds
        self._clip = clip

    @property
    def appearance(self) -> Appearance:
        """Return the character and chrome choices for this drawing surface."""
        return self._canvas.appearance

    @property
    def width_options(self) -> tuple[int, str | bool]:
        """Return ambiguous-width and terminal settings for external text measurements."""
        term = self._canvas.term
        return term.ambiguous_width, term.term_program or False

    @property
    def width(self) -> int:
        """Return the surface width in cells."""
        return self.bounds.width

    @property
    def height(self) -> int:
        """Return the surface height in cells."""
        return self.bounds.height

    def measure(self, text: str) -> int:
        """Return the displayed width of plain text."""
        return self._canvas.width(self._canvas.text(text))

    def translate(self, text: str) -> str:
        """Resolve a localized message before combining it with other text."""
        return self._canvas.translate(text)

    @property
    def clip(self) -> Rect:
        """Absolute visible rectangle before later widgets draw."""
        return self._clip

    @property
    def terminal(self) -> Terminal:
        """Terminal that owns the current surface and its graphics resources."""
        return self._canvas.term

    def graphic(self, layer: GraphicLayer) -> int:
        """Mark a placement in paint order. Later cell writes can occlude it."""
        self._canvas.layers[layer] = None
        self._canvas.stamp += 1
        return self._canvas.stamp

    def _claim_image(self, image: ImageView, bounds: Rect) -> None:
        """Register an image rectangle only when it fits entirely inside the clip."""
        if not bounds.width or not bounds.height or bounds.intersection(self._clip) != bounds:
            return
        self._canvas.images[image] = bounds
        for y in range(bounds.y, bounds.bottom):
            for x in range(bounds.x, bounds.right):
                self._canvas._image_cells[x, y] = image

    def sub(self, bounds: Rect) -> Surface:
        """Restrict drawing to the supplied absolute rectangle."""
        return Surface(self._canvas, bounds, self._clip.intersection(bounds))

    def write(self, x: int, y: int, text: str, role: str = 'text', *,
              end: int | None = None, accessibility_role: str | None = None) -> None:
        """Write text clipped at grapheme boundaries, with an optional screen-reader role."""
        x += self.bounds.x
        y += self.bounds.y
        if not self._clip.y <= y < self._clip.bottom:
            return
        right = self._clip.right if end is None else min(self._clip.right, self.bounds.x + end)
        for grapheme in iter_graphemes(self._canvas.text(text)):
            span = self._canvas.width(grapheme)
            if x + span > right:
                break
            if span > 0 and x >= self._clip.x:
                self._canvas.put(x, y, grapheme, span, role, accessibility_role)
            x += max(span, 0)

    def fill(self, role: str = 'window') -> None:
        """Paint the clipped surface with a background role."""
        line = ' ' * self._clip.width
        for y in range(self._clip.y, self._clip.bottom):
            self.write(self._clip.x - self.bounds.x, y - self.bounds.y, line, role)

    def highlight(self, x: int, y: int, role: str) -> None:
        """Change the style of one cell or the complete grapheme covering it."""
        x += self.bounds.x
        y += self.bounds.y
        if not self._clip.contains(x, y):
            return
        row = self._canvas.rows[y]
        start = row[x][2]
        for col in range(start, self._clip.right):
            text, _, origin = row[col]
            if origin != start:
                break
            row[col] = (text, role, origin)
