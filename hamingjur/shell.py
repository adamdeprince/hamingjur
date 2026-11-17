"""Shell working-directory and prompt/command markers."""
from __future__ import annotations

# std imports
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

# local
from .control import osc
from .protocols import integer

if TYPE_CHECKING:
    # local
    from .terminal import Terminal


def working_directory(path: str, *, host: str = '') -> str:
    """
    Encode an absolute working directory as an OSC 7 file URI.

    :raises ValueError: If the path is relative or the host contains URI delimiters.
    """
    if not path.startswith('/'):
        raise ValueError('Working directory must be an absolute path')
    if any(char in host for char in '/?#@'):
        raise ValueError('Host must not contain URI delimiters')
    return osc(7, 'file://' + quote(host, safe='[]:') + quote(path, safe='/'))


def mark(kind: str, *, status: Optional[int] = None, **fields: str) -> str:
    """
    Encode OSC 133 prompt (A), input (B), output (C), or command completion (D).

    Optional protocol fields are preserved. A prompt marker can reset Silkie input modes;
    applications using those modes should enable them after marking their prompt.

    :raises ValueError: If marker fields contain delimiters or status is misplaced.
    """
    if kind not in {'A', 'B', 'C', 'D'}:
        raise ValueError('Shell marker must be A, B, C, or D')
    parts = [kind]
    if status is not None:
        if kind != 'D':
            raise ValueError('Only command completion has an exit status')
        parts.append(str(integer(status, 'exit status')))
    for key, value in fields.items():
        if not key or any(char in key + value for char in ';='):
            raise ValueError('Shell marker fields cannot contain delimiters')
        parts.append(f'{key}={value}')
    return osc(133, ';'.join(parts))


class Shell:
    """Explicit shell integration output, available as ``term.shell``."""

    def __init__(self, terminal: Terminal):
        """Bind shell markers to a terminal."""
        self.terminal = terminal

    def working_directory(self, path: str, *, host: str = '') -> None:
        """Report the shell's working directory without changing the Python process directory."""
        self.terminal.send(working_directory(path, host=host))

    def mark(self, kind: str, *, status: Optional[int] = None, **fields: str) -> None:
        """Report a prompt or command boundary."""
        self.terminal.send(mark(kind, status=status, **fields))
