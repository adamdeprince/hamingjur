"""Pure Python framing and events for Kitty and Silkie terminal protocols."""
# std imports
import re
import math
import base64
from typing import Dict, Union, Mapping, Optional
from dataclasses import dataclass

ST = '\x1b\\'
Field = Union[str, int]
_TOKEN = re.compile(r'[a-zA-Z_][a-zA-Z0-9_.-]*\Z')


def integer(value: int, name: str, minimum: int = 0,
            maximum: Optional[int] = None) -> int:
    """
    Validate an integer, with upper bounds only where the protocol requires them.

    :raises ValueError: If the value is not an integer in the protocol range.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{name} is outside the protocol range')
    return value


def token(value: str) -> str:
    """
    Validate a protocol selector or field name without restricting its length.

    :raises ValueError: If the name contains characters outside the protocol alphabet.
    """
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f'Invalid protocol name: {value!r}')
    return value


def safe(value: Field, forbidden: str = ':;') -> str:
    """
    Validate an ASCII field so it cannot inject another field or escape sequence.

    :raises ValueError: If a field contains control characters or delimiters.
    """
    result = str(value)
    if any(ord(char) < 33 or ord(char) > 126 or char in forbidden for char in result):
        raise ValueError('Protocol fields must contain printable ASCII without delimiters')
    return result


def encode_fields(fields: Mapping[str, Field], separator: str = ':') -> str:
    """Encode fields, retaining insertion order and unknown extension keys."""
    return separator.join(f'{token(key)}={safe(value, separator + ";")}'
                          for key, value in fields.items())


def parse_fields(text: str, separator: str = ':', *, repeated: bool = False) -> Dict[str, str]:
    """
    Decode fields; reject duplicate keys and malformed framing.

    :raises ValueError: If fields are malformed or contain forbidden duplicate keys.
    """
    fields: Dict[str, str] = {}
    if not text:
        return fields
    for item in text.split(separator):
        key, equals, value = item.partition('=')
        if not equals or (key in fields and not repeated):
            raise ValueError('Malformed or duplicate protocol field')
        fields[token(key)] = safe(value, separator + ';')
    return fields


def b64(data: bytes) -> str:
    """Encode binary data for terminal transport."""
    return base64.b64encode(data).decode('ascii')


def unb64(data: str) -> bytes:
    """Decode base64, accepting the optional padding used by Kitty protocols."""
    return base64.b64decode(data + '=' * (-len(data) % 4), validate=True)


def agent_token(value: str) -> str:
    """
    Validate an SSH-agent ID, connection or generation, with no length cap.

    :raises ValueError: If the token is empty or outside its wire alphabet.
    """
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', value):
        raise ValueError('Agent tokens use ASCII letters, digits, periods, underscores and hyphens')
    return value


def agent_packet(data: bytes) -> bytes:
    """
    Validate exactly one SSH agent packet, including its unsigned 32-bit length.

    :raises ValueError: If the packet is truncated or has trailing data.
    """
    if (not isinstance(data, bytes) or len(data) < 5
            or len(data) - 4 != int.from_bytes(data[:4], 'big')):
        raise ValueError('Expected one complete SSH agent packet including its length')
    return data


def notification_hops(route: str) -> tuple[tuple[str, str, int], ...]:
    """
    Decode an OSC 99 return route, ordered from application to outer terminal.

    :raises ValueError: If a hop has invalid components, delimiters or a window ID.
    """
    hops = []
    for hop in route.split(',') if route else ():
        match = re.fullmatch(r'([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/([0-9]+)', hop)
        if match is None:
            raise ValueError('Notification hops require component/instance/window')
        hops.append((match[1], match[2], int(match[3])))
    return tuple(hops)


@dataclass(frozen=True)
class ProtocolEvent:
    """A complete terminal message, preserving all fields and its wire representation."""

    protocol: str
    fields: Mapping[str, str]
    payload: str = ''
    raw: str = ''

    def integer(self, key: str, default: Optional[int] = None) -> Optional[int]:
        """
        Read a decimal integer field without losing missing-value information.

        :raises ValueError: If the value is not an integer in the protocol range.
        """
        value = self.fields.get(key)
        if value is None:
            return default
        if not re.fullmatch(r'-?[0-9]+', value):
            raise ValueError(f'{key} is not a decimal integer')
        return int(value)

    def number(self, key: str) -> Optional[float]:
        """
        Read a finite numeric field, such as display headroom or scale.

        :raises ValueError: If the field is not a finite number.
        """
        if key not in self.fields:
            return None
        result = float(self.fields[key])
        if not math.isfinite(result):
            raise ValueError(f'{key} is not finite')
        return result

    @property
    def data(self) -> bytes:
        """Decode a binary payload (call only for payload types defined as base64)."""
        return unb64(self.payload)

    def values(self, key: str) -> tuple[str, ...]:
        """All values for a repeated notification metadata key, in wire order."""
        if self.protocol == 'notification':
            header = self.raw.split(';', 2)[1]
            return tuple(item.partition('=')[2] for item in header.split(':')
                         if item.partition('=')[0] == key)
        return (self.fields[key],) if key in self.fields else ()

    @property
    def operation(self) -> str:
        """Return the Silkie operation or Kitty message type/action."""
        return self.fields.get('op', self.fields.get('type',
                               self.fields.get('t', self.fields.get('ac', ''))))

    @property
    def notification_route(self) -> tuple[tuple[str, str, int], ...]:
        """Notification return hops; effective actions remain in fields['a']."""
        if self.protocol == 'notification':
            return notification_hops(self.fields.get('r', ''))
        return ()

    @property
    def timestamp(self) -> Optional[float]:
        """Silkie monotonic event time in seconds, including microsecond precision."""
        if 't' not in self.fields or not self.protocol.startswith('silkie:'):
            return None
        return (int(self.fields['t']) + int(self.fields.get('tu', '0')) / 1000) / 1000

    @property
    def position(self) -> Optional[tuple[float, float]]:
        """Silkie physical pixel position; raw 1/256-pixel values remain in fields."""
        if 'x' not in self.fields or 'y' not in self.fields:
            return None
        scale = 256 if self.protocol.startswith('silkie:') else 1
        return int(self.fields['x']) / scale, int(self.fields['y']) / scale


@dataclass(frozen=True)
class TabletEvent(ProtocolEvent):
    """Pen, touch, tablet-pad, device and tool reports, including future fields."""

    @property
    def position(self) -> Optional[tuple[float, float]]:
        """Physical pixels for screen reports; touchpad reports use touchpad_position."""
        if self.fields.get('kind') == '2' and self.fields.get('src') == '1':
            return None
        return super().position

    @property
    def touchpad_position(self) -> Optional[tuple[float, float]]:
        """Normalized finger position on a touchpad, distinct from screen pixels."""
        if self.fields.get('kind') != '2' or self.fields.get('src') != '1':
            return None
        if 'x' not in self.fields or 'y' not in self.fields:
            return None
        return int(self.fields['x']) / 65535, int(self.fields['y']) / 65535

    @property
    def pressure(self) -> Optional[float]:
        """Normalized pressure, or None when the report does not mark it valid."""
        if not int(self.fields.get('valid', '0')) & 1 or 'p' not in self.fields:
            return None
        return int(self.fields['p']) / 65535

    @property
    def tilt(self) -> Optional[tuple[float, float]]:
        """Pen tilt in degrees, or None when absent or not valid."""
        if not int(self.fields.get('valid', '0')) & 2:
            return None
        if 'tx' not in self.fields or 'ty' not in self.fields:
            return None
        return int(self.fields['tx']) / 100, int(self.fields['ty']) / 100

    @property
    def device_name(self) -> Optional[str]:
        """Decode a device report's UTF-8 name."""
        value = self.fields.get('name')
        return unb64(value).decode('utf-8') if value is not None else None


