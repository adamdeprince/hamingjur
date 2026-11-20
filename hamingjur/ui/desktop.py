"""Terminal desktop, overlapping windows, commands and menus."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
from math import isqrt
from time import monotonic
from typing import Callable, Iterable

from .. import Terminal
from ..keyboard import Keystroke
from ..protocols import DisplayAppearance, DisplayEvent
from .images import ImageView, _DesktopImages
from .layout import Appearance, Canvas, Rect, Row, Surface, Theme
from .widgets import InputLine, ListBox, Pointer, Widget, key_name

Action = Callable[[], None]


def _chord(shortcut: str) -> str:
    return shortcut.upper().replace('+', '_').replace(' ', '')


def _mnemonic(label: str) -> tuple[str, str]:
    text: list[str] = []
    mnemonic = ''
    chars = iter(label)
    for char in chars:
        if char == '&':
            char = next(chars, '&')
            if char != '&' and not mnemonic:
                mnemonic = char.upper()
        text.append(char)
    return ''.join(text), mnemonic


@dataclass(frozen=True)
class MenuItem:
    """A command, submenu or separator. Mark a mnemonic with an ampersand."""

    label: str
    action: Action | None = None
    shortcut: str = ''
    items: tuple[MenuItem, ...] = ()
    enabled: bool = True

    @classmethod
    def submenu(cls, label: str, *items: MenuItem) -> MenuItem:
        """Build a submenu from a sequence of commands."""
        return cls(label, items=items)

    @property
    def text(self) -> str:
        """Return the label without its mnemonic marker."""
        return _mnemonic(self.label)[0]

    @property
    def mnemonic(self) -> str:
        """Return the access key, if one was marked."""
        return _mnemonic(self.label)[1]


@dataclass(frozen=True)
class StatusItem:
    """A visible, clickable shortcut in the status line."""

    shortcut: str
    label: str
    action: Action


@dataclass
class _MenuPage:
    items: tuple[MenuItem, ...]
    selected: int
    first: int = 0
    box: Rect = Rect(0, 0, 0, 0)

    @classmethod
    def open(cls, items: tuple[MenuItem, ...]) -> _MenuPage:
        index = next((i for i, item in enumerate(items) if item.label and item.enabled), -1)
        return cls(items, index)

    def move(self, delta: int) -> None:
        if self.items:
            for step in range(1, len(self.items) + 1):
                index = (self.selected + delta * step) % len(self.items)
                item = self.items[index]
                if item.label and item.enabled:
                    self.selected = index
                    return

    def draw(self, surface: Surface) -> None:
        surface.fill('menu')
        rows = max(0, surface.height - 2)
        self.first = min(max(0, len(self.items) - rows), max(
            min(self.first, self.selected), self.selected - rows + 1))
        horizontal = surface.appearance.frame[4]
        surface.write(0, 0, horizontal * surface.width, 'bar')
        surface.write(0, surface.height - 1, horizontal * surface.width, 'bar')
        if self.first:
            surface.write(0, 0, '^', 'bar')
        if self.first + rows < len(self.items):
            surface.write(0, surface.height - 1, 'v', 'bar')
        for y, item in enumerate(self.items[self.first:self.first + rows], 1):
            index = self.first + y - 1
            if not item.label:
                surface.write(1, y, horizontal * max(0, surface.width - 2), 'menu')
                continue
            role = 'focus' if index == self.selected else 'menu' if item.enabled else 'disabled'
            suffix = surface.appearance.arrow if item.items else item.shortcut
            column = surface.width - surface.measure(suffix) - 1
            surface.write(0, y, ' ' * surface.width, role)
            surface.write(1, y, item.text, role, end=max(1, column - bool(suffix)))
            if suffix:
                surface.write(max(1, column), y, suffix, role)


class MenuBar:
    """Pull-down and cascading menus shared by keyboard and pointer input."""

    def __init__(self, menus: Iterable[MenuItem] = ()):
        """Set the top-level menus."""
        self.menus = tuple(menus)
        self._buttons: list[tuple[int, int]] = []
        self._pages: list[_MenuPage] = []
        self._active: int | None = None
        self._popup = (0, 1)

    @property
    def open(self) -> bool:
        """Return whether a menu is taking input."""
        return bool(self._pages)

    def close(self) -> None:
        """Dismiss the current menu hierarchy."""
        self._pages.clear()
        self._active = None

    def show(self, index: int = 0) -> bool:
        """Open a top-level menu."""
        if not self.menus:
            return False
        for offset in range(len(self.menus)):
            candidate = (index + offset) % len(self.menus)
            item = self.menus[candidate]
            if item.enabled and item.items:
                self._active = candidate
                self._pages[:] = [_MenuPage.open(item.items)]
                return True
        return False

    def popup(self, items: Iterable[MenuItem], x: int, y: int) -> None:
        """Open a context menu independently of the menu bar."""
        self._active = None
        self._popup = x, y
        self._pages[:] = [_MenuPage.open(tuple(items))]

    def draw_bar(self, surface: Surface) -> None:
        """Draw the menu bar and remember its clickable spans."""
        self._buttons.clear()
        surface.fill('bar')
        x = 0
        for index, menu in enumerate(self.menus):
            text = f' {menu.text} '
            width = surface.measure(text)
            self._buttons.append((x, x + width))
            surface.write(x, 0, text, 'focus' if index == self._active else 'bar')
            x += width

    def draw_dropdowns(self, canvas: Canvas, area: Rect) -> None:
        """Draw the active submenu chain above the windows."""
        x, y = self._popup if self._active is None else (self._buttons[self._active][0], 1)
        measure = canvas.surface(area).measure
        for page in self._pages:
            width = max((measure(item.text) + measure(item.shortcut) + 4 for item in page.items),
                        default=4)
            page.box = Rect(x, y, width, len(page.items) + 2).fit(area)
            page.draw(canvas.surface(page.box))
            x = page.box.right - 1
            y = page.box.y + page.selected - page.first + 1

    def _choose(self, page: _MenuPage) -> None:
        if page.selected < 0:
            return
        item = page.items[page.selected]
        if item.items:
            self._pages.append(_MenuPage.open(item.items))
        elif item.action:
            self.close()
            item.action()

    def on_key(self, key: Keystroke | str) -> bool:
        """Open by mnemonic or navigate the active menu."""
        name = key_name(key)
        if name in {'F10', 'CTRL_G'} and not self._pages:
            return self.show()
        if name.startswith('ALT_'):
            for index, menu in enumerate(self.menus):
                if menu.mnemonic == name[4:]:
                    return self.show(index)
        if not self._pages:
            return False
        page = self._pages[-1]
        if name == 'ESCAPE':
            if len(self._pages) > 1:
                self._pages.pop()
            else:
                self.close()
        elif name == 'UP':
            page.move(-1)
        elif name == 'DOWN':
            page.move(1)
        elif name == 'LEFT':
            if len(self._pages) > 1:
                self._pages.pop()
            elif self._active is not None:
                self.show(self._active - 1)
        elif name == 'RIGHT':
            if page.selected >= 0 and page.items[page.selected].items:
                self._choose(page)
            elif self._active is not None:
                self.show(self._active + 1)
        elif name in {'ENTER', 'SPACE'}:
            self._choose(page)
        elif not name:
            for index, item in enumerate(page.items):
                if item.enabled and item.mnemonic == str(key).upper():
                    page.selected = index
                    self._choose(page)
                    break
        return True

    def on_pointer(self, pointer: Pointer) -> bool:
        """Press or hover over menus and invoke commands with a click."""
        pressed = pointer.button == 'LEFT' and pointer.kind in {'press', 'double'}
        if pointer.y == 0 and (pressed or self.open and pointer.kind == 'move'):
            for index, (left, right) in enumerate(self._buttons):
                if left <= pointer.x < right:
                    if index == self._active and pressed:
                        self.close()
                    elif index != self._active:
                        self.show(index)
                    return True
        if not self.open:
            return False
        for level in range(len(self._pages) - 1, -1, -1):
            page = self._pages[level]
            if page.box.contains(pointer.x, pointer.y):
                offset = pointer.y - page.box.y
                if pointer.kind == 'wheel':
                    page.move(-1 if pointer.button == 'UP' else 1)
                elif pressed and offset in {0, page.box.height - 1}:
                    page.move(-1 if offset == 0 else 1)
                elif 0 < offset < page.box.height - 1:
                    index = offset - 1 + page.first
                    if (index < len(page.items) and page.items[index].label
                            and page.items[index].enabled):
                        if pressed or page.selected != index:
                            del self._pages[level + 1:]
                        page.selected = index
                        if pressed:
                            self._choose(page)
                return True
        if pressed:
            self.close()
            return True
        return False

    def dispatch_shortcut(self, name: str) -> bool:
        """Dispatch a shortcut from any enabled menu branch."""
        pending = list(reversed(self.menus))
        while pending:
            item = pending.pop()
            if item.enabled:
                if item.shortcut and _chord(item.shortcut) == name and item.action:
                    item.action()
                    return True
                pending.extend(reversed(item.items))
        return False


class Window:
    """An overlapping desktop window with its own focus and child layout."""

    modal = False

    def __init__(self, title: str, content: Widget, bounds: Rect = Rect(3, 2, 48, 14), *,
                 minimum: tuple[int, int] = (12, 4), resizable: bool = True,
                 closable: bool = True, on_close: Callable[[object], None] | None = None,
                 help_topic: str | None = None):
        """Create a window whose requested bounds are fitted to the desktop."""
        self.title = title
        self.content = content
        self.bounds = bounds
        self.minimum = minimum
        self.resizable = resizable
        self.closable = closable
        self.on_close = on_close
        self.help_topic = help_topic
        self.maximized = False
        self._tiled = False
        self.screen_bounds = Rect(0, 0, 0, 0)
        self.focused: Widget | None = None
        self.app: Application | None = None

    def layout(self, area: Rect) -> None:
        """Fit the window to the current screen and update focus."""
        minimum = (1, 1) if self._tiled else self.minimum
        self.screen_bounds = area if self.maximized else self.bounds.fit(area, minimum)
        self.content.layout(self.screen_bounds.inset())
        choices = list(self.content.focus_order())
        if self.focused not in choices:
            self.focused = choices[0] if choices else None
            if self.focused is not None:
                self.focused.reveal()

    def cycle_focus(self, step: int) -> None:
        """Move focus between enabled controls."""
        choices = list(self.content.focus_order())
        if choices:
            index = choices.index(self.focused) if self.focused in choices else -1
            self.focused = choices[(index + step) % len(choices)]
            self.focused.reveal()

    def draw(self, canvas: Canvas, active: bool) -> None:
        """Draw the frame, chrome, and clipped window content."""
        box = self.screen_bounds
        if not box.width or not box.height:
            return
        surface = canvas.surface(box)
        surface.fill()
        style = 'title' if active else 'border'
        top_left, top_right, bottom_left, bottom_right, across, side = surface.appearance.frame
        horizontal = across * max(0, box.width - 2)
        surface.write(0, 0, top_left + horizontal + top_right, style)
        surface.write(0, box.height - 1, bottom_left + horizontal + bottom_right, 'border')
        for y in range(1, box.height - 1):
            surface.write(0, y, side, 'border')
            surface.write(box.width - 1, y, side, 'border')
        if box.width > 4 and box.height > 1:
            close = f'[{surface.appearance.close}]' if self.closable else ''
            if close:
                surface.write(box.width - 4, 0, close, style)
            surface.write(2, 0, f' {self.title} ', style, end=box.width - len(close) - 1)
            if self.resizable:
                surface.write(box.width - 2, box.height - 1, surface.appearance.grip, style)
        self.content.draw(surface.sub(self.content.bounds), self.focused if active else None)

    def close(self, result: object = None) -> None:
        """Close this window and report a result to its callback."""
        if self.app is not None:
            self.app.close(self, result)

    def on_key(self, key: Keystroke | str) -> bool:
        """Handle a key left unconsumed by the focused widget."""
        return False


class Dialog(Window):
    """A modal window with a default action and cancellation."""

    modal = True

    def __init__(self, title: str, content: Widget, bounds: Rect = Rect(0, 0, 50, 12), *,
                 default: Action | None = None, **options):
        """Set the Return action for fields that do not consume Return themselves."""
        options.setdefault('resizable', False)
        super().__init__(title, content, bounds, **options)
        self.default = default

    def on_key(self, key: Keystroke | str) -> bool:
        """Cancel on Escape or run the default action on Return."""
        name = key_name(key)
        if name == 'ESCAPE':
            self.close()
            return True
        if name == 'ENTER' and self.default:
            self.default()
            return True
        return False


@dataclass
class _Move:
    window: Window
    original: Rect
    start: Rect
    resize: bool
    tiled: bool
    anchor: tuple[int, int] | None = None


class Application:
    """Drive a themed desktop with Hamingjur's keyboard, mouse and resize APIs."""

    def __init__(self, terminal: Terminal | None = None, *, theme: Theme | None = None,
                 appearance: Appearance | None = None, menus: Iterable[MenuItem] = (),
                 status: Iterable[StatusItem] = (), mouse: bool | None = None):
        """Bind a terminal and configure the desktop's appearance and commands."""
        self.terminal = terminal if terminal is not None else Terminal()
        self._theme_override = theme
        self._dark_theme = Theme()
        self._light_theme = Theme(light=True)
        self._display_appearance = DisplayAppearance.NO_PREFERENCE
        self._eink_scheme: DisplayAppearance | None = None
        self._display_poll_on_resize = False
        self.appearance = (appearance if appearance is not None
                           else Appearance.for_terminal(self.terminal))
        self.mouse = not self.appearance.ascii_only if mouse is None else mouse
        self.menus = MenuBar(menus)
        self.status_items = tuple(status)
        self.windows: list[Window] = []
        self.status = ''
        self.status_error = False
        self.result: object = None
        self.clipboard = ''
        self.on_help: Action | None = None
        self._bindings: dict[str, Action] = {}
        self._closed = False
        self._running = False
        self._previous: list[Row] | None = None
        self._visuals: tuple[Theme, Appearance] | None = None
        self._status_buttons: list[tuple[int, int, StatusItem]] = []
        self._moving: _Move | None = None
        self._captured: Widget | None = None
        self._last_click: tuple[float, int, int] | None = None
        self._size = Rect(0, 0, 0, 0)
        self._images = _DesktopImages(self.terminal)
        self._cursor_xy: tuple[int, int] | None = None

    @property
    def theme(self) -> Theme:
        """Return the explicit palette or the palette for the reported display preference."""
        if self._theme_override is not None:
            return self._theme_override
        if (self._eink_scheme or self._display_appearance) == DisplayAppearance.LIGHT:
            return self._light_theme
        return self._dark_theme

    @theme.setter
    def theme(self, theme: Theme | None) -> None:
        """Set a palette, or pass None to follow the terminal's display appearance."""
        self._theme_override = theme

    @property
    def follows_display_appearance(self) -> bool:
        """Return whether the palette follows the terminal's display reports."""
        return self._theme_override is None

    @property
    def display_appearance(self) -> DisplayAppearance:
        """Return the last reported desktop appearance."""
        return self._display_appearance

    @property
    def active_window(self) -> Window | None:
        """Return the topmost window."""
        return self.windows[-1] if self.windows else None

    @property
    def work_area(self) -> Rect:
        """Return the area below the menu and above the status line."""
        return Rect(0, 1, max(0, self.terminal.width), max(0, self.terminal.height - 2))

    def bind(self, shortcut: str, action: Action) -> None:
        """Bind a shortcut using names such as Ctrl+O, Alt+F, or F5."""
        self._bindings[_chord(shortcut)] = action

    def set_status(self, message: str, *, error: bool = False) -> None:
        """Set the application message in the bottom bar."""
        self.status, self.status_error = message, error

    def add(self, window: Window, *, center: bool = False) -> Window:
        """Show a new window, automatically centering modal dialogs."""
        if window.app is not None or window.content.parent is not None or any(
                other.content is window.content for other in self.windows):
            raise ValueError('Window and its contents must belong to only one desktop')
        area = self.work_area
        if center or window.modal:
            size = window.bounds.fit(area, window.minimum)
            window.bounds = replace(size, x=area.x + (area.width - size.width) // 2,
                                    y=area.y + (area.height - size.height) // 2)
        active = self.active_window
        if active is not None and active.modal and not window.modal:
            index = next(i for i, current in enumerate(self.windows) if current.modal)
            self.windows.insert(index, window)
        else:
            self.windows.append(window)
            self.menus.close()
        window.app = self
        window.layout(area)
        return window

    def close(self, window: Window, result: object = None) -> None:
        """Remove a window and call its close handler after restoring focus."""
        if window.app is not self:
            return
        self.windows.remove(window)
        window.app = None
        self._captured = None
        if self._moving and self._moving.window is window:
            self._moving = None
        if window.on_close:
            window.on_close(result)

    def activate(self, window: Window) -> None:
        """Bring a modeless window to front when no modal dialog owns input."""
        if window.app is self and self.active_window and not self.active_window.modal:
            self.windows.remove(window)
            self.windows.append(window)

    def next_window(self, step: int = 1) -> None:
        """Cycle between desktop windows."""
        if len(self.windows) > 1 and not self.windows[-1].modal:
            if step > 0:
                self.windows.insert(0, self.windows.pop())
            else:
                self.windows.append(self.windows.pop(0))

    def tile(self) -> None:
        """Arrange windows into a responsive grid with no overlap."""
        windows = [window for window in self.windows if not window.modal]
        if not windows:
            return
        area = self.work_area
        rows = max(1, isqrt(len(windows)))
        remaining = len(windows)
        index = 0
        for row in range(rows):
            columns = (remaining + rows - row - 1) // (rows - row)
            y = area.y + row * area.height // rows
            bottom = area.y + (row + 1) * area.height // rows
            for column in range(columns):
                x = area.x + column * area.width // columns
                right = area.x + (column + 1) * area.width // columns
                window = windows[index]
                window.maximized = False
                window._tiled = True
                window.bounds = Rect(x, y, right - x, bottom - y)
                window.layout(area)
                index += 1
            remaining -= columns

    def cascade(self) -> None:
        """Arrange windows with successively offset title bars."""
        windows = [window for window in self.windows if not window.modal]
        area = self.work_area
        if not windows:
            return
        max_steps = max(1, min(area.width // 4, area.height // 2))
        steps = min(len(windows), max_steps)
        for index, window in enumerate(windows):
            step = index % steps
            window.maximized = False
            window._tiled = False
            window.bounds = Rect(area.x + step * 2, area.y + step,
                                 area.width - (steps - 1) * 2, area.height - steps + 1)
            window.layout(area)

    def maximize(self, window: Window | None = None) -> None:
        """Toggle maximization while preserving the normal window bounds."""
        window = self.active_window if window is None else window
        if window is not None and window.resizable:
            window.maximized = not window.maximized
            window.layout(self.work_area)

    def move_window(self, window: Window | None = None, *, resize: bool = False) -> None:
        """Enter keyboard move or resize mode for a window."""
        window = self.active_window if window is None else window
        if window is not None and window.app is self:
            self._begin_move(window, resize=resize)

    def stop(self, result: object = None) -> None:
        """End the event loop and return a caller-provided result."""
        self.result, self._closed = result, True

    def _draw_status(self, surface: Surface) -> None:
        self._status_buttons.clear()
        surface.fill('bar')
        x = 0
        for item in self.status_items:
            text = f' {item.shortcut} {item.label} '
            right = x + surface.measure(text)
            surface.write(x, 0, text, 'bar')
            self._status_buttons.append((x, right, item))
            x = right
        if self.status:
            if self.status_error or x >= surface.width:
                x = 0
            surface.write(x, 0, ' ' * max(0, surface.width - x),
                          'error' if self.status_error else 'bar')
            surface.write(x + 1, 0, self.status, 'error' if self.status_error else 'bar')

    def _cursor_position(self, screen: Rect) -> tuple[int, int] | None:
        if self.menus.open and self.menus._pages[-1].selected >= 0:
            page = self.menus._pages[-1]
            x, y = page.box.x, page.box.y + page.selected - page.first + 1
            if page.box.contains(x, y):
                return x, y
        window = self.active_window
        if window is not None and window.focused is not None:
            focused = window.focused
            clip = focused.bounds.intersection(window.content.bounds)
            parent = focused.parent
            while parent is not None:
                clip = clip.intersection(parent.bounds)
                parent = parent.parent
            point = focused.caret_position()
            if point is not None and clip.contains(*point):
                return point
            if clip.width and clip.height:
                return clip.x, clip.y
        if window is not None and screen.contains(window.screen_bounds.x, window.screen_bounds.y):
            return window.screen_bounds.x, window.screen_bounds.y
        return (0, 0) if screen.width and screen.height else None

    def draw(self) -> Canvas:
        """Compose and present the current screen, returning the rendered cells."""
        bounds = Rect(0, 0, max(0, self.terminal.width), max(0, self.terminal.height))
        canvas = Canvas(bounds, self.appearance, self.terminal)
        area = self.work_area
        self.menus.draw_bar(canvas.surface(Rect(0, 0, bounds.width, min(1, bounds.height))))
        for window in self.windows:
            window.layout(area)
            if self.appearance.shadow and not window.maximized:
                box = window.screen_bounds
                shadow = Rect(box.x + 1, box.y + 1, box.width, box.height)
                canvas.surface(shadow.intersection(area)).fill('shadow')
            window.draw(canvas, window is self.active_window)
        if bounds.height > 1:
            self._draw_status(canvas.surface(Rect(0, bounds.height - 1, bounds.width, 1)))
        self.menus.draw_dropdowns(canvas, area)
        self._images.sync(canvas)
        if self._size != bounds or self._visuals != (self.theme, self.appearance):
            self._previous = None
        cursor = self._cursor_position(bounds)
        canvas.present(self.theme, self._previous, cursor=cursor, previous_cursor=self._cursor_xy)
        self._cursor_xy = cursor
        self._previous, self._size = canvas.rows, bounds
        self._visuals = self.theme, self.appearance
        return canvas

    @property
    def visible_images(self) -> tuple[ImageView, ...]:
        """Return fully visible image views from the last frame in reading order."""
        return self._images.visible

    def review_image(self, view: ImageView) -> Dialog:
        """Open one image's full text description in a keyboard-accessible dialog."""
        from .dialogs import message_box

        annotation = view.annotation
        return message_box(self, annotation.name or 'Image', self._image_description(view))

    @staticmethod
    def _image_description(view: ImageView) -> str:
        annotation = view.annotation
        text = [annotation.description, annotation.details]
        return '\n\n'.join(filter(None, text)) or 'No description provided.'

    def review_images(self, *, include_decorative: bool = False) -> Dialog:
        """Review the desktop's visible image descriptions, including on text-only terminals."""
        from .dialogs import message_box

        entries = []
        for view in self.visible_images:
            annotation = view.annotation
            if annotation.purpose == 'decorative' and not include_decorative:
                continue
            entries.append(f'{annotation.name or "Image"}\n{self._image_description(view)}')
        return message_box(self, 'Image descriptions', '\n\n'.join(entries) or 'No visible images.')

    def release_images(self) -> None:
        """Release terminal placements when using a custom event loop."""
        self._images.clear()

    def _begin_move(self, window: Window, *, resize: bool,
                    anchor: tuple[int, int] | None = None) -> None:
        if not window.maximized and (not resize or window.resizable):
            self._moving = _Move(window, window.bounds, window.screen_bounds,
                                 resize, window._tiled, anchor)
            if anchor is None:
                self.set_status('Arrow keys adjust  Enter accepts  Esc cancels')

    def _move_by(self, dx: int, dy: int, relative: bool) -> None:
        assert self._moving is not None
        move = self._moving
        window = move.window
        window._tiled = False
        base = window.screen_bounds if relative else move.start
        if move.resize:
            area = self.work_area
            box = replace(base, width=min(area.right - base.x, base.width + dx),
                          height=min(area.bottom - base.y, base.height + dy))
        else:
            box = replace(base, x=base.x + dx, y=base.y + dy)
        window.bounds = box.fit(self.work_area, window.minimum)
        window.layout(self.work_area)

    def _move_key(self, name: str) -> bool:
        if not self._moving or self._moving.anchor is not None:
            return False
        movement = {'LEFT': (-1, 0), 'RIGHT': (1, 0), 'UP': (0, -1), 'DOWN': (0, 1)}
        if name in movement:
            self._move_by(*movement[name], relative=True)
        elif name in {'ESCAPE', 'ENTER'}:
            if name == 'ESCAPE':
                window = self._moving.window
                window.bounds = self._moving.original
                window._tiled = self._moving.tiled
                window.layout(self.work_area)
            self._moving = None
            self.set_status('')
        return True

    def _key(self, key: Keystroke | str) -> bool:
        name = key_name(key)
        if name == 'RESIZE_EVENT':
            self._previous = None
            if self._display_poll_on_resize:
                self._read_display()
            return True
        window = self.active_window
        modal = window is not None and window.modal
        if self._move_key(name) or (not modal and self.menus.on_key(key)):
            return True
        if name in {'F1', 'CTRL_H'} and self.on_help:
            self.on_help()
            return True
        if window is not None:
            if name in {'TAB', 'SHIFT_TAB'}:
                window.cycle_focus(-1 if name == 'SHIFT_TAB' else 1)
                return True
            if window.focused and window.focused in window.content.focus_order():
                if window.focused.on_key(key, self):
                    return True
            if window.on_key(key):
                return True
        if modal:
            return False
        if name in self._bindings:
            self._bindings[name]()
            return True
        for item in self.status_items:
            if _chord(item.shortcut) == name:
                item.action()
                return True
        if self.menus.dispatch_shortcut(name):
            return True
        if window is not None:
            if name in {'F6', 'SHIFT_F6', 'CTRL_T'}:
                self.next_window(-1 if name == 'SHIFT_F6' else 1)
            elif name == 'F5':
                self.maximize(window)
            elif name in {'F7', 'F8'}:
                self._begin_move(window, resize=name == 'F8')
            elif name in {'ALT_F4', 'CTRL_W'} and window.closable:
                window.close()
            else:
                return False
            return True
        return False

    def _hit_window(self, pointer: Pointer) -> Window | None:
        active = self.active_window
        if active and active.modal:
            return active if active.screen_bounds.contains(pointer.x, pointer.y) else None
        return next((window for window in reversed(self.windows)
                     if window.screen_bounds.contains(pointer.x, pointer.y)), None)

    def _chrome(self, window: Window, pointer: Pointer) -> bool:
        if pointer.button != 'LEFT' or pointer.kind not in {'press', 'double'}:
            return False
        box = window.screen_bounds
        if pointer.y == box.y:
            if window.closable and box.width > 4 and box.right - 4 <= pointer.x < box.right - 1:
                window.close()
            elif pointer.kind == 'double':
                self.maximize(window)
            else:
                self._begin_move(window, resize=False, anchor=(pointer.x, pointer.y))
            return True
        if window.resizable and pointer.y == box.bottom - 1 and pointer.x >= box.right - 2:
            self._begin_move(window, resize=True, anchor=(pointer.x, pointer.y))
            return True
        return False

    def _pointer(self, pointer: Pointer) -> bool:
        if self._moving and self._moving.anchor is not None:
            if pointer.kind == 'move':
                x, y = self._moving.anchor
                self._move_by(pointer.x - x, pointer.y - y, relative=False)
            elif pointer.kind == 'release':
                self._moving = None
            return True
        if self._captured is not None and pointer.kind in {'move', 'release'}:
            captured = self._captured
            if pointer.kind == 'release':
                self._captured = None
            return captured.on_pointer(pointer, self)
        active = self.active_window
        modal = active is not None and active.modal
        if not modal and self.menus.on_pointer(pointer):
            return True
        pressed = pointer.kind in {'press', 'double'}
        if not modal and pressed and pointer.y == self._size.height - 1:
            for left, right, item in self._status_buttons:
                if left <= pointer.x < right:
                    item.action()
                    return True
        window = self._hit_window(pointer)
        if window is None:
            return False
        if pressed:
            self.activate(window)
        if self._chrome(window, pointer):
            return True
        target = window.content.hit(pointer.x, pointer.y)
        while target is not None:
            if pressed and target.focusable:
                window.focused = target
            if target.on_pointer(pointer, self):
                if pressed and window.app is self:
                    self._captured = target
                return True
            target = target.parent
        return False

    @staticmethod
    def _from_mouse(key: Keystroke) -> Pointer:
        name = key.name or ''
        x, y = key.mouse_xy
        if 'SCROLL' in name:
            return Pointer(x, y, 'UP' if name.endswith('_UP') else 'DOWN', 'wheel')
        button = next((item for item in ('LEFT', 'MIDDLE', 'RIGHT') if item in name), '')
        if name.endswith('RELEASED'):
            kind = 'release'
        else:
            kind = 'move' if name.endswith('MOTION') else 'press'
        return Pointer(x, y, button, kind)

    def dispatch(self, event: Keystroke | str | Pointer | DisplayEvent) -> bool:
        """Process an input or display event independently of the terminal loop."""
        if isinstance(event, Keystroke):
            report = event.protocol_event
            if isinstance(report, DisplayEvent):
                event = report
            elif (event.name or '').startswith('MOUSE_'):
                event = self._from_mouse(event)
        if isinstance(event, DisplayEvent):
            self._display_appearance = event.appearance
            self._eink_scheme = event.eink_scheme
            if self._eink_scheme is None and event.fields.get('kind') == 'eink':
                self._eink_scheme = DisplayAppearance.LIGHT
            return True
        if isinstance(event, Pointer):
            if event.x < 0 or event.y < 0:
                return False
            if event.button == 'LEFT' and event.kind == 'press':
                timestamp = monotonic()
                previous = self._last_click
                self._last_click = timestamp, event.x, event.y
                if (previous and previous[1:] == (event.x, event.y)
                        and timestamp - previous[0] < 0.4):
                    event = replace(event, kind='double')
                    self._last_click = None
            return self._pointer(event)
        return self._key(event)

    def choose_history(self, field: InputLine) -> None:
        """Choose a previous entry for an input field in a modal history window."""
        entries = field.editor.history.entries
        if not entries:
            self.set_status('No input history')
            return
        dialog: Dialog

        def choose(value: str) -> None:
            field.value = value
            dialog.close(value)

        content = ListBox(reversed(entries), on_activate=choose, grow=1)
        dialog = Dialog('Input history', content, Rect(0, 0, 46, min(len(entries) + 2, 12)))
        self.add(dialog)

    def _read_display(self) -> bool:
        report = self.terminal.display.query()
        if report is None:
            return False
        self.dispatch(report)
        return True

    def _watch_display(self, stack: ExitStack) -> None:
        # The display query works independently of event-mode permission.
        if not self._read_display():
            return
        self._display_poll_on_resize = True
        try:
            granted = stack.enter_context(self.terminal.display.enabled())
        except RuntimeError:
            return
        self._display_poll_on_resize = not bool(granted)

    def run(self) -> object:
        """Enter terminal modes until stop is called, then restore them on exit."""
        term = self.terminal
        if not term.does_styling or not term.is_a_tty:
            raise RuntimeError('A styled interactive terminal is required')
        if self._running:
            raise RuntimeError('The application is already running')
        self._closed, self._running = False, True
        self.result, self._previous = None, None
        self._display_appearance = DisplayAppearance.NO_PREFERENCE
        self._eink_scheme = None
        try:
            with ExitStack() as stack:
                stack.enter_context(term.fullscreen())
                stack.callback(self.release_images)
                stack.enter_context(term.cbreak())
                stack.enter_context(term.keypad())
                stack.enter_context(term.hidden_cursor())
                if self.mouse:
                    stack.enter_context(term.mouse_enabled(report_drag=True))
                if not self.appearance.ascii_only:
                    stack.enter_context(term.bracketed_paste())
                    stack.enter_context(term.notify_on_resize())
                    self._watch_display(stack)
                while not self._closed:
                    self.draw()
                    self.dispatch(term.inkey())
        finally:
            self._running = False
            self._display_poll_on_resize = False
            self._moving = None
            self._captured = None
            self.menus.close()
        return self.result
