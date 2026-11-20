"""Reusable controls, containers, list views and scrolling."""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Callable, Generic, Iterable, Iterator, TypeVar

from ..keyboard import Keystroke
from ..line_editor import LineEditor, LineHistory
from .layout import Rect, Surface

if TYPE_CHECKING:
    from .desktop import Application

T = TypeVar('T')
_EMPTY = Rect(0, 0, 0, 0)


def key_name(key: Keystroke | str) -> str:
    """Normalize Hamingjur key names and ASCII fallback chords."""
    name = getattr(key, 'name', None) or ''
    aliases = {'KEY_BTAB': 'SHIFT_TAB', 'KEY_STAB': 'SHIFT_TAB', 'KEY_PPAGE': 'PGUP',
               'KEY_NPAGE': 'PGDOWN', 'KEY_DC': 'DELETE', 'KEY_EXIT': 'ESCAPE',
               'KEY_ESC': 'ESCAPE', 'KEY_RETURN': 'ENTER'}
    if name and name.startswith('KEY_'):
        return aliases.get(name, name[4:])
    if name:
        return name
    if key in {'\t', '\r', '\n', '\x1b', '\x08', '\x7f'}:
        return {'\t': 'TAB', '\r': 'ENTER', '\n': 'ENTER', '\x1b': 'ESCAPE',
                '\x08': 'BACKSPACE', '\x7f': 'BACKSPACE'}[key]
    if len(key) == 1 and 0 < ord(key) < 27:
        return 'CTRL_' + chr(ord(key) + 64)
    if len(key) == 2 and key[0] == '\x1b' and key[1].isascii() and key[1].isalpha():
        return 'ALT_' + key[1].upper()
    return 'SPACE' if key == ' ' else ''


def typed_text(key: Keystroke | str) -> str:
    """Return printable text, excluding command chords and key releases."""
    if key_name(key) not in {'', 'SPACE'} and not key_name(key).startswith('SHIFT_'):
        return ''
    value = getattr(key, 'value', str(key))
    return value if value.isprintable() else ''


@dataclass(frozen=True)
class Pointer:
    """A mouse event in absolute screen cells."""

    x: int
    y: int
    button: str = 'LEFT'
    kind: str = 'press'


