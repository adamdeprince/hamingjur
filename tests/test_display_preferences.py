"""Silkie display preferences over the terminal stream and in Hamingjur widgets."""
from contextlib import contextmanager, nullcontext

import pytest

from hamingjur import silkie
from hamingjur.keyboard import Keystroke, resolve_sequence
from hamingjur.protocols import DisplayAppearance, DisplayEvent, parse_event
from hamingjur.ui import ASCII, Application, Button, Theme, ThemeDialog, Window
from .test_ui import terminal_fixture  # noqa: F401


@pytest.mark.parametrize('operation', ['cap', 'event'])
@pytest.mark.parametrize('field,expected', [
    ('light', DisplayAppearance.LIGHT),
    ('dark', DisplayAppearance.DARK),
    ('no_preference', DisplayAppearance.NO_PREFERENCE),
    ('future_preference', DisplayAppearance.NO_PREFERENCE),
    (None, DisplayAppearance.NO_PREFERENCE),
])
def test_display_report_appearance(operation, field, expected):
    """Decode valid, missing and unknown preferences while preserving the raw fields."""
    appearance = {} if field is None else {'appearance': field}
    raw = silkie.command('display', operation, red_bits=5, output_bits=16, **appearance)
    report = parse_event(raw)
    assert isinstance(report, DisplayEvent)
    assert report.appearance is expected
    assert report.fields.get('appearance') == field
    assert report.integer('red_bits') == 5 and report.raw == raw


def test_terminal_query_returns_typed_report_and_preserves_input(term):
    """The typed query correlates its response without consuming input or change reports."""
    term._is_a_tty = True
    term._keyboard_fd = None
    update = silkie.command('display', 'event', t=40, appearance='dark')
    reply = silkie.command('display', 'cap', id=1, mode=0, appearance='light')
    term.ungetch('x' + update + reply + '\x1b[1;1R')
    report = term.display.query(timeout=0)
    assert isinstance(report, DisplayEvent)
    assert report.appearance is DisplayAppearance.LIGHT
    assert term.display is term.silkie.display
    assert term.inkey(timeout=0) == 'x'
    assert term.inkey(timeout=0).protocol_event.appearance is DisplayAppearance.DARK


def test_widgets_follow_display_and_repaint_with_unchanged_dimensions(term):
    """A light/dark report updates the next frame while a fixed theme takes precedence."""
    app = Application(term)
    app.draw()
    original = app.theme
    output = term.stream.getvalue()
    change = resolve_sequence(silkie.command('display', 'event', t=42, appearance='light'),
                              {}, {})
    assert app.dispatch(change)
    light = app.theme
    assert light['window'] == Theme(light=True)['window']
    app.draw()
    assert term.stream.getvalue() != output
    output = term.stream.getvalue()
    app.dispatch(change)
    app.draw()
    assert term.stream.getvalue() == output and app.theme is light

    custom = Theme.high_contrast()
    app.theme = custom
    app.dispatch(parse_event(silkie.command('display', 'event', appearance='no_preference')))
    assert app.theme is custom and not app.follows_display_appearance
    app.theme = None
    assert app.theme is original and app.follows_display_appearance


def test_cancel_theme_dialog_resumes_latest_display_preference(term):
    """Cancel restores automatic selection even if the preference changed during preview."""
    app = Application(term)
    dialog = ThemeDialog(app)
    app.add(dialog)
    dialog.rgb.value = '#123456'
    dialog.preview()
    preview = app.theme
    app.dispatch(parse_event(silkie.command('display', 'event', appearance='light')))
    assert app.theme is preview
    dialog.close()
    assert app.follows_display_appearance
    assert app.theme['window'] == Theme(light=True)['window']


