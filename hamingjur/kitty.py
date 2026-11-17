"""Kitty terminal protocol commands, including clipboard, DnD and file transfer."""
from __future__ import annotations

# std imports
import re
import uuid
import contextlib
from typing import TYPE_CHECKING, Union, Mapping, Iterable, Iterator, Optional

# local
from .graphics import Data, chunks
from .protocols import ST, Field, b64, safe, token, integer, encode_fields, notification_hops


def notification_route(hops: Iterable[tuple[str, str, int]]) -> str:
    """Encode Silkie notification return hops for the r metadata field, without a hop cap."""
    route = ','.join(f'{component}/{instance}/{integer(window, "window")}'
                     for component, instance, window in hops)
    notification_hops(route)
    return route


def notification_packet(payload: str = '', *,
                        metadata: Iterable[tuple[str, Field]] = ()) -> str:
    """
    Encode notification controls with repeated keys (such as multiple icon names/types).

    :raises ValueError: If arguments or protocol data are invalid.
    """
    fields = []
    for key, value in metadata:
        if not re.fullmatch('[a-zA-Z]', key):
            raise ValueError('Notification keys are single ASCII letters')
        fields.append(f'{key}={safe(value)}')
    if len(payload.encode('utf-8')) > 4096:
        raise ValueError('Use notification() for large payloads')
    # Reuse packet validation for the payload, while retaining duplicate keys.
    packet(99, payload)
    return '\x1b]99;' + ':'.join(fields) + ';' + payload + ST


if TYPE_CHECKING:
    # local
    from .terminal import Terminal


def packet(code: int, payload: str = '', **fields: Field) -> str:
    """
    Encode an OSC metadata/payload packet, rejecting terminal control injection.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if any(ord(char) < 32 or 0x7f <= ord(char) < 0xa0 for char in payload):
        raise ValueError('Encode control characters in the payload using base64')
    return f'\x1b]{integer(code, "OSC code")};{encode_fields(fields)};{payload}{ST}'


def clipboard_read(mime_types: Iterable[str] = ('text/plain',), **fields: Field) -> str:
    """Request MIME types (or '.' to list types); responses arrive as protocol events."""
    return packet(5522, b64(' '.join(mime_types).encode('utf-8')), type='read', **fields)


def clipboard_write(content: Mapping[str, Data], **fields: Field) -> Iterator[str]:
    """Stream a multi-format clipboard transaction, then commit it."""
    yield packet(5522, type='write', **fields)
    for mime, data in content.items():
        encoded_mime = b64(mime.encode('utf-8'))
        # OSC 5522 explicitly limits each decoded data chunk to 4096 bytes.
        sent = False
        for block in chunks(data, 4096):
            sent = True
            yield packet(5522, b64(block), type='wdata', mime=encoded_mime)
        if not sent:
            yield packet(5522, type='wdata', mime=encoded_mime)
    yield packet(5522, type='wdata')


def clipboard_alias(mime: str, aliases: Iterable[str]) -> str:
    """Add MIME aliases between a clipboard write's opening and commit packets."""
    return packet(5522, b64(' '.join(aliases).encode()), type='walias', mime=b64(mime.encode()))


def notification(payload: Data, *, identifier: str, kind: str = 'title',
                 done: bool = True, **fields: Field) -> Iterator[str]:
    """
    Stream a notification payload, including text, icons and buttons, using base64.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', identifier):
        raise ValueError('Notification IDs use ASCII letters, digits, hyphen and underscore')
    # 2048 is the notification protocol's limit before base64 encoding.
    parts = iter(chunks(payload, 2048))
    current = next(parts, b'')
    while True:
        following = next(parts, None)
        yield packet(99, b64(current), i=identifier, p=kind, e=1,
                     d=int(done and following is None), **fields)
        if following is None:
            return
        current = following


def notify(title: str, body: str = '', *, identifier: Optional[str] = None,
           **fields: Field) -> Iterator[str]:
    """Create a desktop notification with arbitrarily long Unicode title and body."""
    identifier = identifier or uuid.uuid4().hex
    yield from notification(title.encode('utf-8'), identifier=identifier,
                            done=not body, **fields)
    if body:
        yield from notification(body.encode('utf-8'), identifier=identifier, kind='body', **fields)


def dnd(operation: str, payload: str = '', **fields: Field) -> str:
    """
    Encode any drag/drop operation with its documented t/x/y/X/Y/o/i keys.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if not re.fullmatch('[a-zA-Z]', operation):
        raise ValueError('DnD operations are single letters')
    if len(payload.encode('utf-8')) > 4096:
        raise ValueError('Use dnd_data() to chunk binary payloads larger than 4096 encoded bytes')
    return packet(72, payload, t=operation, **fields)


