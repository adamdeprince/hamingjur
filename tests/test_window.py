"""Window geometry, standard controls, shell integration, and namespace ownership."""
# std imports
import io
import re
import sys
import asyncio
import subprocess
from unittest import mock

# 3rd party
import pytest

# local
from hamingjur import shell, window, control
from hamingjur.terminal import WINSZ
# local
from .accessories import TestTerminal


@pytest.fixture(name='term')
def terminal_fixture(monkeypatch):
    """Use an isolated stream and no ambient color policy."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    return TestTerminal(stream=io.StringIO(), force_styling=True)


@pytest.mark.parametrize('method,kwargs,query,reply,result', [
    ('cell_size', {}, '\x1b[16t', '\x1b[6;24;12t', window.Size(12, 24)),
    ('size', {}, '\x1b[18t', '\x1b[8;40;120t', window.Size(120, 40)),
    ('size', {'pixels': True}, '\x1b[14t', '\x1b[4;960;1440t', window.Size(1440, 960)),
    ('size', {'pixels': True, 'outer': True}, '\x1b[14;2t', '\x1b[4;1000;1500t',
     window.Size(1500, 1000)),
    ('screen_size', {}, '\x1b[19t', '\x1b[9;60;200t', window.Size(200, 60)),
    ('screen_size', {'pixels': True}, '\x1b[15t', '\x1b[5;1440;2560t',
     window.Size(2560, 1440)),
    ('position', {}, '\x1b[13t', '\x1b[3;-20;30t', (-20, 30)),
    ('position', {'text_area': True}, '\x1b[13;2t', '\x1b[3;10;40t', (10, 40)),
    ('state', {}, '\x1b[11t', '\x1b[2t', 2),
])
def test_window_reports(term, method, kwargs, query, reply, result):
    """Decode published wire examples with the correct units and axis order."""
    # pylint: disable=too-many-positional-arguments
    def answer(sequence, pattern, timeout):
        assert sequence == query
        assert timeout == 0.25
        return pattern.fullmatch(reply)

    with mock.patch.object(term, '_query_with_boundary', side_effect=answer):
        assert getattr(term.window, method)(timeout=0.25, **kwargs) == result


def test_cell_properties_cache_and_refresh(term):
    """Pixel properties share one query; explicit refresh observes a changed font."""
    replies = [(24, 12), (32, 16)]
    with mock.patch.object(term, '_get_xtwinops_cell_size', side_effect=replies) as query:
        assert term.cell_height == 24
        assert term.cell_width == 12
        assert query.call_count == 1
        assert term.get_cell_size(force=True) == window.Size(16, 32)
        assert term.cell_width == 16
        assert query.call_count == 2


@pytest.mark.parametrize('asynchronous', [False, True])
def test_resize_event_invalidates_cell_and_window_pixels(term, asynchronous):
    """Both input paths replace observed dimensions and discard old font geometry."""
    # Exercise cache invalidation through the public event readers.
    # pylint: disable=protected-access
    term._xtwinops_cell_cache = (16, 8)
    term._xtwinops_cache = (400, 640)
    term._dec_mode_cache[2048] = True
    term.ungetch('\x1b[48;50;100;1200;1200t')
    key = asyncio.run(term.async_inkey(timeout=0)) if asynchronous else term.inkey(timeout=0)
    assert key.name == 'RESIZE_EVENT'
    assert (term.width, term.height, term.pixel_width, term.pixel_height) == (100, 50, 1200, 1200)
    assert term._xtwinops_cell_cache is None
    assert term._xtwinops_cache is None
    with mock.patch.object(term, '_get_xtwinops_cell_size', return_value=(24, 12)):
        assert term.get_cell_size() == window.Size(12, 24)


def test_missing_geometry_is_not_invented(term):
    """A missing reply is unknown, rather than guessed from an 80x25 fallback."""
    with mock.patch.object(term, '_query_with_boundary', return_value=None):
        assert term.get_cell_size() is None
        assert term.cell_width == term.cell_height == -1
        assert term.window.size() is None
        assert term.window.screen_size() is None
        assert term.window.position() is None
        assert term.window.state() is None


def test_size_preference_does_not_fabricate_dimensions(term):
    """Request framing preserves omitted axes and never treats a preference as an observation."""
    with mock.patch.object(term, '_height_and_width', return_value=WINSZ(24, 80, 800, 480)):
        term.request_size(columns=120, rows=40)
        assert (term.width, term.height) == (80, 24)
        term.request_size(width=1500)
        term.request_size(rows=0)
    assert term.stream.getvalue() == '\x1b[8;40;120t\x1b[4;;1500t\x1b[8;0;t'


@pytest.mark.parametrize('fields', [{}, {'columns': 80, 'height': 500},
                                    {'columns': -1}, {'rows': True}])
def test_invalid_preferences_do_not_emit_commands(term, fields):
    """Units, missing requests and invalid numbers are checked before writing."""
    with pytest.raises(ValueError):
        term.request_size(**fields)
    assert not term.stream.getvalue()


def test_title_stack_and_redirection(term):
    """A failure restores titles, and redirected plain output has no control bytes."""
    with pytest.raises(ZeroDivisionError):
        with term.window.title_saved():
            _ = 1 / 0
    assert term.stream.getvalue() == '\x1b[22;0t\x1b[23;0t'
    plain = TestTerminal(stream=io.StringIO(), force_styling=False)
    plain.request_size(columns=100)
    plain.shell.working_directory('/tmp')
    assert not plain.stream.getvalue()


def test_device_channels_are_first_class(term):
    """Direct APIs reuse the same query transport and mode state as discovery."""
    for name in ('tablet', 'scroll', 'gesture', 'haptic', 'ink', 'font', 'display',
                 'latency', 'frame'):
        assert getattr(term, name) is getattr(term.silkie, name)


def test_standard_controls_and_shell_markers():
    """Known escape sequences and URI escaping remain valid without a size cap."""
    assert control.csi('h', 2004, private='?') == '\x1b[?2004h'
    assert control.csi('m', '4:3') == '\x1b[4:3m'
    assert control.csi('r', 1, None, intermediate='$') == '\x1b[1;$r'
    assert control.dcs('+q544e') == '\x1bP+q544e\x1b\\'
    assert control.apc('Gq=2') == '\x1b_Gq=2\x1b\\'
    title = '界' * 70000
    assert control.osc(0, title) == '\x1b]0;' + title + '\x1b\\'
    assert shell.working_directory('/a b/界', host='example.com') == (
        '\x1b]7;file://example.com/a%20b/%E7%95%8C\x1b\\')
    assert shell.mark('A', k='s') == '\x1b]133;A;k=s\x1b\\'
    assert shell.mark('D', status=3) == '\x1b]133;D;3\x1b\\'


@pytest.mark.parametrize('function,args', [
    (control.csi, ('m\x1b',)), (control.csi, ('m', '0;2')), (control.osc, (0, 'a\x07b')),
    (control.dcs, ('x\x1b\\',)), (shell.working_directory, ('relative',)),
    (shell.mark, ('Z',)),
])
def test_control_injection_is_rejected(function, args):
    """Structured APIs reject embedded sequences before reaching the stream."""
    with pytest.raises(ValueError):
        function(*args)


def test_public_query_returns_complete_reply(term):
    """The public query wrapper returns the matched wire message, not a partial payload."""
    reply = '\x1b]21;fg=rgb:ffff/ffff/ffff\x1b\\'
    with mock.patch.object(term, '_query_with_boundary', return_value=re.match('.*', reply)):
        assert term.query(control.osc(21, 'fg=?'), r'\x1b\]21;.*?\x1b\\') == reply


def test_hamingjur_import_never_loads_blessed():
    """Import every portable implementation module while refusing any Blessed import."""
    script = '''
import sys, importlib.abc, importlib, pkgutil
class RefuseBlessed(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'blessed' or fullname.startswith('blessed.'):
            raise AssertionError('Hamingjur tried to import upstream Blessed')
sys.meta_path.insert(0, RefuseBlessed())
import hamingjur
for module in pkgutil.iter_modules(hamingjur.__path__):
    if module.name != 'win_terminal':
        importlib.import_module('hamingjur.' + module.name)
assert hamingjur.Terminal.__module__ == 'hamingjur.terminal'
assert list(hamingjur.graphics.placeholder(1, 1, 1))
'''
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)
