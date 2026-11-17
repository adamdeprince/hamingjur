"""Exercise the drawing demo without requiring a terminal or tablet hardware."""
# std imports
import io
import itertools
import contextlib
import importlib.util
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

# 3rd party
import pytest

# local
from hamingjur import kitty, silkie
from hamingjur.protocols import parse_event
# local
from .accessories import TestTerminal
from .test_protocols import graphics_payload


@pytest.fixture(name='demo')
def demo_fixture():
    """Load the standalone example without invoking its CLI."""
    spec = importlib.util.spec_from_file_location(
        'tablet_canvas', Path(__file__).parents[1] / 'bin' / 'tablet_canvas.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name='terminal')
def terminal_fixture(monkeypatch):
    """Record complete transmissions as well as the real terminal byte stream."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    term = TestTerminal(stream=io.StringIO(), force_styling=True)
    batches = []
    send = term.graphics.send

    def capture(frames):
        frames = [frames] if isinstance(frames, str) else list(frames)
        batches.append(frames)
        send(frames)

    term.graphics.send = capture
    term.batches = batches
    return term


def event(phase, x=4, y=4, **fields):
    """Encode a real tablet report; None omits screen coordinates."""
    data = {'kind': 1, 'device': 1, 'contact': 0, 'phase': phase, **fields}
    if x is not None:
        data.update(x=round(x * 256), y=round(y * 256))
    return parse_event(silkie.command('tablet-pointer', 'event', **data))


def payloads(terminal):
    """Extract the complete application-generated vector records."""
    return [graphics_payload(batch).decode() for batch in terminal.batches
            if batch[0].startswith('\x1b_Ga=e,')]


@pytest.mark.parametrize('args', [[], ['--mode', 'unknown'], ['--help']])
def test_cli_parses_before_opening_terminal(demo, args):
    """Missing mode, invalid mode, and help must never probe a terminal."""
    with mock.patch.object(demo, 'Terminal') as constructor:
        with pytest.raises(SystemExit) as caught:
            demo.main(args)
        assert caught.value.code == (0 if args == ['--help'] else 2)
        constructor.assert_not_called()


def test_vector_lifecycle_and_pressure(demo, terminal):
    """Up supplies a final sample; cancel without a position deletes the stroke."""
    renderer = demo.Vectors(terminal, 64, 48)
    canvas = demo.Canvas(renderer)
    canvas.handle(event(0))
    canvas.handle(event(1))
    canvas.handle(event(2, x=10, valid=1, p=65535))
    canvas.handle(event(3, x=11, valid=1, p=0))
    canvas.handle(event(1, contact=2))
    canvas.handle(event(4, x=None, contact=2))
    assert payloads(terminal) == [
        'k id=1 pts=4.0,4.0,8.0 fill=#44aaff\n',
        'a id=1 pts=10.0,4.0,8.0\n',
        'a id=1 pts=11.0,4.0,1.0\n',
        'k id=2 pts=4.0,4.0,8.0 fill=#44aaff\n',
        'x id=2\n',
    ]
    assert not canvas.contacts


def test_ink_down_owns_whole_stroke_and_fingers_fall_back(demo, terminal):
    """Only the down event needs an ink ID; later samples must not be redrawn."""
    canvas = demo.Canvas(demo.Vectors(terminal, 64, 48), local_ink=True)
    canvas.handle(event(1, ink=1))
    canvas.handle(event(2, x=20))
    canvas.handle(event(1, kind=2))
    canvas.handle(event(3, x=25))
    canvas.handle(event(3, kind=2))
    canvas.handle(event(1))  # Pen refused by the terminal also uses Python.
    canvas.handle(event(4, x=None))
    assert [line.split()[0] for line in payloads(terminal)] == ['k', 'a', 'k', 'x']
    assert not canvas.contacts


def test_touchpad_coordinates_are_not_pixels(demo, terminal):
    """Normalized touchpad events must not create strokes on the screen."""
    canvas = demo.Canvas(demo.Vectors(terminal, 64, 48))
    canvas.handle(event(1, kind=2, src=1))
    assert not terminal.batches


def test_vector_id_exhaustion_is_explicit(demo, terminal):
    """The only ID ceiling is the wire format, and it never wraps."""
    renderer = demo.Vectors(terminal, 8, 8)
    renderer.ids = itertools.count(demo.MAX_ID)
    assert renderer.start((1, 1, 1)) == demo.MAX_ID
    with pytest.raises(OverflowError, match='ID space'):
        renderer.start((2, 2, 2))


def assembled_alpha(renderer):
    """Stitch cell pixels together, independently of the renderer's placement code."""
    width, height = renderer.size
    result = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            column, row = x // renderer.cell[0], y // renderer.cell[1]
            pixels = renderer.pixels((column, row))
            offset = ((y % renderer.cell[1]) * renderer.cell[0] + x % renderer.cell[0]) * 4
            result[y * width + x] = pixels[offset + 3]
    return result


def test_raster_cells_have_no_seams_and_correct_placements(demo, terminal):
    """A horizontal stroke has identical coverage on both sides of cell seams."""
    renderer = demo.Tiles(terminal, 24, 16, 8, 8)
    stroke = renderer.start((2, 8, 4))
    renderer.append(stroke, (22, 8, 4))
    renderer.finish(stroke)
    alpha = assembled_alpha(renderer)
    for x in range(4, 21):
        assert alpha[7 * 24 + x] == 255
        assert alpha[8 * 24 + x] == 255
        assert alpha[4 * 24 + x] == 0
        assert alpha[11 * 24 + x] == 0
    for batch in terminal.batches:
        frame = parse_event(batch[1])
        assert frame.integer('s') == frame.integer('v') == 8
        assert frame.integer('C') == 1
        assert len(graphics_payload(batch[1:])) == 8 * 8 * 4
        index = frame.integer('i') - demo.INK_IMAGE - 1
        assert batch[0] == terminal.move_yx(index // 3, index % 3)


def test_cancel_preserves_other_contacts_and_completed_strokes(demo, terminal):
    """Cancellation erases only its contact, even after another contact commits."""
    renderer = demo.Tiles(terminal, 16, 16, 8, 8)
    original = renderer.start((4, 4, 4))
    renderer.finish(original)
    before = assembled_alpha(renderer)
    cancelled = renderer.start((8, 8, 8))
    other = renderer.start((4, 4, 4))
    renderer.finish(other)
    assert assembled_alpha(renderer) != before
    renderer.finish(cancelled, cancel=True)
    assert assembled_alpha(renderer) == before
    assert not renderer.active


def test_cell_larger_than_old_64k_boundary(demo, terminal):
    """Cell-sized image allocation and packetization preserve the complete image."""
    renderer = demo.Tiles(terminal, 129, 129, 129, 129)
    stroke = renderer.start((128, 128, 4))
    pixels = graphics_payload(terminal.batches[-1][1:])
    assert len(pixels) == 129 * 129 * 4 > 65536
    assert pixels[-4:] == bytes((68, 170, 255, 255))
    renderer.finish(stroke)


def test_repeated_raster_strokes_release_contact_storage(demo, terminal):
    """Repeated drawing retains only the canvas; no fixed stroke pool fills up."""
    renderer = demo.Tiles(terminal, 8, 8, 8, 8)
    for _ in range(1000):
        stroke = renderer.start((4, 4, 2))
        renderer.finish(stroke)
    assert not renderer.active
    assert len(renderer.committed) == len(renderer.images) == 1
    assert renderer.pixels((0, 0))[(4 * 8 + 4) * 4 + 3] > 0


def test_captured_outside_window_segments_are_clipped(demo, terminal):
    """Work depends on visible pixels, even for captured points far off screen."""
    renderer = demo.Tiles(terminal, 8, 8, 8, 8)
    stroke = renderer.start((-1000000, 4, 2))
    assert not terminal.batches
    renderer.append(stroke, (1000000, 4, 2))
    assert len(renderer.images) == 1
    assert all(assembled_alpha(renderer)[3 * 8 + x] for x in range(8))


def test_pen_pointer_pops_before_image_is_deleted(demo, terminal):
    """
    The pen has a visible nib and its pointer stack is restored on failure.

    :raises RuntimeError: To exercise the error cleanup path.
    """
    with pytest.raises(RuntimeError, match='test failure'):
        with demo.pen_pointer(terminal):
            assert any(demo.pen_pixels()[3::4])
            raise RuntimeError('test failure')
    output = terminal.stream.getvalue()
    push = kitty.pointer('image:1:3:28', action='>')
    pop = kitty.pointer(action='<')
    delete = '\x1b_Ga=d,d=I,q=2,i=1'
    assert push in output
    assert output.index(push) < output.index(pop) < output.index(delete)
    first = parse_event(terminal.batches[0][0])
    assert first.fields['a'] == 't'  # Pointer image has no canvas placement.


@pytest.mark.parametrize('granted', [0, 1, None])
def test_ink_lifecycle_and_disjoint_ids(demo, terminal, granted):
    """Full protocol ID range is reserved, and failures always disable ink."""
    ink = terminal.silkie.ink
    ink.query = mock.Mock(return_value=parse_event(
        silkie.command('ink', 'cap', caps=1, mode=0)))
    ink.read_mode = mock.Mock(return_value=granted)
    ink.send = mock.Mock()
    caps = SimpleNamespace(supports=lambda name: name == 'ink')
    renderer = demo.Vectors(terminal, 64, 48)

    def run():
        with demo.drawing(terminal, renderer, 'ink', caps) as canvas:
            canvas.handle(event(1, ink=1))
            canvas.handle(event(1, kind=2))
            assert len(payloads(terminal)) == 1

    if granted == 1:
        run()
    else:
        with pytest.raises(SystemExit, match='did not grant'):
            run()
    ink.send.assert_any_call('brush', i=demo.INK_IMAGE, k=1, n=demo.MAX_ID,
                             fill=demo.COLOR, w0=1, w1=8)
    assert ink.send.call_args == mock.call('mode', mode=0)
    deleted = [parse_event(batch[0]).integer('i') for batch in terminal.batches
               if batch[0].startswith('\x1b_Ga=d,')]
    assert deleted == [demo.INK_IMAGE, demo.VECTOR_IMAGE]


@pytest.mark.parametrize('mode', ['tiles', 'vectors', 'ink'])
def test_main_runs_all_modes_and_cleans_up(demo, terminal, mode):
    """Exercise the complete CLI session with decoded tablet events and real output."""
    caps = SimpleNamespace(formats={32, 200}, supports=lambda name: True)
    terminal.silkie.capabilities = mock.Mock(return_value=caps)
    terminal.silkie.tablet.query = mock.Mock(return_value=parse_event(silkie.command(
        'tablet-pointer', 'cap', w=24, h=16, cw=8, ch=8)))
    terminal.silkie.tablet.enabled = mock.Mock(side_effect=lambda _: contextlib.nullcontext(3))
    terminal.silkie.ink.query = mock.Mock(return_value=parse_event(
        silkie.command('ink', 'cap', caps=1, mode=0)))
    terminal.silkie.ink.read_mode = mock.Mock(return_value=1)
    terminal.cbreak = contextlib.nullcontext
    events = [event(1, ink=1) if mode == 'ink' else event(1), event(3)]
    keys = [SimpleNamespace(name=None, tablet=sample) for sample in events] + ['q']
    terminal.inkey = mock.Mock(side_effect=keys)
    with mock.patch.object(demo, 'Terminal', return_value=terminal):
        demo.main(['--mode', mode])
    assert terminal.inkey.call_count == 3
    output = terminal.stream.getvalue()
    assert kitty.pointer('image:1:3:28', action='>') in output
    assert kitty.pointer(action='<') in output
    assert silkie.command('ink', 'mode', mode=0) in output
    if mode == 'tiles':
        assert not payloads(terminal)
        assert any(parse_event(batch[1]).fields.get('f') == '32'
                   for batch in terminal.batches if len(batch) > 1)
    elif mode == 'vectors':
        assert len(payloads(terminal)) == 2
    else:
        assert not payloads(terminal)
        assert silkie.command('ink', 'brush', i=3, k=1, n=demo.MAX_ID,
                              fill=demo.COLOR, w0=1, w1=8) in output
