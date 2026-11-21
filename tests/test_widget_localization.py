"""Terminal-host language preferences and native screen-reader widget semantics."""
import ast
from contextlib import nullcontext
from pathlib import Path
from string import Formatter
from urllib.parse import unquote

import pytest

from hamingjur import silkie
from hamingjur.dec_modes import DecPrivateMode
from hamingjur.keyboard import Keystroke
from hamingjur.protocols import FRAME_RE, parse_event
from hamingjur.ui import (Application, Button, DirectoryDialog, FormDialog, InputLine, Label,
                          Rect, Theme, Window)
from hamingjur.ui._i18n import Message, catalogs, select_language
from .test_ui import terminal_fixture  # noqa: F401


def _text(app):
    return '\n'.join(''.join(cell[0] for cell in row) for row in app.draw().rows)


def _update(app, languages):
    app.dispatch(parse_event(silkie.command('display', 'event', languages=languages)))


@pytest.mark.parametrize('preferences,selected', [
    (('und', 'xx', 'fr-CA'), 'fr'),
    (('en-GB', 'fr'), 'en-us'),
    (('en-US',), 'en-us'),
    (('pt-BR', 'pt-PT'), 'pt-br'),
    (('pt-Latn-BR',), 'pt-br'),
    (('pt-PT', 'pt-BR'), 'pt'),
    (('zh-HK', 'ja-JP'), 'zh-hant'),
    (('zh-Hans-TW',), 'zh-hans'),
    (('no-NO',), 'nb'),
    ((), 'en-us'),
])
def test_widget_language_preference_order(preferences, selected):
    """Honor ordered host preferences and distinguish writing systems and regional catalogs."""
    assert select_language(preferences) == selected


def test_widget_catalog_covers_macos_base_languages():
    """Cover every base language in Apple's macOS System Language list."""
    apple_languages = set('ar ca cs da de el en es fi fr he hi hr hu id it ja ko ms nb nl '
                          'pl pt ro ru sk sl sv th tr uk vi zh'.split())
    supported = {code.split('-', 1)[0] for code in (*catalogs(), select_language(()))}
    assert apple_languages <= supported
    for language in apple_languages:
        assert select_language((language,)).split('-', 1)[0] == language


def test_locale_catalogs_cover_default_messages_and_format_variables():
    """Every bundled locale has the same message IDs and preserves formatting fields."""
    expected = set(Theme().roles)
    ui = Path(__file__).resolve().parents[1] / 'hamingjur' / 'ui'
    for source in ui.glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'Message' and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                expected.add(node.args[0].value)
    known_fields = {
        key: {field for _, field, _, _ in Formatter().parse(key) if field is not None}
        for key in expected
    }
    for catalog in catalogs().values():
        assert catalog.keys() == expected
        for key, value in catalog.items():
            assert value
            assert {field for _, field, _, _ in Formatter().parse(value)
                    if field is not None} == known_fields[key]


def test_open_widgets_relocalize_and_measure_in_terminal_cells(term, tmp_path):
    """Relabel an open dialog without touching caller text, input state, or focus."""
    app = Application(term)
    directory = DirectoryDialog(directory=tmp_path)
    app.add(directory)
    directory.name.value = 'Cancel'
    assert 'Choose directory' in _text(app)
    assert app.language == 'en-us'
    initial_focus = directory.focused
    for preferences, expected in (
        ('xx,fr-CA', ('Choisir un dossier', '[ Sélectionner ]', '[ Annuler ]')),
        ('ja-JP', ('フォルダを選択', '[ 選択 ]', '[ キャンセル ]')),
        ('und', ('Choose directory', '[ Select ]', '[ Cancel ]')),
    ):
        _update(app, preferences)
        rendered = _text(app)
        assert all(value in rendered for value in expected)
        assert 'Cancel' in rendered and directory.name.value == 'Cancel'
        assert directory.focused is initial_focus


