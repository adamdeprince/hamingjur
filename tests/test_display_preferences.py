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
    update = silkie.command('display', 'event', t=40, appearance='dark', high_contrast=0,
                            differentiate_without_color=1, languages='fr-CA,en-US')
    reply = silkie.command('display', 'cap', id=1, mode=0, appearance='light',
                           high_contrast=1, differentiate_without_color=0, languages='en-US,fr-CA')
    term.ungetch('x' + update + reply + '\x1b[1;1R')
    report = term.display.query(timeout=0)
    assert isinstance(report, DisplayEvent)
    assert report.appearance is DisplayAppearance.LIGHT
    assert report.high_contrast and not report.differentiate_without_color
    assert report.languages == ('en-US', 'fr-CA')
    assert term.display is term.silkie.display
    assert term.inkey(timeout=0) == 'x'
    event = term.inkey(timeout=0).protocol_event
    assert isinstance(event, DisplayEvent) and event.appearance is DisplayAppearance.DARK
    assert event.differentiate_without_color and not event.high_contrast
    assert event.languages == ('fr-CA', 'en-US')


@pytest.mark.parametrize('fields,languages', [
    ({}, ()),
    ({'high_contrast': '2', 'differentiate_without_color': 'yes', 'languages': 'und'}, ('und',)),
    ({'languages': ',en-US,??,zh-Hant-TW,'}, ('en-US', 'zh-Hant-TW')),
])
def test_display_report_preference_defaults(fields, languages):
    """Default missing or unknown flags and retain the ordered, valid language tags."""
    report = parse_event(silkie.command('display', 'event', **fields))
    assert not report.high_contrast and not report.differentiate_without_color
    assert report.languages == languages


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


def test_high_contrast_uses_pure_black_and_white(term):
    """Repaint the desktop and windows in high contrast when the appearance changes."""
    app = Application(term)
    app.add(Window('Preferences', Button('Done', app.stop)))
    for appearance, background, channel in (
        ('dark', '#000000', 0),
        ('light', '#FFFFFF', 255),
        ('dark', '#000000', 0),
    ):
        previous = len(term.stream.getvalue())
        app.dispatch(parse_event(silkie.command(
            'display', 'event', appearance=appearance, high_contrast=1)))
        assert app.theme['desktop'].bg == app.theme['window'].bg == background
        assert app.theme['window'].fg == ('#FFFFFF' if appearance == 'dark' else '#000000')
        app.draw()
        assert term.on_color_rgb(channel, channel, channel) in term.stream.getvalue()[previous:]


def test_widgets_follow_live_accessibility_hints(term):
    """Use the effective display palette and show errors without depending on hue."""
    app = Application(term)
    app.set_status('Recheck the value', error=True)
    initial = app.theme
    app.draw()
    output = term.stream.getvalue()
    report = parse_event(silkie.command('display', 'event', kind='eink', appearance='dark',
                                       eink_scheme='light', high_contrast=1,
                                       differentiate_without_color=1, languages='en-US'))
    assert app.dispatch(report) and app.display_report is report
    palette = app.theme
    assert palette['window'].bg == '#FFFFFF'
    assert palette['focus'].bg == '#000000'
    assert palette['error'].bg != Theme.high_contrast(light=True)['error'].bg
    assert all(len(set(bytes.fromhex(app.theme[role].bg[1:]))) == 1 for role in app.theme.roles)
    app.draw()
    assert 'Error: Recheck the value' in term.stream.getvalue()[len(output):]
    output = term.stream.getvalue()
    app.dispatch(parse_event(silkie.command('display', 'event', kind='eink', appearance='dark',
                                           eink_scheme='light', high_contrast=1,
                                           differentiate_without_color=1, languages='fr-CA')))
    app.draw()
    assert app.display_report.languages == ('fr-CA',)
    assert app.theme is palette
    assert 'Erreur : Recheck the value' in term.stream.getvalue()[len(output):]
    app.dispatch(parse_event(silkie.command('display', 'event', kind='lcd')))
    assert app.theme is initial


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
        first = len(queries) == 1
        return parse_event(silkie.command(
            'display', 'cap', id=len(queries), appearance='light' if first else 'dark',
            high_contrast=int(not first)))

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
        shown.append((app.display_appearance, app.theme['window'].bg))

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
        frames.append(resolve_sequence(silkie.command('display', 'event', appearance='dark',
                                                       high_contrast=1), {}, {}))
    frames.append(Keystroke('', name='KEY_ENTER'))
    events = iter(frames)
    app.run()
    light = DisplayAppearance.LIGHT, Theme(light=True)['window'].bg
    dark_high_contrast = DisplayAppearance.DARK, '#000000'
    if reporting == 'unsupported':
        assert shown == [(DisplayAppearance.NO_PREFERENCE, Theme()['window'].bg)] * 2
        assert modes == [] and len(queries) == 1
    elif reporting == 'granted':
        assert shown == [light, light, dark_high_contrast]
        assert modes == ['enter', 'exit'] and len(queries) == 1
    else:
        assert shown == [light, dark_high_contrast]
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
