"""Silkie accessibility wire format and portable desktop accessibility behavior."""
from urllib.parse import unquote

import pytest
from PIL import Image

from hamingjur import silkie
from hamingjur.dec_modes import DecModeResponse, DecPrivateMode
from hamingjur.imaging import ImageSupport, Transfer
from hamingjur.keyboard import Keystroke
from hamingjur.protocols import FRAME_RE, AccessibilityAck, ProtocolEvent, parse_event
from hamingjur.remote_control import PREFIX, RemoteControl, packet
from hamingjur.terminal import WINSZ
from hamingjur.ui import ASCII, Application, ImageView, InputLine, Label, Rect, Theme, Window
from .test_ui import terminal_fixture  # noqa: F401


def test_annotation_utf8_full_replacement_and_large_payload():
    """Preserve growing Unicode metadata and the actual wire identifier ranges."""
    detail = 'Résumé: two lines\n\t50% / 東京\n' * 5000
    annotation = silkie.ImageAnnotation('Café: 50%', 'Overview', detail, 'essential')
    event = parse_event(silkie.annotate_image(0xffffffff, 3, annotation, request_id=0))
    assert event.operation == 'set-image'
    assert event.fields['i'] == str(0xffffffff) and event.fields['id'] == '0'
    assert event.fields['name'] == 'Caf%C3%A9%3A%2050%25'
    assert unquote(event.fields['details']) == detail
    assert parse_event(silkie.annotate_image(1, 3, silkie.ImageAnnotation())).fields == {
        'v': '1', 'op': 'set-image', 'i': '1', 'p': '3',
    }


@pytest.mark.parametrize('fields', [
    {'purpose': 'unclear'}, {'name': 'line\nname'}, {'description': '\x1b]'},
    {'details': '\x9c'}, {'details': '\ud800'},
])
def test_annotation_disallows_invalid_text_or_purpose(fields):
    """Reject malformed requests before they reach the terminal."""
    with pytest.raises(ValueError):
        silkie.ImageAnnotation(**fields)


def test_annotation_correlates_reply_and_keeps_input(term):
    """The bound channel preserves an unrelated reply and ordinary keyboard input."""
    term._is_a_tty = True
    term._keyboard_fd = None
    old_reply = silkie.command('a11y', 'ack', id=77, status='OK')
    reply = silkie.command('a11y', 'ack', id=1, status='ENOENT')
    term.ungetch('x' + old_reply + reply + '\x1b[1;1R')
    result = term.accessibility.clear_image(17, 3, timeout=0)
    assert isinstance(result, AccessibilityAck) and result.status == 'ENOENT'
    assert term.inkey(timeout=0) == 'x'
    assert term.inkey(timeout=0).protocol_event.integer('id') == 77
    assert term.accessibility is term.silkie.accessibility
    assert 'op=clear-image:i=17:p=3:id=1' in term.stream.getvalue()
    term.accessibility.set_image(17, 3, silkie.ImageAnnotation('Chart'), wait=False)
    assert 'op=set-image:i=17:p=3:name=Chart' in term.stream.getvalue()


def test_accessibility_v2_wire_format_and_large_text():
    """Encode one-based spans and preserve long, percent-encoded Unicode text."""
    label = 'Search: café; 東京 50%' * 5000
    event = parse_event(silkie.annotate_text(0xffffffff, (1, 6), (2, 1), role='prompt',
                                           label=label, request_id=0))
    assert {key: value for key, value in event.fields.items() if key != 'label'} == {
        'v': '2', 'op': 'set-text', 'range': '4294967295', 'row': '1', 'column': '6',
        'end_row': '2', 'end_column': '1', 'role': 'prompt',
        'id': '0',
    }
    assert unquote(event.fields['label']) == label
    assert parse_event(silkie.clear_text_annotation(7)).fields == {
        'v': '2', 'op': 'clear-text', 'range': '7',
    }
    assert parse_event(silkie.announce('Search failed', priority='assertive')).fields == {
        'v': '2', 'op': 'announce', 'text': 'Search%20failed', 'priority': 'assertive',
    }


@pytest.mark.parametrize('arguments', [
    {'range_id': 0}, {'range_id': 1 << 32}, {'start': (1, 0)}, {'end': (1, 1)},
    {'end': (1, 1 << 31)}, {'role': 'title'}, {'label': 'line\nbreak'}, {'request_id': 1 << 31},
])
def test_text_annotation_rejects_invalid_fields(arguments):
    """Reject inputs the terminal would ignore or that cannot describe a range."""
    fields = {'range_id': 1, 'start': (1, 1), 'end': (1, 2), 'role': 'status'}
    with pytest.raises(ValueError):
        silkie.annotate_text(**{**fields, **arguments})