def dnd_data(operation: str, data: Data, **fields: Field) -> Iterator[str]:
    """
    Stream base64 DnD data/images, preserving metadata only on the first chunk.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if 'm' in fields:
        raise ValueError('dnd_data() manages continuation')
    parts = iter(chunks(data, 3072))  # DnD permits 4096 encoded bytes per packet.
    current = next(parts, b'')
    first = True
    while True:
        following = next(parts, None)
        metadata = {'t': operation, **fields} if first else {
            key: value for key, value in fields.items() if key == 'i'}
        yield packet(72, b64(current), **metadata, m=int(following is not None))
        if following is None:
            return
        current, first = following, False


_TRANSFER_KEYS = {
    'action': 'ac', 'compression': 'zip', 'file_type': 'ft', 'transmission_type': 'tt',
    'id': 'id', 'file_id': 'fid', 'bypass': 'pw', 'quiet': 'q', 'mtime': 'mod',
    'permissions': 'prm', 'size': 'sz', 'name': 'n', 'status': 'st', 'parent': 'pr', 'data': 'd',
}


def file_transfer(action: str, **fields: object) -> str:
    """
    Encode a file-transfer command using descriptive field names.

    Handles all commands and transports, including rsync signature/delta bytes. Applications own the
    transfer state, filesystem decisions and rsync algorithm. No local files are opened as a side
    effect of receiving a terminal message.

    :raises TypeError: If a payload has the wrong type.
    :raises ValueError: If arguments or protocol data are invalid.
    """
    if action not in {'send', 'file', 'data', 'end_data', 'receive', 'cancel', 'status', 'finish'}:
        raise ValueError('Unknown file transfer action')
    result: dict[str, Field] = {'ac': action}
    for key, value in fields.items():
        wire_key = _TRANSFER_KEYS.get(key, key)
        if wire_key in result:
            raise ValueError('Duplicate file transfer field')
        if wire_key in {'n', 'st'}:
            if not isinstance(value, str):
                raise TypeError(f'{key} must be a string')
            result[wire_key] = b64(value.encode('utf-8'))
        elif wire_key == 'd':
            if not isinstance(value, bytes):
                raise TypeError('data must be bytes')
            if len(value) > 4096:
                raise ValueError('Use file_data() for payloads larger than the wire chunk limit')
            result[wire_key] = b64(value)
        else:
            if not re.fullmatch(r'[0-9a-zA-Z_:./@-]*', str(value)):
                raise ValueError('Invalid file transfer field')
            result[token(wire_key)] = str(value)
    return '\x1b]5113;' + encode_fields(result, ';') + ST


def file_data(data: Data, **fields: object) -> Iterator[str]:
    """Stream one file/signature/delta, ending with end_data (no total-size cap)."""
    for block in chunks(data, 4096):
        yield file_transfer('data', data=block, **fields)
    yield file_transfer('end_data', **fields)


def pointer(shape: str = '', *, action: str = '=') -> str:
    """
    Set, push (>), pop (<) or query (?) shapes, including Silkie image:i:x:y pointers.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if action not in ('=', '>', '<', '?'):
        raise ValueError('Invalid pointer action')
    return '\x1b]22;' + action + safe(shape, ';') + ST


def colors(values: Mapping[Union[str, int], str]) -> str:
    """Set/query/reset colors; values are color specs, '?' for queries or '' for resets."""
    parts = []
    for key, value in values.items():
        if isinstance(key, int):
            integer(key, 'color index', 0, 255)
        else:
            token(key)
        parts.append(f'{key}={safe(value, ";")}')
    return '\x1b]21;' + ';'.join(parts) + ST


def cursors(shape: int, coordinates: Iterable[Iterable[int]] = ()) -> str:
    """Encode multi-cursor shapes/colors/queries; coordinate groups use the wire syntax."""
    groups = [str(integer(shape, 'shape'))]
    for group in coordinates:
        groups.append(':'.join(str(integer(item, 'coordinate', -(1 << 31), (1 << 31) - 1))
                               for item in group))
    return '\x1b[>' + ';'.join(groups) + ' q'


def region_style(top: int, left: int, bottom: int, right: int, sgr: str) -> str:
    """
    Set SGR attributes in a 1-based inclusive rectangle using extended DECCARA.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    bounds = [integer(value, 'coordinate', 1) for value in (top, left, bottom, right)]
    if bottom < top or right < left or not re.fullmatch(r'[0-9:;]+', sgr):
        raise ValueError('Invalid rectangle or SGR parameters')
    return '\x1b[2*x\x1b[' + ';'.join(map(str, bounds)) + ';' + sgr + '$r\x1b[*x'


class Kitty:
    """Explicit output and restoring contexts, bound to a Terminal instance."""

    def __init__(self, terminal: 'Terminal'):
        """Bind explicit output operations to a terminal's stream."""
        self.terminal = terminal

    def send(self, sequences: Union[str, Iterable[str]]) -> None:
        """Write one sequence or an iterator of chunks, flushing at completion."""
        self.terminal.send(sequences)

    @contextlib.contextmanager
    def pointer(self, shape: str) -> Iterator[None]:
        """Push a pointer shape and pop it on exit."""
        self.send(pointer(shape, action='>'))
        try:
            yield
        finally:
            self.send(pointer(action='<'))

    @contextlib.contextmanager
    def colors_saved(self) -> Iterator[None]:
        """Save and restore dynamic colors and the ANSI palette."""
        self.send('\x1b]30001' + ST)
        try:
            yield
        finally:
            self.send('\x1b]30101' + ST)

    @contextlib.contextmanager
    def modes_saved(self) -> Iterator[None]:
        """Save and restore all side-effect-free private modes (Kitty XTSAVE/XTRESTORE)."""
        self.send('\x1b[?s')
        try:
            yield
        finally:
            self.send('\x1b[?r')

    @contextlib.contextmanager
    def drops_enabled(self, mime_types: Iterable[str] = ()) -> Iterator[None]:
        """Accept drops during this context; disables acceptance on exit."""
        self.send(dnd('a', ' '.join(mime_types)))
        try:
            yield
        finally:
            self.send(dnd('A'))
