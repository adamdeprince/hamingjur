"""A Unicode multiline text editor for desktop windows and dialogs."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from wcwidth import iter_graphemes

from ..keyboard import Keystroke
from .layout import Surface
from .widgets import Pointer, TextView, Widget, key_name, typed_text

if TYPE_CHECKING:
    from .desktop import Application

Position = tuple[int, int]


def _advance(start: Position, text: str) -> Position:
    lines = text.split('\n')
    return (start[0], start[1] + len(text)) if len(lines) == 1 else (
        start[0] + len(lines) - 1, len(lines[-1]))


@dataclass(frozen=True)
class _Edit:
    start: Position
    removed: str
    inserted: str
    cursor: Position
    anchor: Position | None


class TextEditor(TextView):
    """Edit, select, search, and scroll a text document with undo and redo."""

    def __init__(self, text: str = '', **options):
        """Create an editor with a fresh history and insertion point."""
        super().__init__(text, **options)
        self.path: Path | None = None
        self._saved = self.text
        self._newline = '\n'
        self._column: int | None = None
        self._measure: Callable[[str], int] = len
        self._last_match = ''

    @property
    def text(self) -> str:
        """Return the document with logical newline separators."""
        return super().text

    @text.setter
    def text(self, text: str) -> None:
        self.lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        self.cursor: Position = (0, 0)
        self.anchor: Position | None = None
        self.top = self.left = 0
        self._undo: list[_Edit] = []
        self._redo: list[_Edit] = []

    @property
    def modified(self) -> bool:
        """Return whether the current contents differ from the saved text."""
        return self.text != self._saved

    @property
    def selected_text(self) -> str:
        """Return the text covered by the current selection."""
        return self._range(*self._selection())

    def _selection(self) -> tuple[Position, Position]:
        other = self.anchor if self.anchor is not None else self.cursor
        return min(other, self.cursor), max(other, self.cursor)

    def _range(self, start: Position, end: Position) -> str:
        row, col = start
        last, stop = end
        if row == last:
            return self.lines[row][col:stop]
        lines = (self.lines[row][col:], *self.lines[row + 1:last], self.lines[last][:stop])
        return '\n'.join(lines)

    def _replace(self, start: Position, end: Position, text: str) -> None:
        row, col = start
        last, stop = end
        replacement = self.lines[row][:col] + text + self.lines[last][stop:]
        self.lines[row:last + 1] = replacement.split('\n')
        self.cursor = _advance(start, text)
        self.anchor = None
        self._column = None

    def replace_selection(self, text: str) -> None:
        """Replace the highlighted range or insert at the cursor."""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        start, end = self._selection()
        self._edit(start, end, text)

    def _edit(self, start: Position, end: Position, text: str) -> None:
        old = self._range(start, end)
        if old == text:
            return
        self._undo.append(_Edit(start, old, text, self.cursor, self.anchor))
        self._redo.clear()
        self._replace(start, end, text)

    def undo(self) -> None:
        """Reverse the last edit and restore its selection."""
        if self._undo:
            change = self._undo.pop()
            self._replace(change.start, _advance(change.start, change.inserted), change.removed)
            self.cursor, self.anchor = change.cursor, change.anchor
            self._redo.append(change)

    def redo(self) -> None:
        """Reapply the last reverted edit."""
        if self._redo:
            change = self._redo.pop()
            self._replace(change.start, _advance(change.start, change.removed), change.inserted)
            self._undo.append(change)

    def load(self, path: str | Path, *, encoding: str = 'utf-8') -> None:
        """Load a document into this editor."""
        path = Path(path)
        with path.open(encoding=encoding, newline='') as source:
            contents = source.read()
        newline = re.search(r'\r\n|\r|\n', contents)
        self._newline = newline.group() if newline else '\n'
        self.text = contents
        self.path, self._saved = path, self.text

    def save(self, path: str | Path | None = None, *, encoding: str = 'utf-8') -> None:
        """Atomically write the document with the line endings detected on load."""
        target = self.path if path is None else Path(path)
        if target is None:
            raise ValueError('Specify a file to save')
        contents = self.text
        destination = target.resolve()
        mode = stat.S_IMODE(destination.stat().st_mode) if destination.is_file() else None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding=encoding, newline='',
                                             dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents.replace('\n', self._newline))
            if mode is not None:
                temporary.chmod(mode)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.path, self._saved = target, contents

    def select_all(self) -> None:
        """Select the entire document."""
        self.anchor = (0, 0)
        self.cursor = len(self.lines) - 1, len(self.lines[-1])

    def _at(self, column: int, row: int) -> Position:
        distance = end = 0
        for grapheme in iter_graphemes(self.lines[row]):
            span = self._columns(grapheme, distance)
            if 2 * (column - distance) < span:
                break
            distance += span
            end += len(grapheme)
        return row, end

    def _columns(self, text: str, start: int = 0) -> int:
        """Measure a line prefix with eight-cell tab stops."""
        width = start
        for index, part in enumerate(text.split('\t')):
            if index:
                width += 8 - width % 8
            width += self._measure(part)
        return width - start

    def _horizontal(self, position: Position, step: int) -> Position:
        row, col = position
        line = self.lines[row]
        if step < 0 and col == 0:
            return (row - 1, len(self.lines[row - 1])) if row else position
        if step > 0 and col == len(line):
            return (row + 1, 0) if row + 1 < len(self.lines) else position
        start = 0
        for word in iter_graphemes(line):
            end = start + len(word)
            if (step < 0 and col <= end) or (step > 0 and col < end):
                return row, start if step < 0 else end
            start = end
        return position

    def _navigate(self, name: str, selecting: bool) -> bool:
        row, col = self.cursor
        start = self.cursor
        page = max(1, self.bounds.height)
        movements = {'UP': -1, 'DOWN': 1, 'PGUP': -page, 'PGDOWN': page}
        bounds = {'HOME': (row, 0), 'END': (row, len(self.lines[row])),
                  'CTRL_HOME': (0, 0), 'CTRL_END': (len(self.lines) - 1, len(self.lines[-1]))}
        if name in movements:
            if self._column is None:
                self._column = self._columns(self.lines[row][:col])
            row = max(0, min(len(self.lines) - 1, row + movements[name]))
            self.cursor = self._at(self._column, row)
        elif name in bounds or name in {'LEFT', 'RIGHT'}:
            self.cursor = bounds[name] if name in bounds else self._horizontal(
                self.cursor, -1 if name == 'LEFT' else 1)
            self._column = None
        else:
            return False
        self.anchor = (start if self.anchor is None else self.anchor) if selecting else None
        return True

    def find(self, phrase: str) -> bool:
        """Select the next occurrence, wrapping at the end of the document."""
        self._last_match = phrase
        if not phrase:
            return False
        start, end = self._selection()
        row, col = end if start != end else self.cursor
        index = sum(len(line) + 1 for line in self.lines[:row]) + col
        text = self.text
        index = text.find(phrase, index)
        if index < 0:
            index = text.find(phrase)
        if index < 0:
            return False
        prefix = text[:index]
        self.anchor = (prefix.count('\n'), len(prefix.rsplit('\n', 1)[-1]))
        self.cursor = _advance(self.anchor, phrase)
        return True

    def replace_all(self, phrase: str, replacement: str) -> int:
        """Replace occurrences in one undoable operation and return the count."""
        if not phrase:
            return 0
        contents = self.text
        count = contents.count(phrase)
        if count:
            self._edit((0, 0), (len(self.lines) - 1, len(self.lines[-1])),
                       contents.replace(phrase, replacement))
        return count

    def _visible_cursor(self, surface: Surface) -> tuple[int, int]:
        row, col = self.cursor
        column = self._columns(self.lines[row][:col])
        self.top = max(0, min(self.top, row))
        self.top = max(self.top, row - max(1, surface.height) + 1)
        self.left = max(0, min(self.left, column))
        self.left = max(self.left, column - max(1, surface.width) + 1)
        return column - self.left, row - self.top

    def caret_position(self) -> tuple[int, int]:
        """Return the visible editor caret for terminal magnification."""
        row, col = self.cursor
        return (self.bounds.x + self._columns(self.lines[row][:col]) - self.left,
                self.bounds.y + row - self.top)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw the viewport, selection and insertion point."""
        self._measure = surface.measure
        cell = self._visible_cursor(surface) if focused is self else None
        surface.fill('window')
        start, end = self._selection()
        for visible, line in enumerate(self.lines[self.top:self.top + surface.height]):
            row = visible + self.top
            if row < start[0] or row > end[0] or start == end:
                self._draw_line(surface, visible, line, 0, 'text')
            else:
                first = start[1] if row == start[0] else 0
                last = end[1] if row == end[0] else len(line)
                before = self._draw_line(surface, visible, line[:first], 0, 'text')
                middle = self._draw_line(surface, visible, line[first:last], before, 'selection')
                self._draw_line(surface, visible, line[last:], middle, 'text')
                if row < end[0]:
                    surface.write(middle - self.left, visible, ' ', 'selection')
        if cell is not None:
            surface.highlight(*cell, 'focus')

    def _draw_line(self, surface: Surface, row: int, text: str, start: int, role: str) -> int:
        column = start
        for index, part in enumerate(text.split('\t')):
            if column - self.left >= surface.width:
                break
            if index:
                amount = 8 - column % 8
                surface.write(column - self.left, row, ' ' * amount, role)
                column += amount
            surface.write(column - self.left, row, part, role)
            column += self._measure(part)
        return column

    def _clipboard(self, name: str, app: Application) -> bool:
        if name in {'CTRL_C', 'CTRL_X'}:
            text = self.selected_text
            if text:
                app.clipboard = text
                if name == 'CTRL_X':
                    self.replace_selection('')
        elif name == 'CTRL_V':
            if app.clipboard:
                self.replace_selection(app.clipboard)
        else:
            return False
        return True

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Handle movement, editing, selection, clipboard, undo and repeated search."""
        name = key_name(key)
        selecting = 'SHIFT_' in name
        if self._navigate(name.replace('SHIFT_', ''), selecting):
            return True
        if self._clipboard(name, app):
            return True
        if name in {'BACKSPACE', 'DELETE'}:
            start, end = self._selection()
            if start == end:
                step = -1 if name == 'BACKSPACE' else 1
                other = self._horizontal(start, step)
                start, end = min(start, other), max(end, other)
            self._edit(start, end, '')
        elif name in {'ENTER', 'BRACKETED_PASTE'}:
            text = '\n' if name == 'ENTER' else getattr(key, 'text', None) or ''
            clean = ''.join(char for char in text if char in '\r\n\t' or (
                ord(char) >= 32 and not 127 <= ord(char) <= 159))
            self.replace_selection(clean)
        elif name == 'CTRL_A':
            self.select_all()
        elif name == 'CTRL_Z':
            self.undo()
        elif name == 'CTRL_Y':
            self.redo()
        elif name == 'F3':
            if not self.find(self._last_match):
                app.set_status('Text not found', error=True)
        else:
            text = typed_text(key)
            if not text:
                return False
            self.replace_selection(text)
        return True

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Scroll or use a pointer to position and select text."""
        if pointer.kind == 'wheel':
            self.scroll(-3 if pointer.button == 'UP' else 3)
            target = min(self.top + max(1, self.bounds.height) - 1, max(self.top, self.cursor[0]))
            row, col = self.cursor
            self.cursor = self._at(self._columns(self.lines[row][:col]), target)
            self.anchor = None
            return True
        if pointer.button == 'LEFT' and pointer.kind in {'press', 'double', 'move'}:
            row = max(0, min(len(self.lines) - 1, self.top + pointer.y - self.bounds.y))
            position = self._at(self.left + pointer.x - self.bounds.x, row)
            if pointer.kind == 'move':
                self.anchor = self.cursor if self.anchor is None else self.anchor
            else:
                self.anchor = None
            self.cursor = position
            self._column = None
            return True
        return False
