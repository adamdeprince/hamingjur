"""Keep Silkie's native text annotations in sync with the visible desktop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from ..silkie import annotate_text, announce, clear_text_annotation

if TYPE_CHECKING:
    from ..terminal import Terminal
    from .layout import Canvas, Row


@dataclass(frozen=True)
class _Span:
    row: int
    column: int
    end: int
    role: str


def _spans(cells: dict[tuple[int, int], str]) -> Iterator[_Span]:
    span = None
    for row, column in sorted(cells):
        role = cells[row, column]
        if span is not None and (span.row, span.end, span.role) == (row, column, role):
            span = _Span(row, span.column, column + 1, role)
        else:
            if span is not None:
                yield span
            span = _Span(row, column, column + 1, role)
    if span is not None:
        yield span


class ScreenReader:
    """Publish screen text roles and surface errors through native accessibility."""

    def __init__(self, terminal: Terminal):
        """Keep the pane's currently installed ranges."""
        self.terminal = terminal
        self.enabled = False
        self._ranges: dict[int, _Span] = {}
        self._last_error: str | None = None

    def sync(self, canvas: Canvas, previous: list[Row] | None,
             error: str | None) -> None:
        """Publish final, unoccluded spans after the corresponding cells are drawn."""
        cells = canvas.accessible_cells
        if cells is None:
            return
        # PTY rows and columns are unsigned 16-bit values. Their cell indices fit
        # in Silkie's 32-bit ID space. Count down so ordinary app IDs can start at one.
        current = {0xffffffff - span.row * canvas.size.width - span.column: span
                   for span in _spans(cells)}
        changed_rows = {y for y, row in enumerate(canvas.rows)
                        if previous is None or y >= len(previous) or previous[y] != row}
        self.terminal.send(self._updates(current, changed_rows))
        self._ranges = current
        if error != self._last_error:
            self._last_error = error
            message = canvas.text(error) if error is not None else ''
            if message.strip():
                self.terminal.send(announce(message))

    def _updates(self, current: dict[int, _Span], changed_rows: set[int]) -> Iterator[str]:
        for key in self._ranges.keys() - current.keys():
            yield clear_text_annotation(key)
        for key, span in current.items():
            if self._ranges.get(key) != span or span.row in changed_rows:
                yield annotate_text(key, (span.row + 1, span.column + 1),
                                    (span.row + 1, span.end + 1), role=span.role)

    def clear(self) -> None:
        """Remove desktop annotations while their screen is still active."""
        self.terminal.send(clear_text_annotation(key) for key in self._ranges)
        self._ranges.clear()
        self._last_error = None