def test_widgets_follow_eink_palette_and_restore_os_appearance(term):
    app = Application(term)
    for kind, eink_scheme, appearance, light in (
        ('eink', 'light', 'dark', True),
        ('eink', 'dark', 'light', False),
        ('eink', None, 'dark', True),
        ('eink', 'future_scheme', 'dark', True),
        ('lcd', None, 'dark', False),
    ):
        fields = {'eink_scheme': eink_scheme} if eink_scheme is not None else {}
        report = parse_event(silkie.command('display', 'event', kind=kind, appearance=appearance, **fields))
        expected = DisplayAppearance(eink_scheme) if eink_scheme in ('light', 'dark') else None
        assert report.eink_scheme is expected
        assert app.dispatch(report)
        assert app.display_appearance is DisplayAppearance(appearance)
        assert app.theme['window'] == Theme(light=light)['window']

    override = Theme.high_contrast()
    app.theme = override
    app.dispatch(parse_event(silkie.command('display', 'event', kind='eink', eink_scheme='light')))
    assert app.theme is override


@pytest.mark.parametrize('reporting', ['granted', 'refused', 'no-mode-reply', 'unsupported'])
def test_application_subscribes_or_requeries_on_resize(term, monkeypatch, reporting):
    """Follow live events or query again on Silkie's same-size resize notification."""
    app = Application(term)
    queries, modes, frames = [], [], []
    shown = []

    def query():
        queries.append(True)
        if reporting == 'unsupported':
            return None
        return parse_event(silkie.command(
            'display', 'cap', id=len(queries), appearance='light' if len(queries) == 1 else 'dark'))

    @contextmanager
    def enabled():
        modes.append('enter')
        if reporting == 'no-mode-reply':
            raise RuntimeError('display mode read-back timed out')
        try:
            yield int(reporting == 'granted')
        finally:
            modes.append('exit')

    def draw():
        shown.append(app.display_appearance)

    def get_key():
        return next(events)

    for name in ('fullscreen', 'cbreak', 'keypad', 'hidden_cursor', 'mouse_enabled',
                 'bracketed_paste', 'notify_on_resize'):
        monkeypatch.setattr(term, name, lambda **_: nullcontext())
    term._is_a_tty = True
    monkeypatch.setattr(term.display, 'query', query)
    monkeypatch.setattr(term.display, 'enabled', enabled)
    monkeypatch.setattr(app, 'draw', draw)
    monkeypatch.setattr(term, 'inkey', get_key)
    app.add(Window('Test', Button('Done', app.stop)))
    frames.append(Keystroke('', name='RESIZE_EVENT'))
    if reporting == 'granted':
        frames.append(resolve_sequence(silkie.command('display', 'event', appearance='dark'),
                                       {}, {}))
    frames.append(Keystroke('', name='KEY_ENTER'))
    events = iter(frames)
    app.run()
    if reporting == 'unsupported':
        assert shown == [DisplayAppearance.NO_PREFERENCE] * 2
        assert modes == [] and len(queries) == 1
    elif reporting == 'granted':
        assert shown == [DisplayAppearance.LIGHT, DisplayAppearance.LIGHT, DisplayAppearance.DARK]
        assert modes == ['enter', 'exit'] and len(queries) == 1
    else:
        assert shown == [DisplayAppearance.LIGHT, DisplayAppearance.DARK]
        assert modes == (['enter'] if reporting == 'no-mode-reply' else ['enter', 'exit'])
        assert len(queries) == 2


def test_ascii_desktop_skips_display_probes(term, monkeypatch):
    """The VT100 profile starts without probing a Silkie extension."""
    app = Application(term, appearance=ASCII)
    for name in ('fullscreen', 'cbreak', 'keypad', 'hidden_cursor'):
        monkeypatch.setattr(term, name, nullcontext)
    term._is_a_tty = True
    monkeypatch.setattr(term, 'inkey', lambda: Keystroke('', name='KEY_ENTER'))
    monkeypatch.setattr(term.display, 'query', lambda: pytest.fail('VT100 sent a display query'))
    app.add(Window('Test', Button('Done', app.stop)))
    app.run()