class Widget:
    """Base for themed, focusable views. Override draw and event handlers."""

    focusable = False

    def __init__(self, *, width: int = 1, height: int = 1, grow: int = 0,
                 enabled: bool = True):
        """Set preferred dimensions and the weight used when distributing spare cells."""
        if min(width, height, grow) < 0:
            raise ValueError('Widget dimensions and grow must be nonnegative')
        self.preferred_width = width
        self.preferred_height = height
        self.grow = grow
        self.enabled = enabled
        self.visible = True
        self.bounds = _EMPTY
        self.parent: Widget | None = None

    def layout(self, bounds: Rect) -> None:
        """Receive the rectangle assigned by the parent."""
        self.bounds = bounds

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw onto the supplied clipped area."""

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Handle a key and return whether it was consumed."""
        return False

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Handle a mouse event and return whether it was consumed."""
        return False

    def caret_position(self) -> tuple[int, int] | None:
        """Return the editor caret in screen cells when the widget has one."""
        return None

    def focus_order(self) -> Iterator[Widget]:
        """Yield eligible widgets in keyboard traversal order."""
        if self.visible and self.enabled and self.bounds.width and self.bounds.height:
            if self.focusable:
                yield self

    def hit(self, x: int, y: int) -> Widget | None:
        """Return the enabled view at a cell."""
        if self.enabled and self.visible and self.bounds.contains(x, y):
            return self
        return None

    def reveal(self) -> None:
        """Bring this widget into view inside any scrolling ancestors."""
        parent = self.parent
        while parent:
            parent.reveal_child(self)
            parent = parent.parent

    def reveal_child(self, child: Widget) -> None:
        """Adjust layout to reveal a descendant, where supported."""


class Group(Widget):
    """A container with child ownership and clipped child drawing."""

    def __init__(self, *children: Widget, **options):
        """Attach children in drawing and tab order."""
        super().__init__(**options)
        self.children: list[Widget] = []
        for child in children:
            self.add(child)

    def add(self, child: Widget) -> Widget:
        """Attach a widget, which can have only one parent."""
        ancestor: Widget | None = self
        while ancestor is not None:
            if ancestor is child:
                raise ValueError('Widgets cannot contain themselves')
            ancestor = ancestor.parent
        if child.parent is not None:
            raise ValueError('Widget already has a parent')
        child.parent = self
        self.children.append(child)
        return child

    def remove(self, child: Widget) -> None:
        """Detach a child from this group."""
        self.children.remove(child)
        child.parent = None
        child.layout(_EMPTY)

    def layout(self, bounds: Rect) -> None:
        """Stack children at their preferred height unless specialized."""
        self.bounds = bounds
        self._layout_stack(horizontal=False, gap=0)

    def _layout_stack(self, *, horizontal: bool, gap: int) -> None:
        area = self.bounds
        children = [child for child in self.children if child.visible]
        total = area.width if horizontal else area.height
        available = max(0, total - gap * max(0, len(children) - 1))
        sizes = [child.preferred_width if horizontal else child.preferred_height
                 for child in children]
        # Reserve one usable cell per visible control before satisfying its preferred size.
        reserved = [int(size > 0 or child.grow > 0) for size, child in zip(sizes, children)]
        if sum(reserved) <= available:
            remaining = available - sum(reserved)
            for index, size in enumerate(sizes):
                addition = min(remaining, max(0, size - reserved[index]))
                reserved[index] += addition
                remaining -= addition
        else:
            remaining = 0
        weights = sum(child.grow for child in children)
        cursor = 0
        weight = 0
        for child, size in zip(children, reserved):
            if weights:
                share = (weight + child.grow) * remaining // weights - weight * remaining // weights
                weight += child.grow
                size += share
            size = min(size, max(0, total - cursor))
            if horizontal:
                child.layout(Rect(area.x + cursor, area.y, size, area.height))
            else:
                child.layout(Rect(area.x, area.y + cursor, area.width, size))
            cursor += size + gap

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw visible children from back to front."""
        for child in self.children:
            if child.visible:
                child.draw(surface.sub(child.bounds), focused)

    def focus_order(self) -> Iterator[Widget]:
        """Yield descendants in tab order."""
        if self.visible and self.enabled:
            yield from super().focus_order()
            for child in self.children:
                yield from child.focus_order()

    def hit(self, x: int, y: int) -> Widget | None:
        """Return the frontmost child at a cell."""
        if super().hit(x, y) is not None:
            for child in reversed(self.children):
                target = child.hit(x, y)
                if target is not None:
                    return target
            return self
        return None


class Stack(Group):
    """Lay out controls in a row or column with optional growing slots."""

    def __init__(self, *children: Widget, horizontal: bool = False, gap: int = 0, **options):
        """Set the stack's orientation and spacing."""
        options.setdefault('width', (sum(child.preferred_width for child in children)
                                     + gap * max(0, len(children) - 1)) if horizontal else
                           max((child.preferred_width for child in children), default=0))
        options.setdefault('height', max((child.preferred_height for child in children), default=0)
                           if horizontal else sum(child.preferred_height for child in children)
                           + gap * max(0, len(children) - 1))
        super().__init__(*children, **options)
        if gap < 0:
            raise ValueError('Gap must be nonnegative')
        self.horizontal = horizontal
        self.gap = gap

    def layout(self, bounds: Rect) -> None:
        """Allocate each child along the configured axis."""
        self.bounds = bounds
        self._layout_stack(horizontal=self.horizontal, gap=self.gap)


@dataclass(frozen=True)
class _Placement:
    bounds: Rect
    right: bool
    bottom: bool
    grow_x: bool
    grow_y: bool


