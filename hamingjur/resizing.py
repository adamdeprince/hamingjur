"""Scoped SIGWINCH wakeups for synchronous and asynchronous terminal input."""
# This transport helper intentionally owns Terminal's private wakeup descriptors.
# pylint: disable=protected-access
from __future__ import annotations

# std imports
import os
import signal
import contextlib
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    # local
    from .terminal import Terminal


@contextlib.contextmanager
def signal_resizes(terminal: Terminal) -> Iterator[None]:
    """
    Wake input with a pipe; signals coalesce into the current OS window size.

    :raises RuntimeError: If signal fallback is entered outside the main thread.
    """
    if terminal._resize_read_fd is not None:
        yield
        return
    if not hasattr(signal, 'SIGWINCH'):
        yield
        return
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    previous = signal.getsignal(signal.SIGWINCH)

    def changed(signum: int, frame: object) -> None:
        try:
            os.write(write_fd, b'\0')
        except BlockingIOError:
            pass  # A full pipe already wakes the reader, which queries the latest size.
        if callable(previous):
            previous(signum, frame)

    try:
        try:
            signal.signal(signal.SIGWINCH, changed)
        except ValueError as exc:
            raise RuntimeError(
                'SIGWINCH resize fallback must be entered on the main thread') from exc
        terminal._resize_read_fd = read_fd
        try:
            yield
        finally:
            terminal._resize_read_fd = None
            terminal._pending_resize = None
            if signal.getsignal(signal.SIGWINCH) is changed:
                signal.signal(signal.SIGWINCH, previous)
    finally:
        os.close(read_fd)
        os.close(write_fd)
