"""ECMA-48 control-sequence encoders for terminal operations and future protocols."""
# std imports
import re
from typing import Union, Optional

# local
from .protocols import ST, integer


def csi(final: str, *parameters: Optional[Union[int, str]],
        private: str = '', intermediate: str = '') -> str:
    """
    Encode a CSI instruction with numeric/subparameters, private and intermediate bytes.

    None preserves an omitted parameter. Strings allow colon-separated subparameters, such as SGR
    underline styles. No arbitrary parameter-count or string-size limit applies.

    :raises ValueError: If fields do not follow ECMA-48 CSI grammar.
    """
    if len(final) != 1 or not '@' <= final <= '~':
        raise ValueError('CSI requires one final byte from 0x40 through 0x7e')
    if any(not '<' <= char <= '?' for char in private):
        raise ValueError('CSI private bytes must be from 0x3c through 0x3f')
    if any(not ' ' <= char <= '/' for char in intermediate):
        raise ValueError('CSI intermediate bytes must be from 0x20 through 0x2f')
    values = []
    for value in parameters:
        if value is None:
            values.append('')
        elif isinstance(value, str) and re.fullmatch(r'[0-9:]*', value):
            values.append(value)
        else:
            values.append(str(integer(value, 'CSI parameter')))
    return '\x1b[' + private + ';'.join(values) + intermediate + final


def _text(payload: str) -> str:
    if any(ord(char) < 32 or 0x7f <= ord(char) < 0xa0 for char in payload):
        raise ValueError('String payloads cannot contain terminal control characters')
    return payload


def osc(code: int, payload: str = '') -> str:
    """
    Encode an OSC string, including Unicode, without allowing control injection.

    :raises ValueError: If the code or payload is invalid.
    """
    return f'\x1b]{integer(code, "OSC code")};{_text(payload)}{ST}'


def dcs(payload: str) -> str:
    """
    Encode a DCS string, such as a terminal capability query or encoded data.

    :raises ValueError: If the payload contains terminal controls.
    """
    return '\x1bP' + _text(payload) + ST


def apc(payload: str) -> str:
    """
    Encode an APC string for an application-defined protocol.

    :raises ValueError: If the payload contains terminal controls.
    """
    return '\x1b_' + _text(payload) + ST