class Panel(Group):
    """Place widgets in local cell rectangles with optional resize anchors."""

    def __init__(self, *, width: int, height: int, **options):
        """Set the initial layout dimensions used by anchors."""
        super().__init__(width=width, height=height, **options)
        self._placements: dict[Widget, _Placement] = {}

    def place(self, child: Widget, bounds: Rect, *, right: bool = False, bottom: bool = False,
              grow_x: bool = False, grow_y: bool = False) -> Widget:
        """Attach a child with fixed, end-anchored, or stretching edges."""
        if (right and grow_x) or (bottom and grow_y):
            raise ValueError('Choose an end anchor or stretch on each axis')
        self.add(child)
        self._placements[child] = _Placement(bounds, right, bottom, grow_x, grow_y)
        return child

    def remove(self, child: Widget) -> None:
        """Remove a child and discard its layout options."""
        super().remove(child)
        self._placements.pop(child, None)

    def layout(self, bounds: Rect) -> None:
        """Apply the difference from the reference size and clip children to the panel."""
        self.bounds = bounds
        dx, dy = bounds.width - self.preferred_width, bounds.height - self.preferred_height
        for child in self.children:
            placement = self._placements.get(child)
            if placement is None:
                child.layout(bounds)
                continue
            box = placement.bounds
            desired = Rect(bounds.x + box.x + (dx if placement.right else 0),
                           bounds.y + box.y + (dy if placement.bottom else 0),
                           max(0, box.width + (dx if placement.grow_x else 0)),
                           max(0, box.height + (dy if placement.grow_y else 0)))
            child.layout(desired.intersection(bounds))


class Label(Widget):
    """Static text, with a semantic style role."""

    def __init__(self, text: str, *, role: str = 'text', **options):
        """Set the text and optional style role."""
        options.setdefault('height', len(text.split('\n')))
        super().__init__(**options)
        self.text = text
        self.role = role

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw lines within the allocated area."""
        for y, line in enumerate(self.text.split('\n', surface.height)[:surface.height]):
            surface.write(0, y, line, self.role)


class Button(Widget):
    """A push button activated by Return, Space, or a mouse click."""

    focusable = True

    def __init__(self, label: str, action: Callable[[], None], **options):
        """Bind a label to an action."""
        options.setdefault('width', len(label) + 4)
        super().__init__(**options)
        self.label = label
        self.action = action

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw the button with visible keyboard focus."""
        role = _role(self, focused)
        surface.fill(role)
        surface.write(0, 0, f'[ {self.label} ]', role)

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Activate the button from the keyboard."""
        if key_name(key) in {'ENTER', 'SPACE'}:
            self.action()
            return True
        return False

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Activate on a left press."""
        if pointer.button == 'LEFT' and pointer.kind == 'press':
            self.action()
            return True
        return False


def _role(view: Widget, focused: Widget | None) -> str:
    if not view.enabled:
        return 'disabled'
    return 'focus' if focused is view else 'control'


