"""Exercise discovery, screen layout and cleanup without Silkie hardware."""
# std imports
import contextlib
import importlib.util
import io
import json
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 3rd party
import pytest

# local
from hamingjur import kitty, silkie
from hamingjur.keyboard import Keystroke, resolve_sequence
from hamingjur.protocols import parse_event
from hamingjur.window import Size
from .accessories import TestTerminal
from .test_protocols import graphics_payload


@pytest.fixture(name='showcase')
def showcase_fixture():
    """Load the executable as a module, retaining its dataclass module identity."""
    spec = importlib.util.spec_from_file_location(
        'silkie_showcase', Path(__file__).parents[1] / 'bin' / 'silkie_showcase.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {spec.name: module}):
        spec.loader.exec_module(module)
        yield module


@pytest.fixture(name='demo')
def demo_fixture(showcase, monkeypatch):
    """Use real encoders and stream output, with deterministic query replies."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    term = TestTerminal(stream=io.StringIO(), force_styling=True)
    term._height_and_width = lambda: SimpleNamespace(ws_row=30, ws_col=120)
    args = SimpleNamespace(timeout=0, font=None, gpu_surface=None,
                           gpu_width=None, gpu_height=None)
    return showcase.Showcase(term, args, io.StringIO())


def report(selector, **fields):
    """Create a real decoded capability report."""
    return parse_event(silkie.command(selector, 'cap', **fields))


def supported_terminal(demo, monkeypatch, refused=()):
    """Simulate a terminal accepting all extensions and tracking reporting modes."""
    selectors = ('tablet-pointer', 'scroll', 'gesture', 'haptic', 'ink', 'font',
                 'display', 'latency', 'frame', 'depth', 'ssh-agent')
    caps = silkie.Capabilities(report('caps', ext=','.join(s + '.1' for s in selectors),
                                      format='301,302,304,308,516,32,64,200', media='d,g'))
    reports = {s: report(s, caps=1, mode=0) for s in selectors}
    reports['tablet-pointer'] = report('tablet-pointer', caps=511, mode=0)
    reports['font'] = report('font', caps=1, roles='default,serif')
    reports['display'] = report('display', mode=0, headroom='2.5', gamut='p3')
    reports['ink'] = report('ink', caps=1, mode=1, i=999, k=5, n=99,
                            fill='#112233', w0=1, w1=7)
    monkeypatch.setattr(demo.term.silkie, 'capabilities', lambda **_: caps)
    monkeypatch.setattr(silkie.Channel, 'query', lambda channel, **_: reports[channel.selector])
    modes = {s: 0 for s in selectors}
    modes['ink'] = 1
    send = silkie.Channel.send

    def send_mode(channel, operation, **fields):
        if operation == 'mode' and 'mode' in fields:
            modes[channel.selector] = 0 if channel.selector in refused else fields['mode']
        elif operation == 'brush':
            modes['ink'] = 1
        send(channel, operation, **fields)

    monkeypatch.setattr(silkie.Channel, 'send', send_mode)
    monkeypatch.setattr(silkie.Channel, 'read_mode', lambda channel, *_a, **_k:
                        modes[channel.selector])
    monkeypatch.setattr(demo.term.ssh_agent, 'query', lambda **_: report('ssh-agent', caps=0))
    monkeypatch.setattr(demo.term.graphics, 'supports', lambda *_a, **_k: True)
    monkeypatch.setattr(demo.term, 'get_decrqss', mock.Mock(
        side_effect=['0;56:200', '0;70:40', '0;38:2:1:255:0:0']))
    monkeypatch.setattr(demo.term.graphics, 'measure_text', lambda *_a, **_k: {'width': 80})
    monkeypatch.setattr(demo.term.graphics, 'service', lambda *_a, **_k: {'version': 1})
    monkeypatch.setattr(demo.term, '_query_with_boundary',
                        lambda _sequence, pattern, _timeout: pattern.search('\x1b]22;1\x1b\\'))
    monkeypatch.setattr(demo.term, 'get_cell_size', lambda **_: Size(2, 4))
    return modes


def batches(demo):
    """Read graphics packets from captured terminal output."""
    return re.findall(r'\x1b_G[^\x1b]*\x1b\\', demo.term.stream.getvalue())


def test_unsupported_terminal_still_shows_every_feature(demo, monkeypatch):
    """No extension replies should produce a complete, usable dashboard."""
    monkeypatch.setattr(demo.term.silkie, 'capabilities', lambda **_: None)
    monkeypatch.setattr(silkie.Channel, 'query', lambda *_a, **_k: None)
    monkeypatch.setattr(demo.term.ssh_agent, 'query', lambda **_: None)
    monkeypatch.setattr(demo.term.graphics, 'supports', lambda *_a, **_k: False)
    monkeypatch.setattr(demo.term, 'get_decrqss', lambda **_: None)
    monkeypatch.setattr(demo.term, 'query', lambda *_a, **_k: None)
    monkeypatch.setattr(demo.term, 'get_cell_size', lambda **_: None)
    with contextlib.ExitStack() as stack:
        demo.discover(stack)
        demo.render()
    assert all(feature.status == 'not supported' for feature in demo.features.values())
    output = demo.term.stream.getvalue()
    assert output.count('not supported') >= len(demo.features)
    assert not demo.images


def test_all_visual_samples_and_restoration(demo, monkeypatch):
    """Modes, brushes and pointer stack restore even when application code fails."""
    modes = supported_terminal(demo, monkeypatch)
    with pytest.raises(RuntimeError, match='application stopped'):
        with contextlib.ExitStack() as resources, contextlib.ExitStack() as stack:
            resources.callback(demo.release_images)
            demo.discover(stack)
            demo.render()
            assert {1, 2, 3, 401, 402, 404, 408, 616, 164} == demo.images
            output = demo.term.stream.getvalue()
            assert '56:200' in output and '57:200' in output and '70:40' in output
            assert 'depth;v=1:op=set:d=-150' in output
            assert all(modes[s] for s in ('scroll', 'gesture', 'latency', 'frame', 'display'))
            assert demo.features['gpu'].status == 'needs surface'
            assert demo.features['fonts'].status == 'needs --font'
            assert demo.features['ssh-agent'].status == 'not supported'
            raise RuntimeError('application stopped')
    assert not demo.images
    assert all(modes[s] == 0 for s in ('scroll', 'gesture', 'latency', 'frame', 'display'))
    assert modes['ink'] == 1
    output = demo.term.stream.getvalue()
    assert 'op=brush:i=999:k=5:n=99:fill=#112233:w0=1:w1=7' in output
    assert kitty_pop() in output
    deletion = next(frame for frame in batches(demo)
                    if parse_event(frame).fields.get('a') == 'd'
                    and parse_event(frame).fields.get('i') == '1')
    assert output.index(kitty_pop()) < output.index(deletion)


def kitty_pop():
    """The pointer pop must precede deletion of its backing image."""
    return '\x1b]22;<'


def test_refused_modes_do_not_disable_unrelated_features(demo, monkeypatch):
    """Advertised support is insufficient if the requested mode is refused."""
    supported_terminal(demo, monkeypatch, refused=('tablet-pointer', 'frame'))
    with contextlib.ExitStack() as stack:
        demo.discover(stack)
        assert demo.features['pen'].status == 'not supported'
        assert demo.features['frame'].status == 'not supported'
        assert demo.features['ink'].status == 'not supported'
        assert demo.features['vectors'].status == 'OK'
        assert demo.features['scroll'].status == 'OK'


def test_resize_preserves_ink_and_animation_reuses_ids(demo, monkeypatch):
    """Repeated redraws do not reset ink, allocate new IDs or add placements."""
    supported_terminal(demo, monkeypatch)
    with contextlib.ExitStack() as stack:
        demo.discover(stack)
        demo.render()
        owned = demo.images.copy()
        before = demo.term.stream.getvalue()
        demo.term._height_and_width = lambda: SimpleNamespace(ws_row=42, ws_col=160)
        for _ in range(20):
            demo.render()
        added = demo.term.stream.getvalue()[len(before):]
        assert demo.images == owned
        assert 'op=brush' not in added
        assert not any(parse_event(frame).fields.get('a') == 'T'
                       and parse_event(frame).fields.get('i') == '3'
                       for frame in re.findall(r'\x1b_G[^\x1b]*\x1b\\', added))
        assert 'a=p,i=3,p=1' in added
        edits = [frame for frame in batches(demo) if parse_event(frame).fields.get('a') == 'e']
        assert len(edits) == 21
        assert all(b'g id=5 ' in graphics_payload([frame]) for frame in edits)
        demo.handle(Keystroke('c'))
        demo.render()
        assert demo.term.stream.getvalue().count('op=brush:i=3:k=1:') == 2


def test_event_counts_log_and_graphics_errors(demo):
    """Long reports are fully logged; estimates and device reports reach their panels."""
    frames = [silkie.command('tablet-pointer', 'device', kind=1, name='A' * 70000),
              silkie.command('tablet-pointer', 'event', kind=1, phase=7, est=1, ink=9),
              silkie.command('tablet-pointer', 'event', kind=2, conf=0),
              silkie.command('frame', 'event', seq=123, t=1000, tu=10),
              '\x1b_Gi=2;EINVAL:bad scene\x1b\\']
    for frame in frames:
        key = resolve_sequence(frame, {}, {})
        # The same structured event property used by actual keyboard decoding.
        assert key.protocol_event is not None
        assert demo.handle(key)
    records = [json.loads(line) for line in demo.event_log.getvalue().splitlines()]
    assert len(records) == len(frames)
    assert len(records[0]['fields']['name']) == 70000
    assert demo.features['hardware'].count == 1
    assert demo.features['estimates'].count == 1
    assert demo.features['accidental'].count == 1
    assert demo.features['frame'].count == 1
    assert demo.features['vectors'].status == 'error'
    assert 'bad scene' in demo.features['vectors'].detail


def test_small_screen_navigation_keeps_all_features_accessible(demo):
    """Small windows explicitly offer scrolling instead of dropping feature rows."""
    demo.term._height_and_width = lambda: SimpleNamespace(ws_row=12, ws_col=40)
    demo.render()
    assert 'j/k scroll' in demo.term.stream.getvalue()
    for _ in range(len(demo.features) - 1):
        demo.handle(Keystroke('\t'))
    assert demo.selected == len(demo.features) - 1
    _, _, _, rows, visible, _ = demo.layout()
    assert demo.offset <= demo.selected % rows < demo.offset + visible


@pytest.mark.parametrize('fmt', [301, 302, 304, 308, 516, 32, 64])
@pytest.mark.parametrize('width,height', [(1, 1), (13, 9), (257, 257)])
def test_generated_images_have_complete_protocol_rows(showcase, fmt, width, height):
    """Odd widths, row padding and images beyond old buffer sizes remain complete."""
    data = showcase.pixels(fmt, width, height)
    assert len(data) == math.ceil(width * (fmt % 100) / 8) * height


@pytest.mark.parametrize('value', ['0', '-1', 'inf', 'nan'])
def test_invalid_timing_is_rejected_before_terminal_access(showcase, value):
    """An invalid loop interval cannot hang or spin the demo."""
    with mock.patch.object(showcase, 'Terminal') as constructor:
        with pytest.raises(SystemExit):
            showcase.main(['--fps', value])
        constructor.assert_not_called()


def test_agent_exchange_closes_the_provider(demo, monkeypatch):
    """The transport test sends only an identity request and closes its connection."""
    demo.reports['ssh-agent'] = report('ssh-agent', caps=1, g='providerA')
    reply = parse_event(silkie.command('ssh-agent', 'reply',
                                       data='AAAABQwAAAAA'))
    exchange = mock.Mock(return_value=reply)
    close = mock.Mock()
    monkeypatch.setattr(demo.term.ssh_agent, 'exchange', exchange)
    monkeypatch.setattr(demo.term.ssh_agent, 'close', close)
    demo.probe_agent()
    assert exchange.call_args.args == (b'\0\0\0\1\x0b',)
    assert close.call_args.args == (exchange.call_args.kwargs['connection'], 'providerA')
    assert demo.features['ssh-agent'].status == 'OK'
    assert '0 identities' in demo.features['ssh-agent'].detail


def test_notification_route_query_never_displays_a_notification(demo, monkeypatch):
    """The terminal must echo the route, not merely acknowledge OSC 99 support."""
    queries = []

    def query(sequence, pattern, _timeout):
        queries.append(sequence)
        event = parse_event(sequence)
        assert event.fields['p'] == '?' and not event.payload
        reply = kitty.notification_packet(metadata=list(event.fields.items()))
        return pattern.search(reply)

    monkeypatch.setattr(demo.term, '_query_with_boundary', query)
    demo._probe_notifications()
    assert len(queries) == 1
    assert demo.features['notifications'].status == 'OK'


@pytest.mark.parametrize('terminator', ['\x07', '\x1b\\'])
@pytest.mark.parametrize('answer', ['0', '1', None])
def test_pointer_probe_uses_real_string_query_result(demo, terminator, answer, monkeypatch):
    """Read real buffered replies through Terminal.query, including unsupported terminals."""
    demo.term._is_a_tty = True
    demo.term._keyboard_fd = None
    monkeypatch.setattr(demo.term, 'does_kitty_graphics', lambda *_: True)
    demo.formats.add(32)
    reply = f'\x1b]22;{answer}{terminator}' if answer is not None else ''
    demo.term.ungetch('x' + reply + '\x1b[1;1R')
    with contextlib.ExitStack() as stack:
        demo._probe_pointer(stack)
        assert demo.features['pointer'].status == ('OK' if answer == '1' else 'not supported')
        assert demo.term.inkey(timeout=0) == 'x'
    if answer == '1':
        assert kitty_pop() in demo.term.stream.getvalue()


@pytest.mark.parametrize('echo_route', [False, True])
def test_notification_probe_uses_real_string_query_result(
        showcase, demo, monkeypatch, echo_route):
    """Decode a buffered OSC 99 reply through the actual public query method."""
    monkeypatch.setattr(showcase.uuid, 'uuid4', lambda: SimpleNamespace(hex='test'))
    demo.term._is_a_tty = True
    demo.term._keyboard_fd = None
    metadata = [('i', 'showcase-test'), ('p', '?')]
    if echo_route:
        metadata.append(('r', 'showcase/showcase-test/0'))
    demo.term.ungetch('x' + kitty.notification_packet(metadata=metadata) + '\x1b[1;1R')
    demo._probe_notifications()
    assert demo.features['notifications'].status == ('OK' if echo_route else 'not supported')
    assert demo.term.inkey(timeout=0) == 'x'


@pytest.mark.parametrize('acknowledged', [False, True])
def test_font_upload_lifetime_and_missing_ack(demo, monkeypatch, tmp_path, acknowledged):
    """Release a requested font even when its upload acknowledgement is lost."""
    supported_terminal(demo, monkeypatch)
    demo.args.font = tmp_path / 'outline.ttf'
    demo.args.font.write_bytes(b'font data supplied to the terminal')
    upload = mock.Mock(return_value={} if acknowledged else None)
    service = mock.Mock(return_value={'version': 1})
    monkeypatch.setattr(demo.term.graphics, 'upload_font', upload)
    monkeypatch.setattr(demo.term.graphics, 'service', service)
    with contextlib.ExitStack() as stack:
        demo.discover(stack)
        assert demo.features['fonts'].status == ('OK' if acknowledged else 'not supported')
        if acknowledged:
            assert demo.text_layout['font'] == '@' + demo.font_id
    assert upload.call_count == 1
    assert upload.call_args.args == (demo.font_id, demo.args.font.read_bytes())
    service.assert_any_call('release', id=demo.font_id, timeout=0)


def test_gpu_requires_ack_before_refresh(demo, monkeypatch):
    """A real descriptor is referenced once; refresh starts only after an OK reply."""
    supported_terminal(demo, monkeypatch)
    demo.args.gpu_surface = 'iosurface:123'
    demo.args.gpu_width, demo.args.gpu_height = 640, 480
    demo.args.timeout = 1
    with contextlib.ExitStack() as stack:
        demo.discover(stack)
        demo.render()
        frames = [parse_event(frame) for frame in batches(demo)]
        upload, = [event for event in frames if event.fields.get('t') == 'g']
        assert upload.data == b'iosurface:123'
        assert upload.fields['s'] == '640' and upload.fields['v'] == '480'
        assert demo.features['gpu'].status == 'pending'
        assert not any(event.fields.get('a') == 'r' for event in frames)
        demo.handle(resolve_sequence('\x1b_Gi=4;OK\x1b\\', {}, {}))
        assert demo.features['gpu'].status == 'OK'
        demo.animate()
        assert any(parse_event(frame).fields.get('a') == 'r' for frame in batches(demo))
