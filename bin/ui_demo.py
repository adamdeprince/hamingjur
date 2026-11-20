#!/usr/bin/env python3
"""Explore the Hamingjur widget library in one interactive desktop."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from hamingjur import Terminal
from hamingjur.keyboard import Keystroke
from hamingjur.line_editor import LineHistory
from hamingjur.silkie import ImageAnnotation
from hamingjur.ui import (
    ASCII, ROUNDED, UNICODE, Appearance, Application, Button, Checkbox, CheckGroup,
    Dialog, DirectoryDialog, FileDialog, FormDialog, Group, HelpBook, HelpTopic,
    ImageView, InputLine, IntegerValidator, Label, ListBox, MenuItem, Panel, Pointer,
    ProgressBar, RadioGroup, Rect, RegexValidator, ScrollBar, ScrollView, SplitView,
    Stack, StatusItem, Surface, TextEditor, TextView, Theme, ThemeDialog, TreeNode,
    TreeView, Widget, Window, key_name, message_box, prompt,
)


HELP = {
    'desktop': HelpTopic(
        'Demo All',
        'Arrow keys navigate the tree. Right expands and Left collapses branches.\n'
        'Enter or double-click opens the selected demo. Tab changes focus.\n'
        'Ctrl+G opens the menus and Ctrl+Q quits.\n\n'
        'Drag a window title or bottom-right grip to move or resize it.\n'
        'The Window menu also offers keyboard move, resize and arrangement.',
        ('controls', 'views', 'layout', 'editor', 'images')),
    'controls': HelpTopic(
        'Controls',
        'Tab to the completion track. Arrow keys, Page Up/Down and dragging\n'
        'move the thumb and update the progress bar.\n'
        'Enter submits a project name. Ctrl+R opens saved names.\n'
        'Tab or scroll down for radio buttons, checkboxes and Apply.\n'
        'Open the Profile form to try both validators and a masked field.',
        ('desktop',)),
    'views': HelpTopic(
        'Lists and text',
        'Type a station name prefix to jump to its row. Enter or double-click\n'
        'opens the report. The preview updates as the selection moves.\n'
        'Tab to the preview to scroll with arrows and Page Up/Down.\n'
        'Drag the divider, or focus it and use Up and Down.',
        ('desktop',)),
    'layout': HelpTopic(
        'Layout and pointer',
        'Tab to the marker, then move it with the arrow keys or drag it.\n'
        'Space or right-click opens its context menu.\n'
        'Resize the window to see the marker area grow and the button\n'
        'stay anchored to the bottom-right corner.',
        ('desktop',)),
    'editor': HelpTopic(
        'Editor',
        'Ctrl+O opens a file. Ctrl+S saves. Ctrl+F finds text.\n'
        'F3 repeats the search. Edit > Replace all changes every match.\n'
        'Shift+arrows select, Ctrl+Z undoes and Ctrl+Y redoes.\n'
        'Ctrl+C, Ctrl+X and Ctrl+V use the desktop clipboard.',
        ('desktop',)),
    'images': HelpTopic(
        'Images',
        'Enter on the chart opens its full text description.\n'
        'View > Review visible images lists the accessible image content.\n'
        'Move or resize windows to see how overlapping views clip the chart.',
        ('desktop',)),
}


def show_window(app: Application, window: Window) -> None:
    """Reopen a closed demo or raise its existing window."""
    if window.app is None:
        app.add(window)
    else:
        app.activate(window)


class Workbench:
    """Provide an example document and the editor's file and search commands."""

    def __init__(self, app: Application):
        """Start with a sample document and use the current directory for file dialogs."""
        self.app = app
        self.directory = Path.cwd()
        self.editor = TextEditor(
            'Hamingjur Workbench\n\n'
            'Try finding and replacing the word terminal.\n'
            'Edit this terminal document with the keyboard or mouse.\n'
            'Try graphemes such as café, 東京, and 👩🏽‍💻.\n')
        self.window = Window('Workbench', self.editor, Rect(2, 2, 58, 17),
                             help_topic='editor')

    def _confirm_discard(self, action: Callable[[], None]) -> None:
        if self.editor.modified:
            message_box(self.app, 'Unsaved edits', 'Discard changes to the current document?',
                        choices=('Keep editing', 'Discard'),
                        on_close=lambda answer: action() if answer == 'Discard' else None)
        else:
            action()

    def open_file(self) -> None:
        """Choose a file and load it after resolving any unsaved edits."""
        self.app.add(FileDialog(directory=self.directory, on_select=self._open_path))

    def _open_path(self, path: Path) -> None:
        self._confirm_discard(partial(self._load, path))

    def _load(self, path: Path) -> None:
        try:
            self.editor.load(path)
        except (OSError, UnicodeError) as error:
            message_box(self.app, 'Open file', str(error))
            return
        self.directory = path.parent
        self.window.title = path.name
        show_window(self.app, self.window)
        self.app.set_status(f'Opened {path.name}')

    def choose_directory(self) -> None:
        """Change the starting directory for file dialogs."""
        def choose(path: Path) -> None:
            self.directory = path
            self.app.set_status(f'Working directory set to {path}')

        self.app.add(DirectoryDialog(directory=self.directory, on_select=choose))

    def _save(self, path: Path) -> None:
        try:
            self.editor.save(path)
        except (OSError, UnicodeError) as error:
            message_box(self.app, 'Save file', str(error))
            return
        self.directory = path.parent
        self.window.title = path.name
        self.app.set_status(f'Saved {path.name}')

    def save_file(self) -> None:
        """Save the current document or ask for a path."""
        if self.editor.path is None:
            self.save_as()
        else:
            self._save(self.editor.path)

    def save_as(self) -> None:
        """Choose a save path and confirm replacement of existing files."""
        def choose(path: Path) -> None:
            if path.exists():
                message_box(self.app, 'Replace file', f'Replace {path.name}?',
                            choices=('Cancel', 'Replace'),
                            on_close=lambda answer: self._save(path)
                            if answer == 'Replace' else None)
            else:
                self._save(path)

        dialog = FileDialog('Save file as', directory=self.directory, mode='save', on_select=choose)
        dialog.name.value = self.editor.path.name if self.editor.path else 'workbench.txt'
        self.app.add(dialog)
        dialog.focused = dialog.name

    def find(self) -> None:
        """Prompt for a phrase and select its next occurrence."""
        def find_text(text: str) -> None:
            show_window(self.app, self.window)
            if self.editor.find(text):
                self.app.set_status(f'Found {text!r}')
            else:
                self.app.set_status('Text not found', error=True)

        prompt(self.app, 'Find in document', 'Search text', on_submit=find_text)

    def replace_all(self) -> None:
        """Replace matching text in a single undoable edit."""
        def replace_text(values: dict[str, str]) -> None:
            count = self.editor.replace_all(values['Find'], values['Replace with'])
            show_window(self.app, self.window)
            self.app.set_status(f'Replaced {count} occurrence(s)')

        self.app.add(FormDialog('Replace all', {
            'Find': InputLine(),
            'Replace with': InputLine(),
        }, on_submit=replace_text))

    def quit(self) -> None:
        """Leave the desktop after checking for unsaved edits."""
        self._confirm_discard(self.app.stop)