class Checkbox(Button):
    """An independent Boolean option."""

    def __init__(self, label: str, checked: bool = False, *,
                 on_change: Callable[[bool], None] | None = None, **options):
        """Set the initial check state and change callback."""
        super().__init__(label, self.toggle, **options)
        self.checked = checked
        self.on_change = on_change

    def toggle(self) -> None:
        """Flip the check state and notify the application."""
        self.checked = not self.checked
        if self.on_change is not None:
            self.on_change(self.checked)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw the marker and label."""
        role = _role(self, focused)
        mark = surface.appearance.check if self.checked else ' '
        surface.write(0, 0, f'[{mark}] {self.label}', role)


class InputLine(Widget):
    """Single-line editable input backed by Hamingjur's LineEditor."""

    focusable = True

    def __init__(self, value: str = '', *, history: LineHistory | None = None,
                 password: bool = False, validator: Callable[[str], str | None] | None = None,
                 on_submit: Callable[[str], None] | None = None, **options):
        """Set the initial value, optional validator, and submit action."""
        super().__init__(**options)
        self.editor = LineEditor(history=history, password=password)
        self.value = value
        self.validator = validator
        self.on_submit = on_submit

    @property
    def value(self) -> str:
        """Return the current input text."""
        return self.editor.line

    @value.setter
    def value(self, text: str) -> None:
        self.editor.clear()
        self.editor.insert_text(text)

    def validate(self) -> str | None:
        """Return a validation message or None."""
        return self.validator(self.value) if self.validator else None

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw the editor's Unicode-aware viewport and cell cursor."""
        self.editor.max_width = max(1, surface.width)
        self.editor.ambiguous_width, self.editor.term_program = surface.width_options
        self.editor.password_char = '*' if surface.appearance.ascii_only else '✻'
        self.editor.ellipsis = '>' if surface.appearance.ascii_only else '…'
        display = self.editor.display
        role = _role(self, focused)
        surface.fill(role)
        surface.write(0, 0, display.text, role)
        if display.suggestion:
            surface.write(surface.measure(display.text), 0, display.suggestion, 'muted')
        if focused is self:
            surface.highlight(min(surface.width - 1, display.cursor), 0, 'selection')

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Edit, paste, recall history, or submit the field."""
        name = key_name(key)
        if name == 'ENTER':
            if self.on_submit is None:
                return False
            error = self.validate()
            if error:
                app.set_status(error, error=True)
            else:
                if not self.editor.password_mode:
                    self.editor.history.add(self.value)
                self.on_submit(self.value)
            return True
        if name == 'BRACKETED_PASTE':
            self.editor.insert_text(getattr(key, 'text', None) or '')
            return True
        if name == 'CTRL_R' and not self.editor.password_mode:
            app.choose_history(self)
            return True
        if name == 'CTRL_V':
            self.editor.insert_text(app.clipboard)
            return True
        if name in {'CTRL_C', 'CTRL_X'} and not self.editor.password_mode:
            app.clipboard = self.value
            if name == 'CTRL_X':
                self.value = ''
            return True
        text = typed_text(key)
        if text:
            self.editor.insert_text(text)
            return True
        if 'KEY_' + name in self.editor.keymap and name not in {'CTRL_C', 'CTRL_D'}:
            self.editor.feed_key(Keystroke(str(key), name='KEY_' + name))
            return True
        return False

    def caret_position(self) -> tuple[int, int]:
        """Return the visible input caret for terminal magnification."""
        column = min(max(0, self.bounds.width - 1), self.editor.display.cursor)
        return self.bounds.x + column, self.bounds.y

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Move the insertion point to a clicked column."""
        if pointer.button != 'LEFT' or pointer.kind != 'press':
            return False
        display = self.editor.display
        delta = pointer.x - self.bounds.x - display.cursor
        self.editor.move_to_column(self.editor.cursor_column + delta)
        return True


