"""Silkie discovery, tablet/touch input, gestures, ink, haptics, fonts and timing."""
# Protocol clients intentionally share Terminal's private query transport.
# pylint: disable=protected-access
from __future__ import annotations

# std imports
import re
import uuid
import contextlib
from dataclasses import dataclass, fields as dataclass_fields
from enum import IntFlag
from typing import TYPE_CHECKING, Union, Iterator, Optional
from urllib.parse import quote

# local
from .protocols import (ST,
                        AccessibilityAck,
                        DisplayEvent,
                        Field,
                        ProtocolEvent,
                        SpeechAck,
                        SpeechCapabilities,
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
    return _command(selector, operation, 1, fields)


def _command(selector: str, operation: str, version: int, fields: dict[str, Field]) -> str:
    return f'\x1b]777;{token(selector)};' + encode_fields(
        {'v': version, 'op': token(operation), **fields}) + ST


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


@dataclass(frozen=True)
class ImageAnnotation:
    """A plain-text description of an image placement."""

    name: str = ''
    description: str = ''
    details: str = ''
    purpose: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate purpose and the text accepted by Silkie."""
        if self.purpose not in {None, 'essential', 'informative', 'decorative'}:
            raise ValueError('Unknown image annotation purpose')
        for text, multiline in ((self.name, False), (self.description, True), (self.details, True)):
            _validate_annotation_text(text, multiline)


def _validate_annotation_text(text: str, multiline: bool) -> None:
    if not isinstance(text, str):
        raise TypeError('Accessibility fields must be text')
    for char in text:
        code = ord(char)
        forbidden = code < 32 and not (multiline and char in '\t\n')
        if forbidden or 127 <= code <= 159 or 0xd800 <= code <= 0xdfff:
            raise ValueError('Accessibility text must be valid UTF-8 with no control characters')


def _annotation_ids(image_id: int, placement_id: int, request_id: Optional[int]
                    ) -> dict[str, Field]:
    fields: dict[str, Field] = {'i': integer(image_id, 'image_id', 1, 0xffffffff),
                                'p': integer(placement_id, 'placement_id', 1, 0xffffffff)}
    if request_id is not None:
        fields['id'] = integer(request_id, 'request_id', 0, 0x7fffffff)
    return fields


def annotate_image(image_id: int, placement_id: int, annotation: ImageAnnotation, *,
                   request_id: Optional[int] = None) -> str:
    """Encode an annotation which replaces all metadata on an existing placement."""
    fields = _annotation_ids(image_id, placement_id, request_id)
    if annotation.purpose is not None:
        fields['purpose'] = annotation.purpose
    for key in ('name', 'description', 'details'):
        value = getattr(annotation, key)
        if value:
            fields[key] = quote(value, safe='')
    return command('a11y', 'set-image', **fields)


def clear_image_annotation(image_id: int, placement_id: int, *,
                           request_id: Optional[int] = None) -> str:
    """Encode a request to remove metadata from an existing placement."""
    return command('a11y', 'clear-image', **_annotation_ids(image_id, placement_id, request_id))


def _text_command(operation: str, request_id: Optional[int], **fields: Field) -> str:
    if request_id is not None:
        fields['id'] = integer(request_id, 'request_id', 0, 0x7fffffff)
    return _command('a11y', operation, 2, fields)


def annotate_text(range_id: int, start: tuple[int, int], end: tuple[int, int], *,
                  role: str, label: str = '', request_id: Optional[int] = None) -> str:
    """
    Label a screen range with one-based (row, column) coordinates.

    The start is inclusive and the end is exclusive. The end column can be one past
    the last column. The terminal checks the range against the active screen grid.

    :raises ValueError: If the ID, coordinates, role or label are invalid.
    """
    row, column = start
    end_row, end_column = end
    fields: dict[str, Field] = {
        'range': integer(range_id, 'range_id', 1, 0xffffffff),
        'row': integer(row, 'row', 1, 0x7fffffff),
        'column': integer(column, 'column', 1, 0x7fffffff),
        'end_row': integer(end_row, 'end_row', 1, 0x7fffffff),
        'end_column': integer(end_column, 'end_column', 1, 0x7fffffff),
    }
    if (row, column) >= (end_row, end_column):
        raise ValueError('The text range must end after its start')
    if role not in ('prompt', 'status', 'completion', 'decorative'):
        raise ValueError('Unknown text annotation role')
    fields['role'] = role
    _validate_annotation_text(label, False)
    if label:
        fields['label'] = quote(label, safe='')
    return _text_command('set-text', request_id, **fields)


def clear_text_annotation(range_id: int, *, request_id: Optional[int] = None) -> str:
    """
    Encode a request to remove a previously annotated text range.

    :raises ValueError: If a supplied ID is outside the protocol range.
    """
    range_id = integer(range_id, 'range_id', 1, 0xffffffff)
    return _text_command('clear-text', request_id, range=range_id)


def announce(text: str, *, priority: str = 'polite', request_id: Optional[int] = None) -> str:
    """
    Encode one screen-reader announcement with optional assertive priority.

    :raises ValueError: If the text, priority or request ID is invalid.
    """
    _validate_annotation_text(text, False)
    if not text:
        raise ValueError('Announcement text must be nonempty')
    if priority not in ('polite', 'assertive'):
        raise ValueError('Unknown announcement priority')
    return _text_command('announce', request_id, text=quote(text, safe=''), priority=priority)


@dataclass(frozen=True)
class SpeechStyle:  # pylint: disable=too-many-instance-attributes
    """Voice Lock settings for a reusable style or an individual utterance."""

    voice: str | None = None
    language: str | None = None
    rate: int | None = None
    pitch: int | None = None
    pitch_range: int | None = None
    volume: int | None = None
    punctuation: str | None = None
    split_caps: bool | None = None

    def _fields(self) -> dict[str, Field]:
        result: dict[str, Field] = {}
        for field in dataclass_fields(self):
            value = getattr(self, field.name)
            if value is not None:
                result[field.name] = (int(value) if field.name == 'split_caps'
                                      and isinstance(value, bool) else value)
        return result


_SPEECH_INTEGER_FIELDS = {
    'rate': (-100, 100), 'pitch': (-100, 100), 'pitch_range': (-100, 100),
    'volume': (0, 100), 'duration': (0, 0x7fffffff), 'id': (0, 0x7fffffff),
    'queued': (0, 1), 'interrupt': (0, 1), 'split_caps': (0, 1),
}


def speech_command(operation: str, **fields: Field) -> str:
    """
    Encode an OSC 777 speech command with unescaped text and the documented field names.

    Numeric fields take integers, and the optional wire field ``id`` requests an acknowledgement.

    :raises ValueError: If speech fields are outside their wire ranges or contain invalid text.
    """
    text = fields.get('text', '')
    if operation == 'character' and (not isinstance(text, str) or len(text) != 1):
        raise ValueError('Character echo requires one Unicode character')
    if operation in {'speak', 'queue'} and (not isinstance(text, str) or not text.strip()):
        raise ValueError('Speech text must be nonempty')
    for key, value in fields.items():
        if key in _SPEECH_INTEGER_FIELDS:
            low, high = _SPEECH_INTEGER_FIELDS[key]
            if not isinstance(value, int):
                raise ValueError(f'{key} must be an integer')
            integer(value, key, low, high)
        elif key in {'style', 'name', 'language'}:
            if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', value):
                raise ValueError(f'Invalid speech {key}')
        elif key in {'text', 'voice'}:
            if not isinstance(value, str):
                raise ValueError(f'{key} must be text')
            _validate_annotation_text(value, key == 'text')
            fields[key] = quote(value, safe='')
        elif key == 'punctuation' and value not in ('none', 'some', 'all'):
            raise ValueError('Unknown speech punctuation level')
    return command('speech', operation, **fields)


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
            name = token(name)
            self.protocols[name] = max(self.protocols.get(name, 0), int(version))
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


class DisplayChannel(Channel):
    """Report host display capabilities and desktop preferences over the terminal stream."""

    def __init__(self, client: 'Silkie'):
        """Bind the display selector."""
        super().__init__(client, 'display')

    def query(self, timeout: Optional[float] = 1.0) -> Optional[DisplayEvent]:
        """Query display capabilities and the current desktop preferences."""
        report = super().query(timeout=timeout)
        return report if isinstance(report, DisplayEvent) else None


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


class SpeechChannel(Channel):
    """Direct a terminal host's speech queue and Voice Lock styles for the current pane."""

    def __init__(self, client: 'Silkie'):
        """Bind the speech selector to the terminal connection."""
        super().__init__(client, 'speech')

    def command(self, operation: str, **fields: Field) -> str:
        """Encode a speech command, percent-encoding utterance text and voice names."""
        return speech_command(operation, **fields)

    def query(self, timeout: Optional[float] = 1.0) -> Optional[SpeechCapabilities]:
        """Read native backend capabilities, returning None if the host does not reply."""
        report = super().query(timeout)
        return report if isinstance(report, SpeechCapabilities) else None

    def supported(self, timeout: Optional[float] = 1.0) -> bool:
        """Return whether the terminal reports an available speech backend."""
        report = self.query(timeout)
        return report is not None and report.available

    def _execute(self, operation: str, wait: bool, timeout: Optional[float],
                 **fields: Field) -> Optional[SpeechAck]:
        if not wait:
            self.send(operation, **fields)
            return None
        request_id = self.client.next_id()
        event = self.client.request(self.command(operation, id=request_id, **fields),
                                    self.selector, 'ack', timeout, request_id)
        return event if isinstance(event, SpeechAck) else None

    def _utterance(self, operation: str, text: str, style: str | None,
                   options: SpeechStyle | None, *, wait: bool, timeout: Optional[float],
                   **fields: Field) -> Optional[SpeechAck]:
        if style is not None:
            fields['style'] = style
        if options is not None:
            fields.update(options._fields())
        return self._execute(operation, wait, timeout, text=text, **fields)

    def speak(self, text: str, *, style: str | None = None, options: SpeechStyle | None = None,
              interrupt: bool = False, wait: bool = False, timeout: Optional[float] = 1.0
              ) -> Optional[SpeechAck]:
        """Send an utterance for playback, optionally clearing the pane's speech first."""
        return self._utterance('speak', text, style, options, wait=wait, timeout=timeout,
                               interrupt=int(interrupt))

    def queue(self, text: str, *, style: str | None = None, options: SpeechStyle | None = None,
              interrupt: bool = False, wait: bool = False, timeout: Optional[float] = 1.0
              ) -> Optional[SpeechAck]:
        """Queue a styled utterance for the next dispatch."""
        return self._utterance('queue', text, style, options, wait=wait, timeout=timeout,
                               interrupt=int(interrupt))

    def character(self, text: str, *, style: str | None = None, options: SpeechStyle | None = None,
                  interrupt: bool = False, queued: bool = False, wait: bool = False,
                  timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Speak one Unicode character using the host's character-echo behavior."""
        return self._utterance('character', text, style, options, wait=wait, timeout=timeout,
                               interrupt=int(interrupt), queued=int(queued))

    def sound(self, name: str, *, queued: bool = False, interrupt: bool = False,
              wait: bool = False, timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Play a native sound by name, or enqueue it for dispatch."""
        return self._execute('sound', wait, timeout, name=name, queued=int(queued),
                             interrupt=int(interrupt))

    def silence(self, duration: int, *, queued: bool = False, interrupt: bool = False,
                wait: bool = False, timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Insert a duration in milliseconds into playback, or enqueue it for dispatch."""
        return self._execute('silence', wait, timeout, duration=duration, queued=int(queued),
                             interrupt=int(interrupt))

    def set_style(self, name: str, options: SpeechStyle, *, wait: bool = False,
                  timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Define or replace a named Voice Lock style in the pane."""
        return self._execute('set-style', wait, timeout, style=name, **options._fields())

    def clear_style(self, name: str, *, wait: bool = False,
                    timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Remove a previously defined style."""
        return self._execute('clear-style', wait, timeout, style=name)

    def dispatch(self, *, wait: bool = False, timeout: Optional[float] = 1.0
                 ) -> Optional[SpeechAck]:
        """Send the queued utterances, sounds, and silence to playback in order."""
        return self._execute('dispatch', wait, timeout)

    def pause(self, *, wait: bool = False, timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Pause playback in the current pane."""
        return self._execute('pause', wait, timeout)

    def resume(self, *, wait: bool = False, timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Resume paused speech in the current pane."""
        return self._execute('resume', wait, timeout)

    def stop(self, *, wait: bool = False, timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Interrupt playback and clear the pane's speech queues."""
        return self._execute('stop', wait, timeout)

    def reset(self, *, wait: bool = False, timeout: Optional[float] = 1.0) -> Optional[SpeechAck]:
        """Stop playback, clear the queues, and remove the pane's Voice Lock styles."""
        return self._execute('reset', wait, timeout)


class Accessibility:
    """Silkie image and text annotations and screen-reader announcements."""

    def __init__(self, client: 'Silkie'):
        """Bind annotation writes and acknowledgements to a Silkie client."""
        self.client = client

    def supported(self, timeout: Optional[float] = 1.0, *, force: bool = False,
                  version: int = 1) -> bool:
        """Check discovery for the required accessibility version."""
        caps = self.client.capabilities(timeout, force=force)
        return caps is not None and caps.supports('a11y', version)

    def _send(self, sequence: str, version: int, request_id: Optional[int],
              timeout: Optional[float]) -> Optional[AccessibilityAck]:
        if request_id is None:
            self.client.write(sequence)
            return None
        event = self.client.request(sequence, 'a11y', 'ack', timeout, request_id, v=version)
        return event if isinstance(event, AccessibilityAck) else None

    def set_image(self, image_id: int, placement_id: int, annotation: ImageAnnotation, *,
                  wait: bool = True, timeout: Optional[float] = 1.0) -> Optional[AccessibilityAck]:
        """Annotate a placed image and optionally return the correlated terminal reply."""
        request_id = self.client.next_id() if wait else None
        return self._send(annotate_image(image_id, placement_id, annotation, request_id=request_id),
                          1, request_id, timeout)

    def clear_image(self, image_id: int, placement_id: int, *, wait: bool = True,
                    timeout: Optional[float] = 1.0) -> Optional[AccessibilityAck]:
        """Clear a placement's annotation and optionally return the correlated terminal reply."""
        request_id = self.client.next_id() if wait else None
        return self._send(clear_image_annotation(image_id, placement_id, request_id=request_id),
                          1, request_id, timeout)

    def set_text(self, range_id: int, start: tuple[int, int], end: tuple[int, int], *,
                 role: str, label: str = '', wait: bool = True, timeout: Optional[float] = 1.0
                 ) -> Optional[AccessibilityAck]:
        """Label a screen range and optionally return the correlated terminal reply."""
        request_id = self.client.next_id() if wait else None
        sequence = annotate_text(range_id, start, end, role=role, label=label,
                                 request_id=request_id)
        return self._send(sequence, 2, request_id, timeout)

    def clear_text(self, range_id: int, *, wait: bool = True,
                   timeout: Optional[float] = 1.0) -> Optional[AccessibilityAck]:
        """Clear a screen range's annotation and optionally return the terminal reply."""
        request_id = self.client.next_id() if wait else None
        return self._send(clear_text_annotation(range_id, request_id=request_id),
                          2, request_id, timeout)

    def announce(self, text: str, *, priority: str = 'polite', wait: bool = True,
                 timeout: Optional[float] = 1.0) -> Optional[AccessibilityAck]:
        """Request one screen-reader announcement and optionally return the terminal reply."""
        request_id = self.client.next_id() if wait else None
        return self._send(announce(text, priority=priority, request_id=request_id),
                          2, request_id, timeout)


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
        self.display = DisplayChannel(self)
        self.latency = Channel(self, 'latency')
        self.frame = Channel(self, 'frame')
        self.depth = Channel(self, 'depth')
        self.ssh_agent = AgentChannel(self, 'ssh-agent')
        self.speech = SpeechChannel(self)
        self.accessibility = Accessibility(self)

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
        checks = [r'(?=[^\x1b\x07]*(?:;|:)op=' + re.escape(operation) + r'(?=:|\x07|\x1b))']
        reply_fields.setdefault('v', 1)
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
