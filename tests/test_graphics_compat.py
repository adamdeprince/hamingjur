"""Exercise transparent graphics fallback through the bound terminal APIs."""
import io
import re

import pytest
from PIL import Image

from hamingjur import graphics
from hamingjur.imaging import PreparedImage, Transfer, decode_image
from hamingjur.protocols import ST, ProtocolEvent, parse_event
from hamingjur.window import Size
from .accessories import TestTerminal
from .test_protocols import graphics_payload


@pytest.fixture(name='terminal')
def terminal_fixture(monkeypatch):
    """Bind an output stream to independently controllable protocol probes."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    term = TestTerminal(stream=io.StringIO(), force_styling=True, is_a_tty=True)
    term._keyboard_fd = None
    monkeypatch.setattr(term, '_is_apple_terminal', lambda *_: False)
    monkeypatch.setattr(term, 'does_kitty_graphics', lambda *_: False)
    monkeypatch.setattr(term, 'does_sixel', lambda *_, **__: True)
    monkeypatch.setattr(term, 'get_sixel_colors', lambda *_: 256)
    monkeypatch.setattr(term, 'get_cell_size', lambda *_: Size(4, 6))
    monkeypatch.setattr(term, 'get_location', lambda *_: (3, 4))
    monkeypatch.setattr(term.silkie, 'capabilities', lambda *_, **__: None)
    monkeypatch.setattr(term.graphics, 'query', lambda *_, **__: None)
    return term


def test_streamed_upload_and_sixel_placement(terminal):
    """A stored image spans many packets and remains reusable at the current cursor."""
    frames = graphics.image(bytes((255, 0, 0)) * 23000, image_id=9, format=24,
                            width=23000, height=1, display=False)
    for packet in frames:
        terminal.graphics.send(packet)
    assert terminal.stream.getvalue() == ''

    terminal.send('prefix' + graphics.place(9, x=3000, w=4, c=2, X=1, C=1) + 'suffix')
    output = terminal.stream.getvalue()
    assert output == ('prefix\x1bP0;1;0q"1;1;9;2'
                      '#0;2;100;0;0#0?!8B' + ST + terminal.move(3, 4) + 'suffix')
    with pytest.raises(NotImplementedError, match='displayed pixels'):
        terminal.graphics.send(graphics.delete(9))
    terminal.graphics.forget(9)
    with pytest.raises(ValueError, match='No retained image'):
        terminal.graphics.send(graphics.place(9))


def test_sixel_draw_without_acknowledgement(terminal):
    """The PIL path uses the same retained raster, without inventing a protocol reply."""
    image = Image.new('RGBA', (2, 2), (200, 100, 30, 255))
    image.putpixel((0, 0), (255, 0, 255, 0))
    terminal.graphics.send(graphics.image(b'\x00\x00\x00', image_id=1, format=24,
                                          width=1, height=1, display=False))
    assert terminal.graphics.supports(32)
    assert terminal.graphics.transfers() == {Transfer.DIRECT}
    drawn = terminal.graphics.draw(image, x=2, y=3, width=2)
    assert drawn.image_id == 2 and drawn.transfer == Transfer.DIRECT and not drawn.acknowledged
    assert terminal.stream.getvalue() == (
        terminal.save + terminal.move(3, 2) +
        '\x1bP0;1;0q"1;1;2;2#0;2;78;39;12#0AB' + ST + terminal.restore)
    with pytest.raises(NotImplementedError, match='fields'):
        terminal.graphics.send(graphics.place(drawn.image_id, z=-1))
    with pytest.raises(NotImplementedError, match='transport'):
        terminal.graphics.send(graphics.image(b'/resource', image_id=2, t='f'))
    assert terminal.inkey(timeout=0).protocol_event is None


def test_packed_palette_fallback_preserves_rgba():
    """Image-local RGBA palettes are decoded with independent row padding."""
    data = b'\xff\x00\x00\x00\x00\x00\xff\x80' + b'\xa0\x40'
    raster = decode_image(data, 401, 3, 2)
    assert raster.tobytes() == b'\x00\x00\xff\x80\xff\x00\x00\x00' * 3


@pytest.mark.parametrize('backend', ['silkie', 'kitty', 'sixel'])
def test_vector_fallback_uses_best_backend(terminal, monkeypatch, backend):
    """Use native vectors when present, Kitty PNG otherwise, then sixel."""
    monkeypatch.setattr(terminal, 'does_kitty_graphics', lambda *_: backend != 'sixel')
    if backend == 'silkie':
        monkeypatch.setattr(terminal.graphics, 'query',
                            lambda *_, **__: ProtocolEvent('graphics', {}, 'OK'))

    records = ['g id=3 box=0,0,4,8', 'p id=4 g=3 d=M0,0H8V8H0Z fill=#ff0000']
    frames = list(graphics.scene(records, image_id=2, width=8, height=8, compress=True))
    assert terminal.graphics.supports(200)
    terminal.graphics.send(frames)
    output = terminal.stream.getvalue()
    if backend == 'silkie':
        assert output == ''.join(frames)
    elif backend == 'kitty':
        assert parse_event(output).fields['f'] == '100'
        raster = Image.open(io.BytesIO(parse_event(output).data)).convert('RGBA')
        assert raster.getpixel((2, 2)) == (255, 0, 0, 255)
        assert raster.getpixel((6, 2))[3] == 0

        terminal.graphics.send(graphics.patch(2, ['p id=4 g=3 d=M0,0H8V8H0Z fill=#0000ff']))
        update = parse_event(terminal.stream.getvalue()[len(output):])
        assert update.fields['a'] == 'f' and update.fields['r'] == '1'
        raster = Image.open(io.BytesIO(update.data)).convert('RGBA')
        assert raster.getpixel((2, 2)) == (0, 0, 255, 255)
    else:
        assert output.startswith('\x1bP0;1;0q"1;1;8;8#0;2;100;0;0')
        with pytest.raises(NotImplementedError, match='displayed scene'):
            terminal.graphics.send(graphics.patch(2, ['x all']))


def test_vector_patch_failure_preserves_unplaced_scene(terminal):
    """A rejected edit leaves the stored scene available for another edit and placement."""
    terminal.graphics.send(graphics.scene(
        ['p id=1 d=M0,0H2V2H0Z fill=#ff0000'], image_id=3, width=2, height=2, display=False))
    with pytest.raises(NotImplementedError, match='requires native Silkie'):
        terminal.graphics.send(graphics.patch(3, ['x all', 'k id=2 pts=0,0,2']))
    terminal.graphics.send(graphics.patch(3, ['p id=2 d=M0,0H1V1H0Z fill=#0000ff']))
    terminal.graphics.send(graphics.place(3))
    assert '#0;2;0;0;100#1;2;100;0;0' in terminal.stream.getvalue()


def test_kitty_raster_packets_pass_through(terminal, monkeypatch):
    """Native Kitty receives the original chunks and untouched passthrough strings."""
    monkeypatch.setattr(terminal, 'does_kitty_graphics', lambda *_: True)
    original = list(graphics.image(b'\xff' * 12000, image_id=5, format=24,
                                   width=4000, height=1, z=-1, p=1))
    wrapped = '\x1bPtmux;' + original[0].replace('\x1b', '\x1b\x1b') + ST
    terminal.send(wrapped + ''.join(original))
    assert terminal.stream.getvalue() == wrapped + ''.join(original)
    wire = re.findall(r'\x1b_G[^\x1b]*\x1b\\', terminal.stream.getvalue()[len(wrapped):])
    assert graphics_payload(wire) == b'\xff' * 12000


def test_kitty_draw_converts_a_prepared_silkie_raster(terminal, monkeypatch):
    """A plan carried over from a Silkie display can still be drawn on Kitty."""
    monkeypatch.setattr(terminal.graphics, 'query', lambda *_, **fields:
                        ProtocolEvent('graphics', {}, 'OK') if 't' not in fields else None)
    seen = []

    def acknowledge(sequence, image_id, _timeout):
        seen.append(parse_event(sequence))
        return ProtocolEvent('graphics', {'i': str(image_id)}, 'OK')

    monkeypatch.setattr(terminal.graphics, '_request', acknowledge)
    source = PreparedImage(b'\x00\xf8', 516, Size(1, 1), False)
    result = terminal.graphics.draw(source)
    actual = decode_image(seen[0].data, result.image.format, 1, 1)
    assert result.acknowledged and actual.getpixel((0, 0)) == (255, 0, 0, 255)


def test_abandoned_fallback_upload_can_be_retried(terminal):
    """A fresh command can replace an unfinished locally translated upload."""
    interrupted = graphics.image(b'\xff' * 12000, image_id=5, format=24, width=4000, height=1)
    terminal.send(next(interrupted))
    terminal.graphics.send(graphics.image(b'\xff\x00\x00', image_id=5,
                                          format=24, width=1, height=1))
    assert terminal.stream.getvalue() == '\x1bP0;1;0q"1;1;1;1#0;2;100;0;0#0@' + ST


def test_sixel_honors_more_than_256_color_registers(terminal, monkeypatch):
    """The palette grows to match the terminal's reported register count."""
    monkeypatch.setattr(terminal, 'get_sixel_colors', lambda *_: 512)
    data = bytes(channel for red in range(0, 255, 17) for green in range(0, 255, 11)
                 for channel in (red, green, 0))
    terminal.graphics.send(graphics.image(data, image_id=3, format=24,
                                          width=len(data) // 3, height=1))
    assert len(re.findall(r'#\d+;2;', terminal.stream.getvalue())) == len(data) // 3
