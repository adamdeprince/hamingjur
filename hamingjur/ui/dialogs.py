"""Standard forms, file selectors, help and theme dialogs."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ._i18n import Message
from .layout import Rect, Theme
from .desktop import Application, Dialog
from .widgets import (Button, InputLine, Label, ListBox, RadioGroup, ScrollView, Stack,
                      TextView, Widget)


class RegexValidator:
    """Validate an input field using an entire-value regular expression."""

    def __init__(self, pattern: str, message: str):
        """Set the expression and the message shown on validation failure."""
        self.pattern = re.compile(pattern)
        self.message = message

    def __call__(self, text: str) -> str | None:
        """Return the configured error message when the value does not match."""
        return None if self.pattern.fullmatch(text) else self.message


@dataclass(frozen=True)
class IntegerValidator:
    """An inclusive integer range validator."""

    minimum: int
    maximum: int

    def __call__(self, text: str) -> str | None:
        """Return an error when the value is outside the range."""
        try:
            if self.minimum <= int(text) <= self.maximum:
                return None
        except ValueError:
            pass
        return Message('Enter an integer from {minimum} to {maximum}',
                       minimum=self.minimum, maximum=self.maximum)


class FormDialog(Dialog):
    """A modal form that validates its input fields before returning values."""

    def __init__(self, title: str, fields: Mapping[str, InputLine], *,
                 on_submit: Callable[[dict[str, str]], None] | None = None,
                 bounds: Rect | None = None):
        """Lay out named fields in order, with OK and Cancel actions."""
        self.fields = dict(fields)
        self.on_submit = on_submit
        rows: list[Widget] = []
        for name, field in self.fields.items():
            rows.extend((Label(name, accessibility_role='prompt'), field))
        rows.append(Stack(Button(Message('OK'), self.submit), Button(Message('Cancel'), self.close),
                          horizontal=True, gap=1))
        content = Stack(*rows, gap=1)
        size = bounds if bounds is not None else Rect(0, 0, 50, content.preferred_height + 2)
        super().__init__(title, ScrollView(content), size, default=self.submit)

    def submit(self) -> None:
        """Validate, remember non-password history, and return the form data."""
        for field in self.fields.values():
            error = field.validate()
            if error:
                self.focused = field
                field.reveal()
                if self.app:
                    self.app.set_status(error, error=True)
                return
        values = {name: field.value for name, field in self.fields.items()}
        for field in self.fields.values():
            if not field.editor.password_mode:
                field.editor.history.add(field.value)
        if self.app:
            self.app.set_status('')
        self.close(values)
        if self.on_submit:
            self.on_submit(values)


class _MessageView(TextView):
    def __init__(self, message: str, *, grow: int, height: int):
        super().__init__(message, grow=grow, height=height)
        self.message = message

    def _localize(self, translate: Callable[[str], str], measure: Callable[[str], int]) -> bool:
        self.text = translate(self.message)
        return False


def message_box(app: Application, title: str, message: str, *,
                choices: Iterable[str] | None = None,
                on_close: Callable[[object], None] | None = None) -> Dialog:
    """Open a modal message with caller-defined buttons and return their labels."""
    labels = tuple(choices) if choices is not None else ('OK',)
    if not labels:
        raise ValueError('A message box needs at least one button')
    dialog: Dialog

    def choice(value: str) -> Callable[[], None]:
        return lambda: dialog.close(value)

    buttons = ([Button(label, choice(label)) for label in labels] if choices is not None
               else [Button(Message('OK'), choice('OK'))])
    # The default height is a visual viewport. Text beyond it remains scrollable.
    view = _MessageView if isinstance(message, Message) else TextView
    text = view(message, grow=1, height=min(8, max(1, len(message.splitlines()))))
    actions = Stack(*buttons, horizontal=True, gap=1)
    content = Stack(text, actions)
    size = Rect(0, 0, max(48, actions.preferred_width + 2), content.preferred_height + 2)
    dialog = Dialog(title, content, size, default=lambda: dialog.close(labels[0]),
                    on_close=on_close)
    app.add(dialog)
    if actions.children[0] in content.focus_order():
        dialog.focused = actions.children[0]
    return dialog


def prompt(app: Application, title: str, label: str, *, value: str = '',
           validator: Callable[[str], str | None] | None = None,
           on_submit: Callable[[str], None] | None = None) -> FormDialog:
    """Open a single-field modal prompt."""
    def submit(values: dict[str, str]) -> None:
        if on_submit:
            on_submit(values[label])

    dialog = FormDialog(title, {label: InputLine(value, validator=validator)}, on_submit=submit)
    app.add(dialog)
    return dialog


class FileDialog(Dialog):
    """A keyboard and mouse file selector for open, save, or directory selection."""

    def __init__(self, title: str = Message('Open file'), *, directory: str | Path = '.',
                 mode: str = 'open', pattern: str = '*',
                 on_select: Callable[[Path], None] | None = None):
        """Start in the specified directory and filter files with a glob pattern."""
        if mode not in {'open', 'save', 'directory'}:
            raise ValueError('File dialog mode must be open, save, or directory')
        self.mode = mode
        self.pattern = pattern
        self.directory = Path(directory).expanduser().absolute()
        self.on_select = on_select
        self.location = Label(str(self.directory), role='muted')
        self.name = InputLine(on_submit=lambda _: self.submit())
        self.listing = ListBox(label=self._label, on_activate=self._open_entry, grow=1, height=6)
        self.error = Label('', role='error')
        controls = Stack(Button(Message('Select'), self.submit), Button(Message('Up'), self.up),
                         Button(Message('Cancel'), self.close), horizontal=True, gap=1)
        super().__init__(title, Stack(self.location, self.listing, self.name, self.error, controls),
                         Rect(0, 0, 58, 16), default=self.submit)
        self._read(self.directory)

    @staticmethod
    def _label(path: Path) -> str:
        return '../' if path.name == '..' else path.name + ('/' if path.is_dir() else '')

    def _read(self, directory: Path) -> None:
        """Change directories only after successfully obtaining the new list."""
        try:
            directory = directory.resolve()
            entries = []
            if directory.parent != directory:
                entries.append(directory / '..')
            children = [(path.is_dir(), path) for path in directory.iterdir()]
            entries.extend(path for isdir, path in sorted(
                children, key=lambda item: (not item[0], item[1].name.casefold()))
                           if isdir or (self.mode != 'directory' and path.match(self.pattern)))
        except (OSError, RuntimeError) as error:
            self.error.text = str(error)
            return
        self.directory = directory
        self.location.text = str(directory)
        self.listing.set_items(entries)
        self.listing.select(0)
        self.name.value = ''
        self.error.text = ''

    def _open_entry(self, path: Path) -> None:
        if path.is_dir():
            self._read(path)
        else:
            self._accept(path)

    def _accept(self, path: Path) -> None:
        self.close(path)
        if self.on_select:
            self.on_select(path)

    def up(self) -> None:
        """Navigate to the parent directory."""
        self._read(self.directory.parent)

    def submit(self) -> None:
        """Validate the typed path or selected entry and return it."""
        typed = self.name.value.strip()
        path: Path | None
        if typed:
            try:
                path = (self.directory / Path(typed).expanduser()).resolve()
            except (OSError, RuntimeError) as error:
                self.error.text = str(error)
                return
        elif self.mode == 'directory':
            path = self.listing.value if self.focused is self.listing else self.directory
            if path is None or path.name == '..':
                path = self.directory
        else:
            path = self.listing.value
        try:
            if path is None:
                self.error.text = Message('Choose a file or enter a path')
                return
            if path.is_dir() and self.mode != 'directory':
                self._read(path)
                return
            if ((self.mode == 'open' and path.is_file())
                    or (self.mode == 'save' and path.parent.is_dir() and path.name)
                    or (self.mode == 'directory' and path.is_dir())):
                self._accept(path)
            else:
                self.error.text = Message('The selected path is unavailable')
        except OSError as error:
            self.error.text = str(error)


class DirectoryDialog(FileDialog):
    """A file selector restricted to directories."""

    def __init__(self, title: str = Message('Choose directory'), **options):
        """Open a directory selector."""
        super().__init__(title, mode='directory', **options)


@dataclass(frozen=True)
class HelpTopic:
    """A context-help page with links to related topics."""

    title: str
    text: str
    links: tuple[str, ...] = ()


class HelpBook:
    """Context-sensitive help topics displayed in a standard modal viewer."""

    def __init__(self, topics: Mapping[str, HelpTopic], start: str):
        """Store topics and their default page."""
        self.topics = dict(topics)
        if start not in self.topics:
            raise ValueError('The starting help topic must exist')
        self.start = start

    def install(self, app: Application) -> None:
        """Handle F1 and Ctrl+H using the active window's help context."""
        def help_for_window() -> None:
            window = app.active_window
            topic = window.help_topic if window is not None else None
            self.open(app, topic)

        app.on_help = help_for_window

    def open(self, app: Application, topic: str | None = None) -> Dialog:
        """Open a page with related topics and a Back command."""
        dialog: Dialog
        current = topic if topic in self.topics else self.start
        visited: list[str] = []

        def show(key: str) -> None:
            nonlocal current
            current = key
            page = self.topics[key]
            dialog.title = Message('Help · {title}', title=page.title)
            body.text = page.text
            body.top = body.left = 0
            links.set_items(link for link in page.links if link in self.topics)
            links.select(0)
            links.visible = bool(links.items)

        def follow(key: str) -> None:
            visited.append(current)
            show(key)

        def back() -> None:
            if visited:
                show(visited.pop())

        body = TextView(grow=1, height=5)
        links = ListBox(label=lambda key: self.topics[key].title,
                        on_activate=follow, height=3)
        actions = Stack(Button(Message('Back'), back),
                        Button(Message('Close'), lambda: dialog.close()),
                        horizontal=True, gap=1)
        dialog = Dialog(Message('Help'), Stack(body, links, actions), Rect(0, 0, 64, 16))
        show(current)
        app.add(dialog)
        return dialog


