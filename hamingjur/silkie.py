"""Silkie discovery, tablet/touch input, gestures, ink, haptics, fonts and timing."""
# Protocol clients intentionally share Terminal's private query transport.
# pylint: disable=protected-access
from __future__ import annotations

# std imports
import re
import uuid
import contextlib
from enum import IntFlag
from typing import TYPE_CHECKING, Union, Iterator, Optional

# local
from .protocols import (ST,
                        Field,
                        ProtocolEvent,
                        b64,
                        token,
                        integer,
                        agent_token,
                        parse_event,
                        agent_packet,
                        encode_fields)

if TYPE_CHECKING:
    # local
    from .terminal import Terminal


class TabletMode(IntFlag):
    """Tablet reporting bits; unsupported or refused bits are read back as off."""

    TOOLS = 1
    TOUCH = 2
    PAD = 4
    HARDWARE_IDS = 8
    TOOL_ACTIONS = 16
    ESTIMATES = 32
    ACCIDENTAL_TOUCH = 64


def command(selector: str, operation: str, **fields: Field) -> str:
    """
    Encode any version-one Silkie operation, preserving extension-specific fields.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if fields.keys() & {'v', 'op'}:
        raise ValueError('v and op are controlled by command()')
    return f'\x1b]777;{token(selector)};' + encode_fields(
        {'v': 1, 'op': token(operation), **fields}) + ST


def depth(millidegrees: Optional[int] = None, *, pixels: Optional[float] = None,
          phase: Optional[float] = None) -> str:
    """
    Set stereo text depth and/or vertical phase, or reset both with no arguments.

    Pixel depth uses reference pixels at 1920 pixels per eye; phase uses physical
    pixels. Omitted attributes are preserved. The terminal applies user limits.

    :raises ValueError: If depth forms conflict or values exceed their wire representation.
    """
    if millidegrees is not None and pixels is not None:
        raise ValueError('Choose millidegrees or pixels for depth')
    fields: dict[str, Field] = {}
    if millidegrees is not None:
        fields['d'] = integer(millidegrees, 'millidegrees', -(1 << 31), (1 << 31) - 1)
    for key, value in (('p', pixels), ('y', phase)):
        if value is not None:
            # Silkie stores pixel attributes in IEEE-754 binary32, per its wire specification.
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not -float.fromhex('0x1.fffffep+127') <= value
                    <= float.fromhex('0x1.fffffep+127')):
                raise ValueError('Pixel attributes require finite binary32 values')
            fields[key] = str(value)
    return command('depth', 'set' if fields else 'reset', **fields)


def agent_request(packet: bytes, *, identifier: str, connection: str,
                  generation: str, expires: int) -> str:
    """Encode one SSH-agent request; expires is an absolute Unix millisecond deadline."""
    return command('ssh-agent', 'request', id=agent_token(identifier),
                   c=agent_token(connection), g=agent_token(generation),
                   expires=integer(expires, 'expires', 0, (1 << 64) - 1),
                   data=b64(agent_packet(packet)))


class Capabilities:
    """The selector versions, graphics media and formats advertised by a terminal."""

    def __init__(self, event: ProtocolEvent):
        """Parse a capability report, retaining its original fields."""
        self.event = event
        self.protocols: dict[str, int] = {}
        for item in filter(None, event.fields.get('ext', '').split(',')):
            name, separator, version = item.rpartition('.')
            if not separator or not version.isdecimal():
                raise ValueError('Invalid extension version')
            self.protocols[token(name)] = int(version)
        self.media = frozenset(filter(None, event.fields.get('media', '').split(',')))
        self.formats = frozenset(int(value) for value in
                                 filter(None, event.fields.get('format', '').split(',')))

    def supports(self, selector: str, version: int = 1) -> bool:
        """Check the advertised version of a selector, not its current hardware support."""
        return self.protocols.get(selector, 0) >= version


class Channel:
    """One OSC 777 selector, with commands, queries and restoring mode contexts."""

    def __init__(self, client: 'Silkie', selector: str):
        """Bind a selector to the shared client and query transport."""
        self.client = client
        self.selector = token(selector)

    def command(self, operation: str, **fields: Field) -> str:
        """Return a sequence without performing I/O."""
        return command(self.selector, operation, **fields)

    def send(self, operation: str, **fields: Field) -> None:
        """Write and flush an operation when terminal styling is enabled."""
        self.client.write(self.command(operation, **fields))

    def query(self, timeout: Optional[float] = 1.0) -> Optional[ProtocolEvent]:
        """Read capability and current-mode fields; return None if unsupported."""
        request_id = self.client.next_id()
        return self.client.request(self.command('query', id=request_id), self.selector,
                                   'cap', timeout, request_id)

    def read_mode(self, timeout: Optional[float] = 1.0) -> Optional[int]:
        """Read the granted mode rather than assuming every requested bit was accepted."""
        result = self.client.request(self.command('mode'), self.selector, 'mode', timeout)
        return result.integer('mode') if result is not None else None

    @contextlib.contextmanager
    def enabled(self, mode: int = 1, *, timeout: Optional[float] = 1.0
                ) -> Iterator[int]:
        """
        Set a reporting mode, yield granted bits, and restore its previous value.

        Raises RuntimeError when the mode cannot be read. Nested contexts restore the enclosing
        mode. Cleanup runs even if read-back or application code fails.

        :raises RuntimeError: If the mode is unavailable or read-back times out.
        """
        integer(mode, 'mode', 0, 0x7fffffff)
        previous = self.read_mode(timeout)
        if previous is None:
            raise RuntimeError(f'{self.selector} mode is unavailable')
        try:
            self.send('mode', mode=int(mode))
            granted = self.read_mode(timeout)
            if granted is None:
                raise RuntimeError(f'{self.selector} mode read-back timed out')
            yield granted
        finally:
            self.send('mode', mode=previous)


class AgentChannel(Channel):
    """Explicit SSH-agent packet transport; no sockets, key caching or automatic retries."""

    def query(self, timeout: Optional[float] = 1.0, *, identifier: Optional[str] = None
              ) -> Optional[ProtocolEvent]:
        """Probe provider availability; caps=1 requires a generation token in g."""
        request_id = agent_token(identifier if identifier is not None else uuid.uuid4().hex)
        return self.client.request(self.command('query', id=request_id), self.selector,
                                   'cap', timeout, request_id)

    def exchange(self, packet: bytes, *, connection: str, generation: str, expires: int,
                 identifier: Optional[str] = None, timeout: Optional[float] = 120.0
                 ) -> Optional[ProtocolEvent]:
        """
        Exchange one complete packet, matching ID, connection and provider generation.

        Keep connection and generation stable for a local agent connection, probe before
        each packet, and close on generation changes or timeouts. Requests are never retried.
        A transport failure is returned as an agent failure packet plus an error field.
        """
        request_id = agent_token(identifier if identifier is not None else uuid.uuid4().hex)
        sequence = agent_request(packet, identifier=request_id, connection=connection,
                                 generation=generation, expires=expires)
        return self.client.request(sequence, self.selector, 'reply', timeout, request_id,
                                   c=connection, g=generation)

    def close(self, connection: str, generation: str, *, identifier: Optional[str] = None
              ) -> None:
        """Close a pinned provider connection; its token must never be reused."""
        request_id = agent_token(identifier if identifier is not None else uuid.uuid4().hex)
        self.send('close', id=request_id,
                  c=agent_token(connection), g=agent_token(generation))


class Silkie:
    """
    Bound Silkie protocol client, available as ``term.silkie``.

    Every selector has a named channel. Commands use documented field names,
    including device descriptions/labels, brush configuration and haptic patterns.
    Received messages are available through ``term.inkey().protocol_event``.
    """
    # The named channels are the public protocol API, not independent mutable state.
    # pylint: disable=too-many-instance-attributes

    def __init__(self, terminal: 'Terminal'):
        """Create channels and per-terminal discovery state."""
        self.terminal = terminal
        self._request_id = 0
        self._capabilities: Optional[Capabilities] = None
        self.tablet = Channel(self, 'tablet-pointer')
        self.scroll = Channel(self, 'scroll')
        self.gesture = Channel(self, 'gesture')
        self.haptic = Channel(self, 'haptic')
        self.ink = Channel(self, 'ink')
        self.font = Channel(self, 'font')
        self.display = Channel(self, 'display')
        self.latency = Channel(self, 'latency')
        self.frame = Channel(self, 'frame')
        self.depth = Channel(self, 'depth')
        self.ssh_agent = AgentChannel(self, 'ssh-agent')

    def channel(self, selector: str) -> Channel:
        """Access a future selector without waiting for a library update."""
        return Channel(self, selector)

    def next_id(self) -> int:
        """Allocate a correlation ID, rejecting exhaustion of the protocol's 31-bit space."""
        self._request_id = integer(self._request_id + 1, 'request id', 1, 0x7fffffff)
        return self._request_id

    def write(self, sequence: str) -> None:
        """Write an explicit command; redirected plain-text output remains escape-free."""
        if self.terminal.does_styling:
            self.terminal.stream.write(sequence)
            self.terminal.stream.flush()

    def request(self, sequence: str, selector: str, operation: str,
                timeout: Optional[float], request_id: Optional[Union[int, str]] = None,
                **reply_fields: Field
                ) -> Optional[ProtocolEvent]:
        """Match only this reply, retaining keyboard input and unrelated extension events."""
        # Fields can arrive in any order. Lookaheads are confined to this frame.
        checks = [r'(?=[^\x1b\x07]*(?:;|:)v=1(?=:|\x07|\x1b))',
                  r'(?=[^\x1b\x07]*(?:;|:)op=' + re.escape(operation)
                  + r'(?=:|\x07|\x1b))']
        if request_id is not None:
            reply_fields['id'] = request_id
        for key, value in reply_fields.items():
            checks.append(r'(?=[^\x1b\x07]*(?:;|:)' + re.escape(token(key))
                          + '=' + re.escape(str(value)) + r'(?=:|\x07|\x1b))')
        pattern = re.compile(r'\x1b\]777;' + re.escape(token(selector))
                             + ''.join(checks) + r';[^\x1b\x07]*(?:\x07|\x1b\\)')
        match = self.terminal._query_with_boundary(sequence, pattern, timeout)
        if match is None:
            return None
        try:
            return parse_event(match.group())
        except ValueError:
            return None

    def capabilities(self, timeout: Optional[float] = 1.0, *, force: bool = False
                     ) -> Optional[Capabilities]:
        """Discover selectors/formats/media; cache successful discovery only."""
        if self._capabilities is not None and not force:
            return self._capabilities
        event = self.channel('caps').query(timeout)
        self._capabilities = Capabilities(event) if event is not None else None
        return self._capabilities

    def reset(self) -> None:
        """Explicitly disable all Silkie reporting modes in this terminal window."""
        self.write(command('caps', 'reset'))

    def select_font(self, *, role: Optional[str] = None, weight: Optional[int] = None,
                    slant: Optional[Union[str, int]] = None,
                    italic: Optional[bool] = None) -> str:
        """Select terminal-cell typography; slant is in hundredths of a degree."""
        fields: dict[str, Field] = {}
        if role is not None:
            fields['role'] = token(role)
        if weight is not None:
            fields['weight'] = integer(weight, 'weight', 100 if weight else 0, 900)
        if slant is not None:
            if slant != 'auto':
                integer(slant, 'slant', -1500, 1500)
            fields['slant'] = slant
        if italic is not None:
            fields['italic'] = int(italic)
        return command('font', 'select', **fields) if self.terminal.does_styling else ''

    def label_button(self, device: int, button: int, text: str) -> str:
        """Label a tablet-pad button with Unicode text."""
        return command('tablet-pointer', 'label', device=integer(device, 'device'),
                       button=integer(button, 'button'), text=b64(text.encode('utf-8')))