class ScrollBar(Widget):
    """A vertical or horizontal range control with keyboard and mouse input."""

    focusable = True

    def __init__(self, total: int = 1, page: int = 1, value: int = 0, *,
                 horizontal: bool = False, on_change: Callable[[int], None] | None = None,
                 **options):
        """Bind the scrollbar to a count, viewport size, and offset."""
        super().__init__(**options)
        self.horizontal = horizontal
        self.on_change = on_change
        self.set_range(total, page, value)

    def set_range(self, total: int, page: int, value: int = 0) -> None:
        """Synchronize range and thumb position without calling the change handler."""
        self.total = max(0, total)
        self.page = max(1, page)
        self.value = min(self.maximum, max(0, value))

    @property
    def maximum(self) -> int:
        """Return the last valid offset."""
        return max(0, self.total - self.page)

    def _set_value(self, value: int) -> None:
        value = min(self.maximum, max(0, value))
        if value != self.value:
            self.value = value
            if self.on_change:
                self.on_change(value)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw a thumb sized to the visible fraction."""
        size = surface.width if self.horizontal else surface.height
        if not size:
            return
        length = min(size, max(1, size * self.page // max(1, self.total)))
        start = (size - length) * self.value // max(1, self.maximum)
        for offset in range(size):
            active = start <= offset < start + length
            text = surface.appearance.thumb if active else surface.appearance.track
            surface.write(offset if self.horizontal else 0, 0 if self.horizontal else offset,
                          text, 'focus' if focused is self and active else 'muted')

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Scroll by a cell, a page, or to either edge."""
        actions = {'LEFT': -1, 'UP': -1, 'RIGHT': 1, 'DOWN': 1, 'PGUP': -self.page,
                   'PGDOWN': self.page, 'HOME': -self.total, 'END': self.total}
        movement = actions.get(key_name(key))
        if movement is None:
            return False
        self._set_value(self.value + movement)
        return True

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Jump or drag to a position on the track."""
        if pointer.button == 'LEFT' and pointer.kind in {'press', 'move'}:
            length = self.bounds.width if self.horizontal else self.bounds.height
            offset = (pointer.x - self.bounds.x if self.horizontal else pointer.y - self.bounds.y)
            self._set_value(offset * self.maximum // max(1, length - 1))
            return True
        return False


class ScrollView(Group):
    """A vertical viewport for a widget or form larger than its parent."""

    def __init__(self, child: Widget, **options):
        """Set the content and its preferred viewport dimensions."""
        options.setdefault('height', child.preferred_height)
        super().__init__(child, **options)
        self.offset = 0
        self._scrollbar = ScrollBar(on_change=self._set_offset)

    @property
    def content(self) -> Widget:
        """Return the view contained in the viewport."""
        return self.children[0]

    def layout(self, bounds: Rect) -> None:
        """Lay out the full content and set the visible slice."""
        self.bounds = bounds
        height = max(bounds.height, self.content.preferred_height)
        scrolling = height > bounds.height and bounds.width > 1
        self.offset = min(max(0, height - bounds.height), self.offset)
        self.content.layout(Rect(bounds.x, bounds.y - self.offset,
                                 bounds.width - int(scrolling), height))
        self._scrollbar.layout(Rect(bounds.right - 1, bounds.y, int(scrolling), bounds.height))
        self._scrollbar.set_range(height, bounds.height, self.offset)

    def _set_offset(self, offset: int) -> None:
        self.offset = max(0, offset)
        self.layout(self.bounds)

    def reveal_child(self, child: Widget) -> None:
        """Scroll just enough to expose the focused control."""
        if child.bounds.y < self.bounds.y:
            self._set_offset(self.offset + child.bounds.y - self.bounds.y)
        elif self.bounds.y <= child.bounds.y and child.bounds.bottom > self.bounds.bottom:
            delta = min(child.bounds.y - self.bounds.y, child.bounds.bottom - self.bounds.bottom)
            self._set_offset(self.offset + delta)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw the clipped content and scrollbar."""
        super().draw(surface, focused)
        if self._scrollbar.bounds.width:
            self._scrollbar.draw(surface.sub(self._scrollbar.bounds))

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Scroll using the wheel or the scrollbar."""
        if pointer.kind == 'wheel':
            self._set_offset(self.offset + (-3 if pointer.button == 'UP' else 3))
            return True
        if self._scrollbar.bounds.width and (self._scrollbar.bounds.contains(pointer.x, pointer.y)
                                             or pointer.kind == 'move'):
            return self._scrollbar.on_pointer(pointer, app)
        return False


class ListBox(Widget, Generic[T]):
    """Scrollable rows with incremental search and item activation."""

    focusable = True

    def __init__(self, items: Iterable[T] = (), *, label: Callable[[T], str] = str,
                 on_activate: Callable[[T], None] | None = None,
                 on_change: Callable[[T], None] | None = None, **options):
        """Set items, item formatting, and optional selection callbacks."""
        options.setdefault('height', 3)
        super().__init__(**options)
        self.items = list(items)
        self.label = label
        self.on_activate = on_activate
        self.on_change = on_change
        self.index = 0
        self.offset = 0
        self._search = ''
        self._last_search = 0.0
        self._scroll = ScrollBar(on_change=self._set_offset)

    @property
    def value(self) -> T | None:
        """Get the item at the selection, if any."""
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def set_items(self, items: Iterable[T]) -> None:
        """Replace the list and bring the current selection back into view."""
        self.items = list(items)
        self._search = ''
        self.select(self.index)

    def select(self, index: int) -> None:
        """Select a row and ensure it is scrolled into view."""
        old = self.index
        self.index = min(max(0, len(self.items) - 1), max(0, index))
        self._seek_visible()
        item = self.value
        if item is not None and old != self.index and self.on_change:
            self.on_change(item)

    def _set_offset(self, value: int) -> None:
        self.offset = min(max(0, len(self.items) - max(1, self.bounds.height)), max(0, value))

    def _seek_visible(self) -> None:
        if self.index < self.offset:
            self._set_offset(self.index)
        elif self.index >= self.offset + max(1, self.bounds.height):
            self._set_offset(self.index - max(1, self.bounds.height) + 1)
        else:
            self._set_offset(self.offset)

    def layout(self, bounds: Rect) -> None:
        """Update viewport and scrollbar geometry."""
        selected_visible = self.offset <= self.index < self.offset + max(1, self.bounds.height)
        self.bounds = bounds
        self._scroll.layout(Rect(bounds.right - 1, bounds.y, int(bounds.width > 1), bounds.height))
        if selected_visible:
            self._seek_visible()
        else:
            self._set_offset(self.offset)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw visible rows, highlighting the selection and exposing a scrollbar."""
        surface.fill('window')
        scrolling = len(self.items) > surface.height and surface.width > 1
        content = surface.width - int(scrolling)
        for row, item in enumerate(self.items[self.offset:self.offset + surface.height]):
            role = 'text'
            if row + self.offset == self.index:
                role = 'focus' if focused is self else 'selection'
            surface.write(0, row, ' ' * content, role)
            surface.write(0, row, self.label(item), role, end=content)
        if scrolling:
            self._scroll.set_range(len(self.items), surface.height, self.offset)
            self._scroll.draw(surface.sub(self._scroll.bounds))

    def _find(self, text: str) -> None:
        now = monotonic()
        candidate = (self._search + text if now - self._last_search < 1 else text).casefold()
        self._last_search = now
        for query in (candidate, text.casefold()):
            for index, item in enumerate(self.items):
                if self.label(item).casefold().startswith(query):
                    self._search = query
                    self.select(index)
                    return
        self._search = ''

    def _activate(self) -> None:
        item = self.value
        if item is not None and self.on_activate:
            self.on_activate(item)

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Move the selection, activate it, or seek a typed prefix."""
        page = max(1, self.bounds.height)
        name = key_name(key)
        movements = {'UP': -1, 'DOWN': 1, 'PGUP': -page, 'PGDOWN': page,
                     'HOME': -len(self.items), 'END': len(self.items)}
        if name in movements:
            self.select(self.index + movements[name])
            return True
        if name == 'ENTER' and self.on_activate:
            self._activate()
            return True
        text = typed_text(key)
        if text:
            self._find(text)
            return True
        return False

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Scroll, select a row, or activate a double-clicked row."""
        if pointer.kind == 'wheel':
            self._set_offset(self.offset + (-3 if pointer.button == 'UP' else 3))
            return True
        if (self._scroll.bounds.contains(pointer.x, pointer.y)
                and len(self.items) > self.bounds.height):
            return self._scroll.on_pointer(pointer, app)
        row = self.offset + pointer.y - self.bounds.y
        if (pointer.button == 'LEFT' and pointer.kind in {'press', 'double'}
                and row < len(self.items)):
            self.select(row)
            if pointer.kind == 'double':
                self._activate()
            return True
        return False


