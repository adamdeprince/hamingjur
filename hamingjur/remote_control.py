"""Explicit Kitty/Silkie remote control over the terminal or a configured socket."""
from __future__ import annotations

# std imports
import re
import json
import time
import socket
from sys import float_info
from typing import TYPE_CHECKING, Any, Mapping, Optional
from urllib.parse import urlsplit

# local
from .protocols import ST, integer

if TYPE_CHECKING:
    # local
    from .terminal import Terminal

# Wire version of the Kitty source used for this implementation; callers can override it.
PROTOCOL_VERSION = (0, 49, 2)
PREFIX = '\x1bP@kitty-cmd'
RESPONSE = re.compile(re.escape(PREFIX) + r'[^\x1b]*\x1b\\')


class RemoteControlError(RuntimeError):
    """The terminal refused a command or reported its failure."""

    def __init__(self, reply: Mapping[str, Any]):
        """Preserve the complete response, including optional diagnostic fields."""
        self.response = reply
        super().__init__(str(reply.get('error', 'Remote control command failed')))


def packet(message: Mapping[str, Any]) -> str:
    """Encode a JSON envelope, escaping control characters and rejecting non-finite numbers."""
    return PREFIX + json.dumps(message, ensure_ascii=True, allow_nan=False,
                               separators=(',', ':')) + ST


def command(name: str, payload: Optional[Mapping[str, Any]] = None, *,
            version: tuple[int, int, int] = PROTOCOL_VERSION,
            no_response: bool = False, **fields: Any) -> str:
    """
    Encode any remote-control command, including async and streaming envelope fields.

    The terminal must permit remote control. Password authentication requires an externally
    encrypted envelope passed to packet(); plaintext passwords are rejected.

    :raises ValueError: If command/envelope fields are invalid or include a plaintext password.
    """
    if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
        raise ValueError('Invalid remote-control command name')
    if len(version) != 3:
        raise ValueError('Remote-control version requires three integers')
    for component in version:
        integer(component, 'version')
    if fields.keys() & {'cmd', 'version', 'payload', 'no_response', 'password'}:
        raise ValueError('Reserved envelope field or unsupported plaintext password')
    return packet({'cmd': name, 'version': version, 'no_response': no_response,
                   **({'payload': payload} if payload is not None else {}), **fields})


def response(frame: str) -> Any:
    """
    Decode a complete response and return its data, preserving terminal errors.

    :raises ValueError: If the response is malformed.
    :raises RemoteControlError: If the terminal rejects or fails the request.
    """
    if not RESPONSE.fullmatch(frame):
        raise ValueError('Expected one complete remote-control reply')
    value = json.loads(frame[len(PREFIX):-len(ST)])
    if not isinstance(value, dict) or not isinstance(value.get('ok'), bool):
        raise ValueError('Remote-control reply must contain a boolean ok field')
    if not value['ok']:
        raise RemoteControlError(value)
    return value.get('data')


def _connect(address: str, timeout: Optional[float]) -> socket.socket:
    if address.startswith('unix:'):
        path = address[5:]
        if path.startswith('@'):
            path = '\0' + path[1:]
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(timeout)
            connection.connect(path)
        except BaseException:
            connection.close()
            raise
        return connection
    parsed = urlsplit(address.replace('tcp:', 'tcp://', 1))
    if parsed.scheme != 'tcp' or not parsed.hostname or parsed.port is None:
        raise ValueError('Address must be unix:/path or tcp:host:port')
    return socket.create_connection((parsed.hostname, parsed.port), timeout)


