"""Window geometry, cell pixels, and VT/xterm window operations."""
from __future__ import annotations

# std imports
import re
import contextlib
from typing import TYPE_CHECKING, Iterator, Optional, NamedTuple

# local
from .protocols import integer

if TYPE_CHECKING:
    # local
    from .terminal import Terminal


class Size(NamedTuple):
    """Dimensions with named width and height, in the units requested."""

    width: int
    height: int


def command(operation: int, *parameters: Optional[int]) -> str:
    """
    Encode an XTWINOPS operation, retaining omitted parameters and zero values.

    This includes queries, resize/move requests, stacking, fullscreen/maximize, and title-stack
    operations. Support and permission are terminal-dependent.

    :raises ValueError: If an operation or parameter is not a nonnegative integer.
    """
    fields = [str(integer(operation, 'operation'))]
    fields.extend('' if value is None else str(integer(value, 'parameter'))
                  for value in parameters)
    return '\x1b[' + ';'.join(fields) + 't'


def resize(*, columns: Optional[int] = None, rows: Optional[int] = None,
           width: Optional[int] = None, height: Optional[int] = None) -> str:
    """
    Request a text area size in cells or pixels, leaving omitted dimensions unchanged.

    Zero requests the screen's corresponding dimension. Kitty/Silkie reject these standard resize
    requests; their OS windows require opt-in remote control.

    :raises ValueError: If units are mixed, no size is supplied, or values are invalid.
    """
    cells = columns is not None or rows is not None
    pixels = width is not None or height is not None
    if cells == pixels:
        raise ValueError('Specify cells (columns/rows) or pixels (width/height), not both')
    return command(8, rows, columns) if cells else command(4, height, width)


class Window:
    """Terminal window queries and explicit requests, available as ``term.window``."""

    def __init__(self, terminal: Terminal):
        """Bind to the terminal's stream and input-preserving query transport."""
        self.terminal = terminal

    def send(self, operation: int, *parameters: Optional[int]) -> None:
        """Send an XTWINOPS command; it does not imply that the request was granted."""
        self.terminal.send(command(operation, *parameters))

    def _pair(self, operation: int, response: int, timeout: Optional[float],
              *parameters: Optional[int]) -> Optional[Size]:
        pattern = re.compile(r'\x1b\[' + str(response) + r';(\d+);(\d+)t')
        # Query machinery preserves unrelated input and bounds the wait, not the reply size.
        # pylint: disable=protected-access
        match = self.terminal._query_with_boundary(
            command(operation, *parameters), pattern, timeout)
        return Size(int(match.group(2)), int(match.group(1))) if match else None

    def cell_size(self, timeout: Optional[float] = 1.0, *, force: bool = False) -> Optional[Size]:
        """Return physical pixels per cell, or None when the terminal does not answer."""
        height, width = self.terminal.get_cell_height_and_width(timeout, force)
        return Size(width, height) if width > 0 and height > 0 else None

    def size(self, timeout: Optional[float] = 1.0, *, pixels: bool = False,
             outer: bool = False) -> Optional[Size]:
        """
        Query actual text-area cells/pixels or the outer window's pixels.

        :raises ValueError: If outer window dimensions are requested in cells.
        """
        if outer and not pixels:
            raise ValueError('Outer window dimensions are measured in pixels')
        return self._pair(14, 4, timeout, *([2] if outer else [])) if pixels else self._pair(
            18, 8, timeout)

    def screen_size(self, timeout: Optional[float] = 1.0, *, pixels: bool = False
                    ) -> Optional[Size]:
        """Query the available screen dimensions in cells or physical pixels."""
        return self._pair(15, 5, timeout) if pixels else self._pair(19, 9, timeout)

    def position(self, timeout: Optional[float] = 1.0, *, text_area: bool = False
                 ) -> Optional[tuple[int, int]]:
        """Query window or text-area position, returning (x, y) or None."""
        pattern = re.compile(r'\x1b\[3;(-?\d+);(-?\d+)t')
        # pylint: disable=protected-access
        match = self.terminal._query_with_boundary(
            command(13, *([2] if text_area else [])), pattern, timeout)
        return (int(match.group(1)), int(match.group(2))) if match else None

    def state(self, timeout: Optional[float] = 1.0) -> Optional[int]:
        """Query window state: 1 is open, 2 is iconified, None is unanswered."""
        # pylint: disable=protected-access
        match = self.terminal._query_with_boundary(command(11), re.compile(r'\x1b\[([12])t'),
                                                   timeout)
        return int(match.group(1)) if match else None

    def request_size(self, *, columns: Optional[int] = None, rows: Optional[int] = None,
                     width: Optional[int] = None, height: Optional[int] = None) -> None:
        """Send a size preference; query size() or watch resize events for the actual result."""
        self.terminal.send(resize(columns=columns, rows=rows, width=width, height=height))
        self.terminal.invalidate_geometry()

    @contextlib.contextmanager
    def title_saved(self, which: int = 0) -> Iterator[None]:
        """Push and restore both titles (0), icon title (1), or window title (2)."""
        integer(which, 'title selection', 0, 2)
        self.send(22, which)
        try:
            yield
        finally:
            self.send(23, which)