class RadioGroup(ListBox[T]):
    """A mutually exclusive set of options, navigated with arrow keys."""

    def __init__(self, items: Iterable[T], *, label: Callable[[T], str] = str, **options):
        """Default to showing one row per option."""
        items = list(items)
        options.setdefault('height', len(items))
        super().__init__(items, label=label, **options)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw radio markers and their current selection."""
        surface.fill('window')
        for row, item in enumerate(self.items[self.offset:self.offset + surface.height]):
            selected = self.offset + row == self.index
            marker = '*' if surface.appearance.ascii_only else '•'
            role = 'focus' if focused is self and selected else 'text'
            surface.write(0, row, f'({marker if selected else " "}) {self.label(item)}', role)


class TextView(Widget):
    """A scrollable, read-only text area with horizontal and vertical navigation."""

    focusable = True

    def __init__(self, text: str = '', **options):
        """Store the text as unbounded logical lines."""
        options.setdefault('height', 4)
        super().__init__(**options)
        self.text = text
        self.top = 0
        self.left = 0

    @property
    def text(self) -> str:
        """Return the entire text."""
        return '\n'.join(self.lines)

    @text.setter
    def text(self, text: str) -> None:
        self.lines = text.expandtabs().split('\n')

    def scroll(self, rows: int, columns: int = 0) -> None:
        """Move the viewport without changing the text."""
        maximum = max(0, len(self.lines) - max(1, self.bounds.height))
        self.top = min(maximum, max(0, self.top + rows))
        self.left = max(0, self.left + columns)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw the visible section with an optional scroll guide."""
        self.scroll(0)
        surface.fill('window')
        scrolling = len(self.lines) > surface.height and surface.width > 1
        end = surface.width - int(scrolling)
        for row, line in enumerate(self.lines[self.top:self.top + surface.height]):
            surface.write(-self.left, row, line, 'text', end=end)
        if scrolling:
            bar = ScrollBar(len(self.lines), surface.height, self.top)
            bounds = Rect(surface.bounds.right - 1, surface.bounds.y, 1, surface.height)
            bar.draw(surface.sub(bounds))

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Scroll by rows, columns, or pages."""
        page = max(1, self.bounds.height)
        movements = {'UP': (-1, 0), 'DOWN': (1, 0), 'LEFT': (0, -1), 'RIGHT': (0, 1),
                     'PGUP': (-page, 0), 'PGDOWN': (page, 0),
                     'HOME': (-len(self.lines), -self.left),
                     'END': (len(self.lines), 0)}
        move = movements.get(key_name(key))
        if move is None:
            return False
        self.scroll(*move)
        return True

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Scroll with the mouse wheel or click the scroll guide."""
        if pointer.kind == 'wheel':
            self.scroll(-3 if pointer.button == 'UP' else 3)
            return True
        if (pointer.kind in {'press', 'move'} and pointer.button == 'LEFT'
                and pointer.x == self.bounds.right - 1 and len(self.lines) > self.bounds.height):
            offset = pointer.y - self.bounds.y
            maximum = len(self.lines) - self.bounds.height
            target = offset * maximum // max(1, self.bounds.height - 1)
            self.scroll(target - self.top)
            return True
        return False