def project_validator() -> RegexValidator:
    """Accept project names suitable for the demo's profile."""
    return RegexValidator(r'[A-Za-z][A-Za-z0-9 -]*',
                          'Start with a letter. Use A-Z, 0-9, space or -.')


def show_profile(app: Application, project: InputLine) -> None:
    """Exercise form validation and password masking with sample profile fields."""
    def submit(values: dict[str, str]) -> None:
        project.value = values['Project name']
        project.editor.history.add(project.value)
        app.set_status(f'{project.value} profile set to {values["Target (%)"]}%')

    app.add(FormDialog('Profile', {
        'Project name': InputLine(project.value, validator=project_validator()),
        'Target (%)': InputLine('75', validator=IntegerValidator(0, 100)),
        'Sample passphrase': InputLine(password=True),
    }, on_submit=submit, bounds=Rect(0, 0, 52, 13)))


def controls_window(app: Application, project: InputLine, starting_theme: Theme | None) -> Window:
    """Build a scrollable gallery of inputs, selection controls and progress."""
    progress = ProgressBar(0.25)

    def complete(value: int) -> None:
        progress.value = value / 100

    track = ScrollBar(total=101, page=1, value=25, horizontal=True, on_change=complete)
    options = CheckGroup(('Build', 'Test', 'Document'), checked=('Build', 'Test'))
    summary = Label('Selected steps: Build, Test', role='muted')

    def apply_steps() -> None:
        summary.text = 'Selected steps: ' + (', '.join(options.values) or 'None')
        app.set_status(summary.text)

    def shadows(enabled: bool) -> None:
        app.appearance = replace(app.appearance, shadow=enabled)

    palettes = {'Follow display': None, 'Dark': Theme(), 'Light': Theme(light=True),
                'High contrast': Theme.high_contrast()}
    if starting_theme is not None:
        palettes = {'Starting theme': starting_theme, **palettes}

    def preview(choice: str) -> None:
        app.theme = palettes[choice]

    theme = RadioGroup(palettes, on_change=preview)
    body = Stack(
        Label('Completion (arrow keys or drag)', role='muted'),
        track, progress,
        Label('Project name (Enter submits, Ctrl+R recalls)', role='muted'), project,
        Button('Open profile form', partial(show_profile, app, project)),
        Label('Palette', role='muted'), theme,
        Checkbox('Window shadows', app.appearance.shadow, on_change=shadows),
        Label('Build steps', role='muted'), options,
        Button('Apply steps', apply_steps), summary,
        gap=1,
    )
    return Window('Controls', ScrollView(body), Rect(5, 3, 53, 18), help_topic='controls')