@dataclass(frozen=True)
class AgentEvent(ProtocolEvent):
    """SSH-agent transport reply, retaining generation, connection and failure fields."""

    @property
    def data(self) -> bytes:
        """Decode and validate the complete packet in the data field of a reply."""
        return agent_packet(unb64(self.fields['data']))


# These are complete frames, never a fixed-size input buffer. Unknown OSC 777
# selectors are retained so applications can use extensions added by terminals.
FRAME_RE = re.compile(
    r'\x1b\](?:777|99|5522|5113|72|22|21)(?:;[^\x1b\x07]*)?(?:\x07|\x1b\\)'
    r'|\x1b_G[^\x1b]*\x1b\\'
    r'|\x1bP@kitty-cmd[^\x1b]*\x1b\\'
    r'|\x1b\[>[0-9:;]* q')
FRAME_PREFIXES = ('\x1b]777;', '\x1b]99;', '\x1b]5522;', '\x1b]5113;',
                  '\x1b]72;', '\x1b]22;', '\x1b]21;', '\x1b_G', '\x1bP@kitty-cmd')


def incomplete_frame(text: str) -> bool:
    """Whether text starts with a recognized but unfinished string protocol."""
    if FRAME_RE.match(text):
        return False
    return any(text.startswith(prefix) for prefix in FRAME_PREFIXES) and not (
        '\x07' in text or ST in text)


def parse_event(raw: str) -> ProtocolEvent:
    """
    Parse one complete protocol frame, rejecting incomplete or trailing data.

    :raises ValueError: If framing, fields, or the protocol version are invalid.
    """
    if not FRAME_RE.fullmatch(raw):
        raise ValueError('Expected exactly one complete protocol frame')
    if raw.startswith('\x1bP@kitty-cmd'):
        return ProtocolEvent('remote-control', {}, raw[12:-2], raw)
    if raw.startswith('\x1b_G'):
        header, _, payload = raw[3:-2].partition(';')
        return ProtocolEvent('graphics', parse_fields(header, ','), payload, raw)
    if raw.startswith('\x1b[>'):
        return ProtocolEvent('cursors', {}, raw[3:-2], raw)
    body = raw[2:-1] if raw.endswith('\x07') else raw[2:-2]
    code, _, body = body.partition(';')
    if code == '777':
        selector, separator, header = body.partition(';')
        if not separator:
            raise ValueError('Missing Silkie fields')
        fields = parse_fields(header)
        if fields.get('v') != '1' or not fields.get('op'):
            raise ValueError('Unsupported Silkie version or missing operation')
        event_type = {'tablet-pointer': TabletEvent, 'ssh-agent': AgentEvent}.get(
            selector, ProtocolEvent)
        return event_type('silkie:' + token(selector), fields, raw=raw)
    if code == '5113':
        return ProtocolEvent('file-transfer', parse_fields(body, ';'), raw=raw)
    if code in {'21', '22'}:
        return ProtocolEvent('colors' if code == '21' else 'pointer', {}, body, raw)
    header, _, payload = body.partition(';')
    protocol = {'99': 'notification', '5522': 'clipboard', '72': 'dnd'}[code]
    return ProtocolEvent(protocol, parse_fields(header, repeated=code == '99'), payload, raw)