class ProgressBar(Widget):
    """A determinate progress indicator."""

    def __init__(self, value: float = 0, **options):
        """Set the completed fraction between zero and one."""
        super().__init__(**options)
        self.value = value

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Show progress as a filled track and percentage."""
        fraction = max(0.0, min(1.0, self.value))
        filled = round(fraction * surface.width)
        surface.write(0, 0, ' ' * surface.width, 'control')
        surface.write(0, 0, surface.appearance.thumb * filled, 'accent')
        label = f'{fraction:.0%}'
        if surface.width >= len(label) + 1:
            surface.write(surface.width - len(label), 0, label, 'control')


class CheckGroup(Stack):
    """A vertical group of independent checkbox options."""

    def __init__(self, items: Iterable[str], *, checked: Iterable[str] = (), **options):
        """Create one checkbox per option."""
        selected = set(checked)
        super().__init__(*(Checkbox(label, label in selected) for label in items), **options)

    @property
    def values(self) -> tuple[str, ...]:
        """Return checked labels in display order."""
        return tuple(child.label for child in self.children
                     if isinstance(child, Checkbox) and child.checked)


class SplitView(Group):
    """Two panes with a mouse-draggable and keyboard-adjustable divider."""

    focusable = True

    def __init__(self, first: Widget, second: Widget, *, horizontal: bool = True,
                 fraction: float = 0.5, **options):
        """Split a viewport by columns or rows."""
        if not 0 <= fraction <= 1:
            raise ValueError('Split fraction must be between zero and one')
        super().__init__(first, second, **options)
        self.horizontal = horizontal
        self.fraction = fraction
        self._divider = _EMPTY

    def layout(self, bounds: Rect) -> None:
        """Reflow both panes around a one-cell divider."""
        self.bounds = bounds
        length = bounds.width if self.horizontal else bounds.height
        before = min(max(0, length - 2), max(1, int((length - 1) * self.fraction)))
        after = max(0, length - before - 1)
        if self.horizontal:
            first = Rect(bounds.x, bounds.y, before, bounds.height)
            second = Rect(bounds.x + before + 1, bounds.y, after, bounds.height)
            self._divider = Rect(bounds.x + before, bounds.y, min(1, length), bounds.height)
        else:
            first = Rect(bounds.x, bounds.y, bounds.width, before)
            second = Rect(bounds.x, bounds.y + before + 1, bounds.width, after)
            self._divider = Rect(bounds.x, bounds.y + before, bounds.width, min(1, length))
        self.children[0].layout(first)
        self.children[1].layout(second)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw both panes and the divider between them."""
        super().draw(surface, focused)
        divider = surface.sub(self._divider)
        role = 'focus' if focused is self else 'border'
        char = surface.appearance.frame[5 if self.horizontal else 4]
        for row in range(divider.height):
            divider.write(0, row, char * divider.width, role)

    def _move(self, offset: int) -> None:
        length = self.bounds.width if self.horizontal else self.bounds.height
        if length > 2:
            self.fraction = min(length - 2, max(1, offset)) / (length - 1)
            self.layout(self.bounds)

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Move the focused divider by one cell."""
        name = key_name(key)
        choices = ('LEFT', 'RIGHT') if self.horizontal else ('UP', 'DOWN')
        if name not in choices:
            return False
        offset = self._divider.x - self.bounds.x if self.horizontal else (
            self._divider.y - self.bounds.y)
        self._move(offset + (-1 if name == choices[0] else 1))
        return True

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Drag the divider along its axis."""
        if pointer.button != 'LEFT' or pointer.kind not in {'press', 'move'}:
            return False
        self._move(pointer.x - self.bounds.x if self.horizontal else pointer.y - self.bounds.y)
        return True