STATIONS = (
    ('Alder', 12, 34), ('Birch', 9, 63), ('Cedar', 17, 57), ('Dogwood', 11, 94),
    ('Elm', 13, 42), ('Fir', 15, 58), ('Hawthorn', 18, 41), ('Juniper', 14, 70),
    ('Larch', 13, 35), ('Maple', 19, 47), ('Pine', 8, 89), ('Willow', 12, 93),
)


def views_window(app: Application) -> Window:
    """Link a searchable list to a read-only preview and a modal report."""
    report = TextView(grow=1)

    def details(station: tuple[str, int, int]) -> str:
        name, temperature, humidity = station
        return (f'{name} station\n\n'
                f'Temperature: {temperature} °C\n'
                f'Relative humidity: {humidity}%\n\n'
                f'Dew point: approximately {temperature - (100 - humidity) / 5:.1f} °C\n'
                f'Sensor check: {"Inspect for condensation" if humidity > 90 else "Normal"}\n'
                '\nMeasurements are sample data.')

    def preview(station: tuple[str, int, int]) -> None:
        report.text = details(station)
        report.top = report.left = 0

    def open_report(station: tuple[str, int, int]) -> None:
        message_box(app, station[0] + ' report', details(station))

    listing = ListBox(STATIONS, label=lambda station: station[0],
                      on_change=preview, on_activate=open_report)
    preview(STATIONS[0])
    body = Group(Label('Type a station name or use the arrow keys.', role='muted'),
                 SplitView(listing, report, horizontal=False, grow=1))
    window = Window('Lists and text', body, Rect(10, 4, 61, 11), help_topic='views')
    window.focused = listing
    return window


