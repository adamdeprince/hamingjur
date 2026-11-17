"""Signal wakeups share resize events without corrupting pending terminal input."""
# std imports
import io
import os
import signal
import codecs
import asyncio
import threading
from unittest import mock

# 3rd party
import pytest

# local
from hamingjur import silkie
from hamingjur.terminal import WINSZ
from .accessories import TestTerminal

pytestmark = pytest.mark.skipif(not hasattr(signal, 'SIGWINCH'), reason='Requires SIGWINCH')


@pytest.fixture(name='term')
def terminal_fixture(monkeypatch):
    """Use a real pipe for keyboard readiness and no inline resize support."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    terminal = TestTerminal(stream=io.StringIO(), force_styling=True)
    terminal._dec_mode_cache[2048] = 0
    reader, writer = os.pipe()
    terminal._keyboard_fd = reader
    terminal._keyboard_decoder = codecs.getincrementaldecoder('utf-8')()
    terminal._test_writer = writer
    try:
        yield terminal
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_signal_wakes_blocking_input_and_restores_handler(term, asynchronous):
    """SIGWINCH interrupts sync/async waits, chains the old handler and refreshes cell pixels."""
    old_handler = signal.getsignal(signal.SIGWINCH)
    calls = []

    def previous(signum, frame):
        calls.append(signum)
    signal.signal(signal.SIGWINCH, previous)
    try:
        with mock.patch.object(term, '_height_and_width', return_value=WINSZ(40, 100, 1200, 1000)):
            with term.notify_on_resize(timeout=0):
                descriptor = term._resize_read_fd
                with term.notify_on_resize(timeout=0):
                    assert term._resize_read_fd == descriptor
                    timer = threading.Timer(0.03, lambda: os.kill(os.getpid(), signal.SIGWINCH))
                    timer.start()
                    try:
                        event = (asyncio.run(term.async_inkey(timeout=1)) if asynchronous
                                 else term.inkey(timeout=1))
                    finally:
                        timer.join()
                    assert event.name == 'RESIZE_EVENT'
                    assert event._mode_values.height_chars == 40
                    assert event._mode_values.width_pixels == 1200
                    assert term.get_cell_size() == (12, 25)
                assert term._resize_read_fd == descriptor
            assert term._resize_read_fd is None
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert signal.getsignal(signal.SIGWINCH) is previous
        assert calls == [signal.SIGWINCH]
    finally:
        signal.signal(signal.SIGWINCH, old_handler)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_resize_during_fragmented_protocol_preserves_input(term, asynchronous):
    """A signal becomes a separate event and never gets inserted into a partial OSC payload."""
    sequence = silkie.command('tablet-pointer', 'device', name='a' * 10000)
    with mock.patch.object(term, '_height_and_width', return_value=WINSZ(25, 80, 800, 500)):
        with term.notify_on_resize(timeout=0):
            term.ungetch(sequence[:-2])
            timer = threading.Timer(0.03, lambda: os.kill(os.getpid(), signal.SIGWINCH))
            timer.start()
            try:
                event = (asyncio.run(term.async_inkey(timeout=1)) if asynchronous
                         else term.inkey(timeout=1))
            finally:
                timer.join()
            assert event.name == 'RESIZE_EVENT'
            os.write(term._test_writer, b'\x1b\\z')
            event = (asyncio.run(term.async_inkey(timeout=1)) if asynchronous
                     else term.inkey(timeout=1))
            assert event.tablet.raw == sequence
            assert term.inkey(timeout=0) == 'z'


def test_queries_can_read_keyboard_while_signal_pending(term):
    """A query's bulk reader drains wakeups without blocking on an unreadable keyboard fd."""
    with mock.patch.object(term, '_height_and_width', return_value=WINSZ(25, 80, 800, 500)):
        with term.notify_on_resize(timeout=0):
            os.kill(os.getpid(), signal.SIGWINCH)
            assert term._read_available() == ''
            os.write(term._test_writer, b'reply')
            assert term._read_available() == 'reply'
            assert term.inkey(timeout=0).name == 'RESIZE_EVENT'


def test_signal_cleanup_on_exception(term):
    """An application exception restores mode state, handler and owned file descriptors."""
    previous = signal.getsignal(signal.SIGWINCH)
    with pytest.raises(RuntimeError, match='application'):
        with term.notify_on_resize(timeout=0):
            raise RuntimeError('application')
    assert signal.getsignal(signal.SIGWINCH) is previous
    assert term._resize_read_fd is None and term._dec_mode_cache[2048] == 0