class RemoteControl:
    """
    Explicit remote-control client; construction never connects or changes settings.

    Use term.remote_control for the current terminal, or RemoteControl(address=...)
    for an explicitly configured Unix/TCP listener. Calls must be serialized when
    sharing a terminal: ordinary synchronous replies have no correlation ID.
    """

    def __init__(self, terminal: Optional[Terminal] = None, *, address: Optional[str] = None,
                 version: tuple[int, int, int] = PROTOCOL_VERSION):
        """
        Choose a terminal or socket transport, without reading environment addresses.

        :raises ValueError: If neither or both transports are specified.
        """
        if (terminal is None) == (address is None):
            raise ValueError('Supply exactly one terminal or socket address')
        self.terminal = terminal
        self.address = address
        self.version = version

    def call(self, name: str, payload: Optional[Mapping[str, Any]] = None, *,
             timeout: Optional[float] = 1.0, no_response: bool = False, **fields: Any) -> Any:
        """
        Send an explicit command and return its data; no subprocess is invoked.

        :raises RuntimeError: If terminal output is disabled.
        :raises TimeoutError: If no complete reply arrives within the deadline.
        :raises RemoteControlError: If the terminal reports failure or refusal.
        """
        sequence = command(name, payload, version=self.version, no_response=no_response, **fields)
        frame: Optional[str]
        if self.address is not None:
            frame = self._socket_call(sequence, timeout, no_response)
        else:
            assert self.terminal is not None
            if not self.terminal.does_styling:
                raise RuntimeError('Remote control requires an active terminal stream')
            if no_response:
                self.terminal.send(sequence)
                return None
            frame = self.terminal.query(sequence, RESPONSE.pattern, timeout)
        if no_response:
            return None
        if frame is None:
            raise TimeoutError(
                'No remote-control reply; the terminal may not permit remote control')
        return response(frame)

    def _socket_call(self, sequence: str, timeout: Optional[float], no_response: bool) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        assert self.address is not None
        with _connect(self.address, timeout) as connection:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Remote-control connection deadline exceeded')
                connection.settimeout(remaining)
            connection.sendall(sequence.encode('utf-8'))
            if no_response:
                return ''
            received = bytearray()
            search_from = 0
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Remote-control response deadline exceeded')
                    connection.settimeout(remaining)
                # This is an OS read batch, not a response-size limit. Storage grows until ST.
                block = connection.recv(4096)
                if not block:
                    raise EOFError('Remote-control socket closed before a complete reply')
                received.extend(block)
                end = received.find(ST.encode(), search_from)
                if end >= 0:
                    return received[:end + len(ST)].decode('utf-8')
                search_from = max(0, len(received) - len(ST) + 1)

    def resize_window(self, *, width: int = 0, height: int = 0, unit: str = 'cells',
                      incremental: bool = False, match: Optional[str] = None,
                      timeout: Optional[float] = 1.0) -> Any:
        """
        Resize the calling OS window, or an explicitly matched window, in cells or pixels.

        Zero leaves a dimension unchanged. Incremental sizes may be negative. Successful
        acknowledgement is not a guarantee that the window manager used the requested size.

        :raises ValueError: If units/dimensions are invalid or no size is specified.
        """
        if unit not in {'cells', 'pixels'}:
            raise ValueError('Window units must be cells or pixels')
        for value in (width, height):
            if (isinstance(value, bool) or not isinstance(value, int)
                    or (value < 0 and not incremental)):
                raise ValueError('Window size must be integer and nonnegative unless incremental')
        if width == 0 and height == 0:
            raise ValueError('Specify at least one window dimension')
        if self.address is not None and match is None:
            raise ValueError('Socket resizing requires an explicit window match')
        payload = {'action': 'resize', 'unit': unit, 'width': width, 'height': height,
                   'incremental': incremental, 'self': match is None}
        if match is not None:
            payload['match'] = match
        result = self.call('resize-os-window', payload, timeout=timeout)
        if self.terminal is not None:
            self.terminal.invalidate_geometry()
        return result

    def _pane_command(self, command_name: str, payload: Mapping[str, Any],
                      timeout: Optional[float]) -> Any:
        if self.terminal is None:
            raise ValueError('Silkie pane controls require the terminal connection')
        return self.call(command_name, payload, timeout=timeout)

    def magnify_pane(self, operation: str | float = 'in', *, row: Optional[int] = None,
                     column: Optional[int] = None, timeout: Optional[float] = 1.0) -> Any:
        """Ask Silkie to zoom or pan the calling pane over its terminal connection."""
        if isinstance(operation, str):
            if operation not in {'in', 'out', 'reset', 'caret', 'left', 'right', 'up', 'down'}:
                raise ValueError('Unknown pane magnification operation')
            if row is not None or column is not None:
                raise ValueError('A center requires a numeric magnification')
            argument = operation
        else:
            if (isinstance(operation, bool) or not isinstance(operation, (int, float))
                    or not 1 <= operation <= float_info.max):
                raise ValueError('Pane magnification must be a finite factor of at least one')
            if (row is None) != (column is None):
                raise ValueError('Supply both one-based center coordinates')
            argument = str(operation)
            if row is not None and column is not None:
                argument += (f' {integer(row, "row", 1, (1 << 32) - 1)}'
                             f' {integer(column, "column", 1, (1 << 32) - 1)}')
        result = self._pane_command('action', {'action': f'magnify_pane {argument}', 'self': True},
                                    timeout)
        assert self.terminal is not None
        self.terminal.invalidate_geometry()
        return result

    def review_images(self, *, include_decorative: bool = False,
                      timeout: Optional[float] = 1.0) -> Any:
        """Ask Silkie to open the current pane's native image review."""
        action = 'review_all_images' if include_decorative else 'review_images'
        return self._pane_command('action', {'action': action, 'self': True}, timeout)

    def set_minimum_contrast(self, percent: Optional[float], *,
                             timeout: Optional[float] = 1.0) -> Any:
        """Set the calling pane's contrast threshold, or restore its default with None."""
        if percent is None:
            value = 'default'
        else:
            if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                    or not 0 <= percent <= 100):
                raise ValueError('Minimum contrast must be a percentage from 0 to 100')
            value = str(percent)
        return self._pane_command('set-minimum-contrast', {'value': value}, timeout)
