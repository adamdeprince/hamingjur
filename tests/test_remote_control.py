"""Remote-control wire examples, refusals, explicit targeting and growing socket replies."""
# std imports
import io
import json
import socket
import threading
from unittest import mock

# 3rd party
import pytest

# local
from hamingjur import remote_control as rc
from hamingjur.protocols import parse_event, incomplete_frame
# local
from .accessories import TestTerminal


@pytest.fixture(name='term')
def terminal_fixture(monkeypatch):
    """A terminal stream with no real remote-control target."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    return TestTerminal(stream=io.StringIO(), force_styling=True)


def message(sequence):
    """Extract an envelope using the documented frame delimiters."""
    assert sequence.startswith('\x1bP@kitty-cmd') and sequence.endswith('\x1b\\')
    return json.loads(sequence[12:-2])


def test_resize_targets_calling_window_and_reports_result(term):
    """The convenience API targets self and keeps dimensions separate from acknowledgement."""
    with mock.patch.object(term, 'query', return_value=rc.packet({'ok': True})) as query:
        assert term.remote_control.resize_window(width=120, height=40) is None
    payload = message(query.call_args.args[0])['payload']
    assert payload == {'action': 'resize', 'unit': 'cells', 'width': 120, 'height': 40,
                       'incremental': False, 'self': True}


@pytest.mark.parametrize('reply,error', [
    (rc.packet({'ok': False, 'error': 'Remote control is disabled'}), rc.RemoteControlError),
    (None, TimeoutError),
    (rc.packet({'ok': 1}), ValueError),
])
def test_remote_failures_are_not_success(term, reply, error):
    """Refusals, missing replies and invalid responses are distinguishable failures."""
    with mock.patch.object(term, 'query', return_value=reply):
        with pytest.raises(error):
            term.remote_control.resize_window(width=80)


def test_no_response_is_explicit_and_control_characters_are_escaped(term):
    """JSON text can carry terminal controls only as escaped JSON, never extra frames."""
    term.remote_control.call('send-text', {'text': '\x1b\\\n界'}, no_response=True)
    envelope = message(term.stream.getvalue())
    assert envelope['no_response'] is True
    assert envelope['payload']['text'] == '\x1b\\\n界'
    assert term.stream.getvalue().count('\x1b') == 2


def test_password_and_implicit_socket_target_are_rejected():
    """The client neither sends plaintext authentication nor guesses which window to resize."""
    with pytest.raises(ValueError, match='password'):
        rc.command('ls', password='secret')
    remote = rc.RemoteControl(address='unix:/unused')
    with pytest.raises(ValueError, match='explicit window match'):
        remote.resize_window(width=80)
    with pytest.raises(ValueError):
        remote.resize_window(width=-1, match='id:7')


def test_socket_response_beyond_64k_and_fragmented_terminator():
    """A real stream socket delivers an arbitrarily large result in fragmented writes."""
    client, server = socket.socketpair()
    data = '界' * 70000
    requests = []

    def serve():
        with server:
            request = bytearray()
            while not request.endswith(b'\x1b\\'):
                request.extend(server.recv(4096))
            requests.append(message(request.decode()))
            encoded = rc.packet({'ok': True, 'data': data}).encode()
            server.sendall(encoded[:-1])
            server.sendall(encoded[-1:])

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with mock.patch.object(rc, '_connect', return_value=client):
            remote = rc.RemoteControl(address='unix:/test')
            assert remote.call('ls', timeout=2) == data
    finally:
        client.close()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert requests[0]['cmd'] == 'ls'


def test_partial_socket_eof_is_not_a_partial_response():
    """An incomplete frame is never returned as successful data."""
    client, server = socket.socketpair()
    server.sendall(b'\x1bP@kitty-cmd{"ok":true')
    server.shutdown(socket.SHUT_WR)
    try:
        with mock.patch.object(rc, '_connect', return_value=client):
            with pytest.raises(EOFError):
                rc.RemoteControl(address='unix:/test').call('ls')
    finally:
        client.close()
        server.close()


def test_late_and_fragmented_remote_reply_is_one_event(term):
    """Remote responses use the same growing input parser as other terminal protocols."""
    raw = rc.packet({'ok': True, 'data': 'x' * 70000})
    assert incomplete_frame(raw[:-1])
    event = parse_event(raw)
    assert event.protocol == 'remote-control'
    assert json.loads(event.payload)['data'] == 'x' * 70000
    term.ungetch(raw + 'z')
    key = term.inkey(timeout=0)
    assert key.protocol_event.protocol == 'remote-control'
    assert rc.response(str(key)) == 'x' * 70000
    assert term.inkey(timeout=0) == 'z'


def test_socket_resize_match_and_incremental_values():
    """Explicit socket targets and signed increments survive the envelope intact."""
    remote = rc.RemoteControl(address='unix:/test')
    with mock.patch.object(remote, 'call', return_value=None) as call:
        remote.resize_window(width=-10, height=4, incremental=True, match='id:7')
    assert call.call_args.args == ('resize-os-window', {
        'action': 'resize', 'unit': 'cells', 'width': -10, 'height': 4,
        'incremental': True, 'self': False, 'match': 'id:7'})