@dataclass(eq=False)
class TreeNode:
    """An outline node with optional descendants and application data."""

    label: str
    children: tuple[TreeNode, ...] = ()
    expanded: bool = False
    value: object = None


@dataclass(frozen=True)
class _TreeRow:
    node: TreeNode
    level: int
    parent: int


class TreeView(ListBox[_TreeRow]):
    """An expandable outline with keyboard and mouse navigation."""

    def __init__(self, nodes: Iterable[TreeNode], *,
                 on_select: Callable[[TreeNode], None] | None = None, **options):
        """Create an outline from the supplied root nodes."""
        self.nodes = tuple(nodes)
        self.on_select = on_select
        super().__init__(label=lambda item: item.node.label, on_activate=self._open, **options)
        self.refresh()

    @property
    def selection(self) -> TreeNode | None:
        """Get the currently selected node."""
        return self.value.node if self.value is not None else None

    def refresh(self) -> None:
        """Rebuild the visible outline while preserving the selection by identity."""
        current = self.selection
        rows: list[_TreeRow] = []
        seen = set()
        pending = [_TreeRow(node, 0, -1) for node in reversed(self.nodes)]
        while pending:
            row = pending.pop()
            if row.node in seen:
                raise ValueError('Tree nodes must be unique within an outline')
            seen.add(row.node)
            index = len(rows)
            rows.append(row)
            if row.node.expanded:
                pending.extend(_TreeRow(child, row.level + 1, index)
                               for child in reversed(row.node.children))
        self.set_items(rows)
        if current is not None:
            self.select(next((i for i, row in enumerate(rows) if row.node is current), self.index))

    def _open(self, row: _TreeRow) -> None:
        if row.node.children:
            row.node.expanded = not row.node.expanded
            self.refresh()
        elif self.on_select:
            self.on_select(row.node)

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw indentation and disclosure markers ahead of node labels."""
        original = self.label

        def label(row: _TreeRow) -> str:
            marker = '-' if row.node.expanded else surface.appearance.arrow
            gap = min(row.level * 2, max(0, surface.width - 3))
            return ' ' * gap + (marker if row.node.children else ' ') + ' ' + row.node.label

        self.label = label
        try:
            super().draw(surface, focused)
        finally:
            self.label = original

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Expand or collapse branches with Right or Left."""
        row = self.value
        name = key_name(key)
        if row is not None and name in {'LEFT', 'RIGHT'}:
            if name == 'RIGHT' and row.node.children:
                if row.node.expanded:
                    self.select(self.index + 1)
                else:
                    self._open(row)
            elif name == 'LEFT':
                if row.node.children and row.node.expanded:
                    self._open(row)
                elif row.parent >= 0:
                    self.select(row.parent)
            return True
        return super().on_key(key, app)