@pytest.mark.parametrize('text,priority', [('', 'polite'), ('\x1b', 'polite'), ('hi', 'loud')])
def test_announcement_rejects_invalid_fields(text, priority):
    """Require nonempty, single-line text and a supported priority."""
    with pytest.raises(ValueError):
        silkie.announce(text, priority=priority)


@pytest.mark.parametrize('extensions,supported', [('a11y.1', False), ('a11y.2,a11y.1', True)])
def test_accessibility_version_discovery(term, monkeypatch, extensions, supported):
    """Use the highest advertised version, regardless of advertisement order."""
    caps = silkie.Capabilities(parse_event(silkie.command('caps', 'cap', ext=extensions)))
    monkeypatch.setattr(term.silkie, 'capabilities', lambda *args, **kwargs: caps)
    assert term.accessibility.supported()
    assert term.accessibility.supported(version=2) is supported


def test_text_acknowledgements_match_reply_version_and_id(term):
    """Leave old-version replies, other acknowledgements and input on the stream."""
    term._is_a_tty = True
    term._keyboard_fd = None
    old_reply = silkie.command('a11y', 'ack', id=1, status='OK')
    other_reply = '\x1b]777;a11y;v=2:op=ack:id=77:status=ENOENT\x1b\\'
    reply = '\x1b]777;a11y;status=ENOTSUP:v=2:id=1:op=ack\x07'
    term.ungetch('x' + old_reply + other_reply + reply + '\x1b[1;1R')
    result = term.accessibility.announce('Search failed', timeout=0)
    assert isinstance(result, AccessibilityAck) and result.status == 'ENOTSUP'
    assert 'v=2:op=announce:text=Search%20failed:priority=polite:id=1' in term.stream.getvalue()
    assert term.inkey(timeout=0) == 'x'
    assert term.inkey(timeout=0).protocol_event.raw == old_reply
    assert term.inkey(timeout=0).protocol_event.raw == other_reply
    term.accessibility.set_text(1, (1, 1), (1, 3), role='decorative', wait=False)
    term.accessibility.clear_text(1, wait=False)
    assert term.stream.getvalue().endswith(
        silkie.annotate_text(1, (1, 1), (1, 3), role='decorative') +
        silkie.clear_text_annotation(1))


def test_pane_actions_use_the_terminal_connection(term, monkeypatch):
    """Accessibility actions send Silkie's published payloads over the PTY."""
    import json

    requests = []

    def query(sequence, pattern, timeout):
        requests.append(json.loads(sequence[len(PREFIX):-2]))
        return packet({'ok': True})

    monkeypatch.setattr(term, 'query', query)
    term._preferred_size_cache = WINSZ(24, 80, 640, 384)
    term.remote_control.magnify_pane(3, row=12, column=40)
    assert term._preferred_size_cache is None
    term.remote_control.review_images(include_decorative=True)
    term.remote_control.set_minimum_contrast(40)
    term.remote_control.set_minimum_contrast(None)
    assert [(r['cmd'], r['payload']) for r in requests] == [
        ('action', {'action': 'magnify_pane 3 12 40', 'self': True}),
        ('action', {'action': 'review_all_images', 'self': True}),
        ('set-minimum-contrast', {'value': '40'}),
        ('set-minimum-contrast', {'value': 'default'}),
    ]
    with pytest.raises(ValueError, match='terminal connection'):
        RemoteControl(address='unix:/unused').review_images()


def test_graphics_draw_can_send_nonzero_placement_id(term, monkeypatch):
    """Adaptive raster upload names the placement for later annotation."""
    term.graphics._support = ImageSupport(frozenset({24, 32, 100}), frozenset({Transfer.DIRECT}))
    frames = []

    def acknowledge(sequence, image_id, timeout):
        frames.append(parse_event(sequence))
        return ProtocolEvent('graphics', {'i': str(image_id)}, 'OK')

    monkeypatch.setattr(term.graphics, '_request', acknowledge)
    term.graphics.draw(Image.new('RGB', (1, 1)), width=1, placement_id=3)
    assert frames[0].fields['p'] == '3'