def test_live_native_widget_roles_and_localized_error_announcements(term, monkeypatch):
    """Expose visible semantic spans and announce each error once through a11y.2."""
    modes = []
    monkeypatch.setattr(term.accessibility, 'supported', lambda *, version: version == 2)
    monkeypatch.setattr(term, 'dec_modes_enabled',
                        lambda *args: modes.extend(args) or nullcontext())
    app = Application(term)
    form = FormDialog('Open file', {'Project': InputLine()})
    app.add(form)
    _update(app, 'fr-CA')
    app.set_status(Message('Text not found'), error=True)
    with app.screen_reader():
        canvas = app.draw()
        assert canvas.accessible_cells
        assert any(role == 'prompt' for role in canvas.accessible_cells.values())
        assert any(role == 'status' for role in canvas.accessible_cells.values())
        assert any(role == 'decorative' for role in canvas.accessible_cells.values())
        app.draw()
        overlay = app.add(Window('', Button('Cover', app.stop),
                                 form.screen_bounds, framed=False, closable=False))
        # The modal stays above normal windows. Compare after closing it instead.
        form.close()
        canvas = app.draw()
        assert 'prompt' not in canvas.accessible_cells.values()
        overlay.close()
    events = [parse_event(frame) for frame in FRAME_RE.findall(term.stream.getvalue())]
    assert modes == [DecPrivateMode.DECTCEM]
    assert all(event.protocol == 'silkie:a11y' for event in events)
    announced = [unquote(event.fields['text']) for event in events if event.operation == 'announce']
    assert announced == ['Texte introuvable']
    assert any(event.operation == 'clear-text' for event in events)
    assert any(event.fields.get('role') == 'prompt' for event in events)
    assert not app.draw().accessible_cells


def test_occluded_prompt_spans_are_removed(term, monkeypatch):
    """A later window can cover a prompt without exposing the hidden label to a reader."""
    monkeypatch.setattr(term.accessibility, 'supported', lambda **_: True)
    monkeypatch.setattr(term, 'dec_modes_enabled', lambda *_: nullcontext())
    app = Application(term)
    bounds = Rect(0, 1, 20, 2)
    app.add(Window('', Label('Secret prompt', accessibility_role='prompt'),
                   bounds, minimum=(1, 1), framed=False))
    with app.screen_reader():
        assert any(role == 'prompt' for role in app.draw().accessible_cells.values())
        app.add(Window('', Button('Cover', app.stop), bounds, minimum=(1, 1), framed=False))
        assert 'prompt' not in app.draw().accessible_cells.values()


def test_desktop_run_enables_native_screen_reader_and_host_language(term, monkeypatch):
    """The normal entry point applies host language and accessibility discovery."""
    for name in ('fullscreen', 'cbreak', 'keypad', 'bracketed_paste', 'notify_on_resize'):
        monkeypatch.setattr(term, name, lambda **_: nullcontext())
    term._is_a_tty = True
    monkeypatch.setattr(term.display, 'query', lambda: parse_event(
        silkie.command('display', 'cap', languages='zh-TW')))
    monkeypatch.setattr(term.display, 'enabled', lambda: nullcontext(1))
    monkeypatch.setattr(term.accessibility, 'supported', lambda *, version: version == 2)
    monkeypatch.setattr(term, 'dec_modes_enabled', lambda *_: nullcontext())
    monkeypatch.setattr(term, 'inkey', lambda: Keystroke('', name='KEY_ENTER'))
    app = Application(term, mouse=False)
    app.add(Window('', Button('Exit', app.stop)))
    app.set_status(Message('No input history'))
    app.run()
    output = term.stream.getvalue()
    assert app.language == 'zh-hant' and '沒有輸入記錄' in output
    events = [parse_event(frame) for frame in FRAME_RE.findall(output)]
    assert any(event.fields.get('role') == 'status' for event in events)
    assert any(event.operation == 'clear-text' for event in events)