class Marker(Widget):
    """Draw a movable marker and handle keyboard and pointer input."""

    focusable = True

    def __init__(self):
        """Start with the marker centered."""
        super().__init__()
        self.position: tuple[int, int] | None = None

    def _position(self) -> tuple[int, int]:
        width, height = self.bounds.width, self.bounds.height
        x, y = self.position if self.position is not None else (width // 2, height // 2)
        return min(max(0, width - 1), max(0, x)), min(max(0, height - 1), max(0, y))

    def center(self) -> None:
        """Return the marker to the center of its current layout."""
        self.position = None

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw a single-cell marker inside a shaded area."""
        surface.fill('control')
        x, y = self._position()
        surface.write(x, y, '+', 'focus' if focused is self else 'accent')

    def _menu(self, app: Application, x: int, y: int) -> None:
        app.menus.popup((MenuItem('&Center marker', self.center),), x, y)

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Move one cell with an arrow key or open the context menu with Space."""
        name = key_name(key)
        x, y = self._position()
        steps = {'LEFT': (-1, 0), 'RIGHT': (1, 0), 'UP': (0, -1), 'DOWN': (0, 1)}
        if name in steps:
            dx, dy = steps[name]
            self.position = x + dx, y + dy
        elif name == 'SPACE':
            self._menu(app, self.bounds.x + x, self.bounds.y + y)
        else:
            return False
        return True

    def on_pointer(self, pointer: Pointer, app: Application) -> bool:
        """Move the marker with the left button or open the menu with the right."""
        if pointer.button == 'RIGHT' and pointer.kind == 'press':
            self._menu(app, pointer.x, pointer.y)
            return True
        if pointer.button == 'LEFT' and pointer.kind in {'press', 'double', 'move'}:
            self.position = pointer.x - self.bounds.x, pointer.y - self.bounds.y
            return True
        return False


def layout_window() -> Window:
    """Demonstrate a stretching area and a bottom-right anchored button."""
    panel = Panel(width=51, height=11)
    marker = Marker()
    center = Button('Center marker', marker.center)
    button_x = panel.preferred_width - center.preferred_width
    panel.place(Label('Arrows or drag move the marker.', role='muted'), Rect(0, 0, 51, 1),
                grow_x=True)
    panel.place(marker, Rect(0, 1, 51, 9), grow_x=True, grow_y=True)
    panel.place(Label('Space or right-click opens menu.', role='muted'),
                Rect(0, 10, button_x - 1, 1), bottom=True, grow_x=True)
    panel.place(center, Rect(button_x, 10, center.preferred_width, 1),
                right=True, bottom=True)
    return Window('Layout and pointer', panel, Rect(11, 5, 53, 13), help_topic='layout')


def chart_window() -> Window:
    """Draw an original raster chart with an accessible description."""
    image = Image.new('RGB', (240, 108), '#101720')
    drawing = ImageDraw.Draw(image)
    for index, value in enumerate((30, 60, 45)):
        x = 26 + index * 70
        drawing.rectangle((x, 84 - value, x + 38, 84), fill='#8EF0D2')
        drawing.text((x, 89), ('Mon', 'Tue', 'Wed')[index], fill='#FFFFFF')
    annotation = ImageAnnotation('Requests by day', 'Tuesday had the most requests.',
                                 'Monday: 30\nTuesday: 60\nWednesday: 45', 'essential')
    return Window('Chart', ImageView(image, annotation), Rect(14, 4, 43, 12), help_topic='images')


def launcher(groups: dict[str, tuple[tuple[str, Callable[[], None]], ...]]) -> Window:
    """Build an expandable index with keyboard and mouse entry points."""
    def run_item(node: TreeNode) -> None:
        if callable(node.value):
            node.value()

    outline = TreeView(
        (TreeNode(title, tuple(TreeNode(label, value=action) for label, action in entries),
                  expanded=True) for title, entries in groups.items()),
        on_select=run_item,
    )
    outline.select(1)
    instructions = TextView(
        'Choose a demo and press Enter.\n'
        'Right and Left open or close branches.\n\n'
        'Ctrl+G opens the menus.\n'
        'Tab moves focus.\n'
        'F1 opens context help.\n'
        'Ctrl+Q quits.\n\n'
        'Resize or tile the windows to\n'
        'try different terminal sizes.')
    window = Window('Demo All', SplitView(outline, instructions, fraction=0.43),
                    Rect(0, 0, 68, 17), help_topic='desktop')
    window.focused = outline
    return window


def list_windows(app: Application) -> None:
    """Choose which open window to bring to the front."""
    def activate(window: Window) -> None:
        dialog.close()
        app.activate(window)

    listing = ListBox(app.windows, label=lambda window: window.title, on_activate=activate)
    dialog = Dialog('Windows', listing)
    app.add(dialog)


def desktop(theme: Theme | None, appearance: Appearance | None = None, *,
            terminal: Terminal | None = None) -> Application:
    """Build the full demo with a launcher and reusable modeless windows."""
    app = Application(terminal, theme=theme, appearance=appearance)
    editor = Workbench(app)
    history = LineHistory()
    for value in ('Workbench', 'Terminal Lab', 'Cedar Station'):
        history.add(value)
    project = InputLine('Workbench', history=history, validator=project_validator(),
                        on_submit=lambda value: app.set_status(f'Project name set to {value}'))
    show_editor = partial(show_window, app, editor.window)
    show_controls = partial(show_window, app, controls_window(app, project, theme))
    show_views = partial(show_window, app, views_window(app))
    show_layout = partial(show_window, app, layout_window())
    show_chart = partial(show_window, app, chart_window())
    profile = partial(show_profile, app, project)

    def appearance_dialog() -> None:
        app.add(ThemeDialog(app))

    index = launcher({
        'Widgets': (('Controls', show_controls), ('Lists and text', show_views),
                    ('Layout and pointer', show_layout), ('Chart', show_chart)),
        'Workbench': (('Text editor', show_editor), ('Open file', editor.open_file),
                      ('Choose directory', editor.choose_directory)),
        'Dialogs': (('Profile', profile), ('Theme editor', appearance_dialog)),
    })
    show_index = partial(show_window, app, index)
    book = HelpBook(HELP, 'desktop')
    book.install(app)
    context_help = app.on_help
    assert context_help is not None
    app.status_items = (
        StatusItem('Ctrl+G', 'Menu', app.menus.show),
        StatusItem('F1', 'Help', context_help),
        StatusItem('Ctrl+Q', 'Quit', editor.quit),
    )
    app.menus.menus = (
        MenuItem.submenu('&Demo', MenuItem('Demo &All', show_index),
                         MenuItem('&Controls', show_controls),
                         MenuItem('&Lists and text', show_views),
                         MenuItem('Layout and &pointer', show_layout)),
        MenuItem.submenu('&File', MenuItem('&Open…', editor.open_file, 'Ctrl+O'),
                         MenuItem('&Save', editor.save_file, 'Ctrl+S'),
                         MenuItem('Save &as…', editor.save_as),
                         MenuItem('Choose &directory…', editor.choose_directory), MenuItem(''),
                         MenuItem('&Quit', editor.quit, 'Ctrl+Q')),
        MenuItem.submenu('&Edit', MenuItem('Text &editor', show_editor),
                         MenuItem('&Find…', editor.find, 'Ctrl+F'),
                         MenuItem('&Replace all…', editor.replace_all),
                         MenuItem('&Profile…', profile),
                         MenuItem('&Theme editor…', appearance_dialog)),
        MenuItem.submenu('&View', MenuItem('Sample &chart', show_chart),
                         MenuItem('&Review visible images', app.review_images)),
        MenuItem.submenu('&Window', MenuItem('&Next', app.next_window, 'F6'),
                         MenuItem('&List…', partial(list_windows, app)),
                         MenuItem.submenu('&Arrange', MenuItem('&Tile', app.tile),
                                          MenuItem('&Cascade', app.cascade)),
                         MenuItem('&Move', app.move_window, 'F7'),
                         MenuItem('&Resize', partial(app.move_window, resize=True), 'F8'),
                         MenuItem('Ma&ximize', app.maximize, 'F5')),
        MenuItem.submenu('&Help', MenuItem('&Context help', context_help, 'F1'),
                         MenuItem('&Demo guide', partial(book.open, app, 'desktop'))),
    )
    app.add(editor.window)
    app.add(index, center=True)
    return app


def main() -> None:
    """Apply a palette and appearance before starting the terminal event loop."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme', default='auto',
                        help='auto, dark, light, high-contrast, or a JSON palette file')
    parser.add_argument('--appearance', default='auto',
                        choices=('auto', 'ascii', 'unicode', 'rounded'))
    args = parser.parse_args()
    if args.theme == 'auto':
        theme = None
    elif args.theme == 'high-contrast':
        theme = Theme.high_contrast()
    elif args.theme in {'dark', 'light'}:
        theme = Theme(light=args.theme == 'light')
    else:
        theme = Theme.from_mapping(json.loads(Path(args.theme).read_text(encoding='utf-8')))
    looks = {'auto': None, 'ascii': ASCII, 'unicode': UNICODE, 'rounded': ROUNDED}
    app = desktop(theme, looks[args.appearance])
    # Deliver Ctrl+S and Ctrl+Q even when the tty enables flow control.
    with app.terminal.raw():
        app.run()


if __name__ == '__main__':
    main()