def test_desktop_fallback_and_readable_image_review(term):
    """Image descriptions are accessible with plain text and keyboard on VT100."""
    app = Application(term, appearance=ASCII, theme=Theme.high_contrast())
    raster = Image.new('RGB', (4, 4))
    chart = ImageView(raster, silkie.ImageAnnotation('Chart', 'Value rose', '1 -> 2', 'essential'))
    ornament = ImageView(raster, silkie.ImageAnnotation('Ornament', purpose='decorative'))
    app.add(Window('Chart window', chart, Rect(1, 2, 22, 10)))
    app.add(Window('Ornament window', ornament, Rect(32, 2, 25, 10)))
    canvas = app.draw()
    assert 'Value rose' in ''.join(cell[0] for cell in canvas.rows[4])
    assert '\x1b_G' not in term.stream.getvalue()
    assert app.visible_images == (chart, ornament)
    dialog = app.review_images()
    assert dialog.content.children[0].text == 'Chart\nValue rose\n\n1 -> 2'
    dialog.close()
    dialog = app.review_images(include_decorative=True)
    assert 'Ornament' in dialog.content.children[0].text


def test_desktop_reannotates_image_on_pixel_only_resize_and_removes_on_overlap(term, monkeypatch):
    """Use resize reports from the terminal and remove stale placements from native review."""
    term._is_a_tty = True
    term._keyboard_fd = None
    term._preferred_size_cache = WINSZ(24, 80, 640, 384)
    term._dec_mode_cache[DecPrivateMode.IN_BAND_WINDOW_RESIZE] = DecModeResponse.SET
    term.graphics._support = ImageSupport(
        frozenset({24, 32, 100, 316}), frozenset({Transfer.DIRECT}))
    monkeypatch.setattr(term, 'does_kitty_graphics', lambda: True)
    monkeypatch.setattr(term.accessibility, 'supported', lambda: True)
    images, annotations, deletes = [], [], []

    def acknowledge(sequence, image_id, timeout):
        images.append(parse_event(FRAME_RE.match(sequence).group()).fields)
        return ProtocolEvent('graphics', {'i': str(image_id)}, 'OK')

    def describe(image_id, placement_id, annotation, *, wait):
        annotations.append((image_id, placement_id, annotation))

    monkeypatch.setattr(term.graphics, '_request', acknowledge)
    monkeypatch.setattr(term.graphics, 'send', deletes.append)
    monkeypatch.setattr(term.accessibility, 'set_image', describe)
    view = ImageView(Image.new('I;16', (288, 160), 32768),
                     silkie.ImageAnnotation('Graph', 'Rising'))
    app = Application(term)
    app.add(Window('Chart', view, Rect(2, 2, 20, 8)))
    app.draw()
    app.draw()
    assert len(images) == len(annotations) == 1
    assert images[0]['f'] == '316'
    assert images[0]['p'] == '1' and annotations[0][1] == 1
    assert 't' not in images[0] and (images[0]['s'], images[0]['v']) == ('144', '80')
    view.annotation = silkie.ImageAnnotation('Updated chart', 'Stable')
    app.draw()
    assert len(images) == 1 and annotations[-1][-1] is view.annotation

    term.ungetch('\x1b[48;24;80;768;1280t')
    assert app.dispatch(term.inkey(timeout=0))
    app.draw()
    assert len(images) == 2 and len(annotations) == 3
    assert images[1]['i'] == images[0]['i']
    assert (images[1]['s'], images[1]['v']) == ('288', '160')

    cover = app.add(Window('Cover', Label('Overlay'), Rect(4, 4, 20, 8)))
    app.draw()
    assert not app.visible_images and parse_event(deletes[0]).fields['a'] == 'd'
    cover.close()
    app.draw()
    assert len(images) == 3 and len(annotations) == 4
    app.release_images()
    assert len(deletes) == 2

    term.graphics._support = ImageSupport(frozenset({24, 32, 100}), frozenset({Transfer.DIRECT}))
    canvas = app.draw()
    assert len(images) == 3
    assert any('Stable' in ''.join(cell[0] for cell in row) for row in canvas.rows)


def test_desktop_positions_real_terminal_cursor_at_the_focused_editor(term):
    """Silkie's native center-on-caret action has the editor's actual position."""
    field = InputLine('abc')
    app = Application(term)
    app.add(Window('Input', field, Rect(2, 3, 20, 6)))
    app.draw()
    assert term.stream.getvalue().endswith(term.move_xy(6, 4))
    app.dispatch(Keystroke('', name='KEY_HOME'))
    app.draw()
    assert term.stream.getvalue().endswith(term.move_xy(3, 4))
