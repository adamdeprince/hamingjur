"""Test desktop behavior with real Hamingjur rendering and input decoding."""
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hamingjur.keyboard import Keystroke
from hamingjur.ui import (
    ASCII, UNICODE, Appearance, Application, Button, Checkbox, Dialog, FileDialog,
    FormDialog, InputLine, Label, ListBox, MenuItem, Panel, Pointer, Rect, RegexValidator, Stack,
    Style, TextEditor, Theme, ThemeDialog, TreeNode, TreeView, Window,
)
from .accessories import TestTerminal


def key(name):
    """Return a decoded-style Hamingjur key."""
    return Keystroke('', name='KEY_' + name)


@pytest.fixture(name='term')
def terminal_fixture(monkeypatch):
    """Use a truecolor-capable terminal model with an in-memory screen stream."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('COLORTERM', 'truecolor')
    terminal = TestTerminal(stream=io.StringIO(), kind='xterm-256color', force_styling=True)
    terminal._height_and_width = lambda: SimpleNamespace(ws_col=80, ws_row=24)
    return terminal


def lines(canvas):
    """Extract plain text from the composited cell rows."""
    return [''.join(cell[0] for cell in row) for row in canvas.rows]


@pytest.mark.parametrize('kind,program,corner,shadow', [
    ('vt100', '', '+', False),
    ('xterm-256color', '', '╭', False),
    ('xterm-256color', 'iterm2', '╭', False),
    ('xterm-kitty', '', '╭', True),
    ('xterm-256color', 'silkie', '╭', True),
])
def test_terminal_profiles(term, kind, program, corner, shadow):
    """Terminal profiles use appropriate glyphs and always offer keyboard focus."""
    term._kind = kind
    term.term_program = program
    app = Application(term)
    assert app.appearance.shadow is shadow
    app.add(Window('État', InputLine(), Rect(1, 2, 22, 6)))
    assert lines(app.draw())[2][1] == corner
    output = term.stream.getvalue()
    if kind == 'vt100':
        output.encode('ascii')
        assert 'Etat' in output and '38;2' not in output
        assert term.reverse in output and not app.mouse
    else:
        assert 'État' in output


def test_manual_appearance_and_wide_chrome(term):
    """Square Unicode frames remain selectable and double-width chrome falls back to ASCII."""
    assert Application(term, appearance=UNICODE).appearance.frame[0] == '┌'
    term.ambiguous_width = 2
    appearance = Appearance.for_terminal(term)
    assert appearance.frame == ASCII.frame and not appearance.ascii_only


def test_default_palettes_and_theme_invalidation(term):
    """Themes change semantic styles and send a new frame when only colors change."""
    app = Application(term, theme=Theme.from_mapping({'focus': {'bg': '#C060D0'}}))
    app.add(Window('Colors', Button('Apply', lambda: None)))
    app.draw()
    old = term.stream.getvalue()
    assert '\x1b[48;2;192;96;208m' in old
    app.draw()
    assert term.stream.getvalue() == old
    app.theme = Theme(light=True).with_styles(focus=Style(fg='#FFFFFF', bg='#112244'))
    app.draw()
    assert '\x1b[48;2;17;34;68m' in term.stream.getvalue()[len(old):]


def test_theme_dialog_cancel_reverts_preview(term):
    """Previewed colors are reversible until Apply."""
    app = Application(term)
    original = app.theme
    dialog = ThemeDialog(app)
    app.add(dialog)
    dialog.rgb.value = '#FF00FF'
    dialog.preview()
    assert app.theme['desktop'].fg == '#FF00FF'
    app.dispatch(key('ESCAPE'))
    assert app.theme is original


def test_painting_over_wide_glyph_clears_its_other_cell(term):
    """An overlapping window cannot leave half a Unicode glyph on the display."""
    app = Application(term)
    app.add(Window('Under', Label('界Q'), Rect(1, 2, 16, 8)))
    before = app.draw().rows[3]
    assert before[2][0] == '界' and before[3][0] == ''
    app.add(Window('Over', Label(''), Rect(3, 3, 15, 7)))
    after = app.draw().rows[3]
    assert after[2][0] == ' ' and after[3][0] == '╭'


def test_panel_resize_anchors(term):
    """Absolute cell placement reflows end-anchored and stretching controls."""
    panel = Panel(width=20, height=7)
    text = panel.place(InputLine(), Rect(1, 1, 18, 1), grow_x=True)
    apply = panel.place(Button('Apply', lambda: None), Rect(12, 5, 8, 1),
                        right=True, bottom=True)
    app = Application(term)
    window = Window('Panel', panel, Rect(2, 2, 22, 9))
    app.add(window)
    assert text.bounds.width == 18 and apply.bounds == Rect(15, 8, 8, 1)
    window.bounds = Rect(2, 2, 32, 13)
    app.draw()
    assert text.bounds.width == 28 and apply.bounds == Rect(25, 12, 8, 1)


def test_menu_mnemonics_submenus_and_disabled_actions(term):
    """The menu is navigable with VT100 controls and dispatches only enabled commands."""
    app = Application(term, appearance=ASCII)
    commands = []
    app.menus.menus = (MenuItem.submenu(
        '&File', MenuItem('', enabled=False),
        MenuItem('&Disabled', lambda: commands.append('disabled'), enabled=False),
        MenuItem.submenu('&More', MenuItem('&Open', lambda: commands.append('open'), 'Ctrl+O'))),)
    app.draw()
    app.dispatch('\x07')
    app.dispatch('m')
    app.dispatch('o')
    assert commands == ['open']
    assert not app.menus.open
    app.dispatch(key('CTRL_O'))
    assert commands == ['open', 'open']


def test_modal_focus_validation_and_large_form(term):
    """Fields beyond the viewport scroll into view and receive focus on validation failure."""
    app = Application(term)
    blocked = []
    app.bind('Ctrl+O', lambda: blocked.append(True))
    fields = {f'Field {index}': InputLine('ok') for index in range(20)}
    fields['Final'] = InputLine('', validator=RegexValidator('yes', 'Enter yes'))
    results = []
    form = FormDialog('Configure', fields, on_submit=results.append)
    app.add(form)
    app.draw()
    app.dispatch(key('CTRL_O'))
    assert blocked == []
    app.dispatch(key('ENTER'))
    assert form.focused is fields['Final']
    assert 'Enter yes' in app.status
    screen = lines(app.draw())
    assert any('Final' in row for row in screen)
    app.dispatch('yes')
    app.dispatch(key('ENTER'))
    assert results[0]['Final'] == 'yes' and app.active_window is None


def test_pointer_drag_resize_and_modal_use(term):
    """Dragging captures a window or control and modal dialogs isolate the desktop."""
    app = Application(term)
    checkbox = Checkbox('Select')
    window = Window('Move', checkbox, Rect(3, 3, 25, 8))
    app.add(window)
    app.draw()
    app.dispatch(Pointer(6, 3))
    app.dispatch(Pointer(10, 6, kind='move'))
    app.dispatch(Pointer(10, 6, kind='release'))
    assert (window.screen_bounds.x, window.screen_bounds.y) == (7, 6)
    app.move_window(resize=True)
    app.dispatch(key('RIGHT'))
    app.dispatch(key('DOWN'))
    app.dispatch(key('ENTER'))
    assert window.screen_bounds.width == 26 and window.screen_bounds.height == 9
    app.add(Dialog('Modal', Label('Focus stays here')))
    app.dispatch(Pointer(8, 7))
    assert not checkbox.checked
    app.dispatch(key('ESCAPE'))
    app.dispatch(Pointer(9, 7))
    assert checkbox.checked


def test_tile_and_cascade(term):
    """Tiling keeps the active window while arranging nonoverlapping rectangles."""
    app = Application(term)
    windows = [Window(str(index), Label('body')) for index in range(12)]
    for window in windows:
        app.add(window)
    app.tile()
    for index, first in enumerate(windows):
        for second in windows[index + 1:]:
            intersection = first.screen_bounds.intersection(second.screen_bounds)
            assert intersection.width * intersection.height == 0
    assert app.active_window is windows[-1]
    app.cascade()
    assert windows[0].screen_bounds.x < windows[1].screen_bounds.x


def test_file_dialog_selection(term, tmp_path):
    """The file picker enters directories, respects filters, and returns a selected path."""
    sub = tmp_path / 'sub'
    sub.mkdir()
    path = sub / 'note.txt'
    path.write_text('Example', encoding='utf-8')
    (sub / 'hidden.md').write_text('skip', encoding='utf-8')
    app = Application(term)
    selected = []
    dialog = FileDialog(directory=tmp_path, pattern='*.txt', on_select=selected.append)
    app.add(dialog)
    app.draw()
    app.dispatch('s')
    app.dispatch(key('ENTER'))
    assert dialog.directory == sub
    assert dialog.listing.items == [sub / '..', path]
    app.dispatch('n')
    app.dispatch(key('ENTER'))
    assert selected == [path]


def test_unicode_editor_undo_search_and_file_line_endings(term, tmp_path):
    """The editor handles full graphemes, large buffers and undo, preserving CRLF on save."""
    app = Application(term)
    path = tmp_path / 'note.txt'
    path.write_bytes('A👩‍💻B\r\nSecond line\r\n'.encode('utf-8'))
    editor = TextEditor()
    editor.load(path)
    app.add(Window('Editor', editor))
    app.draw()
    app.dispatch(key('END'))
    app.dispatch(key('LEFT'))
    app.dispatch(key('BACKSPACE'))
    assert editor.lines[0] == 'AB'
    app.dispatch(key('CTRL_Z'))
    assert editor.lines[0] == 'A👩‍💻B'
    assert editor.find('Second line')
    app.dispatch(key('CTRL_C'))
    assert app.clipboard == 'Second line'
    text = 'Sample ' * 16000
    editor.replace_selection(text)
    editor.save()
    expected = ('A👩‍💻B\r\n' + text + '\r\n').encode('utf-8')
    assert path.read_bytes() == expected
    assert not editor.modified
    editor.undo()
    assert editor.modified and editor.selected_text == 'Second line'


def test_tree_keyboard_and_list_search(term):
    """Expanded outline items remain selectable and long lists support incremental search."""
    app = Application(term)
    leaf = TreeNode('Leaf')
    root = TreeNode('Root', (leaf,))
    tree = TreeView((root,))
    collection = ListBox((f'Item {i}' for i in range(2000)))
    app.add(Window('Views', Stack(tree, collection)))
    app.dispatch(key('RIGHT'))
    app.dispatch(key('RIGHT'))
    assert tree.selection is leaf
    app.dispatch(key('LEFT'))
    assert tree.selection is root
    app.dispatch(key('TAB'))
    app.dispatch('Item 1999')
    assert collection.value == 'Item 1999' and collection.offset > 1900


def test_terminal_modes_restored_when_callback_raises(term, monkeypatch):
    """The event loop unwinds every scoped terminal mode on application errors."""
    app = Application(term)
    active = []

    @contextmanager
    def mode(name):
        active.append(name)
        try:
            yield
        finally:
            assert active.pop() == name

    def fail():
        raise RuntimeError('callback failed')

    methods = ('fullscreen', 'cbreak', 'keypad', 'hidden_cursor', 'mouse_enabled',
               'bracketed_paste', 'notify_on_resize')
    for method in methods:
        monkeypatch.setattr(term, method, lambda value=method, **_: mode(value))
    monkeypatch.setattr(term, '_is_a_tty', True)
    monkeypatch.setattr(term, 'inkey', lambda: key('ENTER'))
    app.add(Window('Test', Button('Fail', fail)))
    with pytest.raises(RuntimeError, match='callback failed'):
        app.run()
    assert active == []