class ThemeDialog(Dialog):
    """Edit a desktop's semantic palette one foreground or background at a time."""

    def __init__(self, app: Application):
        """Create a color editor with a live preview and a reversible Cancel action."""
        self.desktop = app
        self.original = app.theme
        self._followed_display = app.follows_display_appearance
        self.roles = ListBox(app.theme.roles, label=Message, height=5, grow=1,
                             on_change=lambda _: self._sync())
        self.channels = RadioGroup((Message('Foreground'), Message('Background')),
                                   on_change=lambda _: self._sync())
        self.rgb = InputLine(validator=RegexValidator(
            r'#[0-9a-fA-F]{6}|', Message('Enter #RRGGBB or leave blank for the terminal default')),
            on_submit=lambda _: self.preview())
        actions = Stack(Button(Message('Preview'), self.preview), Button(Message('Apply'), self.apply),
                        Button(Message('Cancel'), self.close), horizontal=True, gap=1)
        super().__init__(Message('Appearance'), Stack(self.roles, self.channels, self.rgb, actions),
                         Rect(0, 0, 50, 15), default=self.preview, on_close=self._closed)
        self._sync()

    def _sync(self) -> None:
        role = self.roles.value
        if role is not None:
            style = self.desktop.theme[role]
            self.rgb.value = (style.bg if self.channels.index else style.fg) or ''

    def preview(self) -> None:
        """Preview the current color in the desktop."""
        error = self.rgb.validate()
        if error:
            self.desktop.set_status(error, error=True)
        else:
            role = self.roles.value
            if role is not None:
                style = self.desktop.theme[role]
                value = self.rgb.value or None
                new_style = (replace(style, bg=value) if self.channels.index
                             else replace(style, fg=value))
                self.desktop.theme = self.desktop.theme.with_styles(**{role: new_style})
                self.desktop.set_status('')

    def apply(self) -> None:
        """Keep all previewed colors and close the dialog."""
        self.preview()
        if not self.rgb.validate():
            self.close(self.desktop.theme)

    def _closed(self, result: object) -> None:
        if not isinstance(result, Theme):
            self.desktop.theme = None if self._followed_display else self.original
            self.desktop.set_status('')
