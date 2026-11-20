"""Silkie SVG framing, fallback pixels and static document resource rules."""
import io

import pytest
from PIL import Image

from hamingjur import graphics
from hamingjur._vector_raster import rasterize_svg
from hamingjur.protocols import ProtocolEvent, parse_event
from .test_graphics_compat import terminal_fixture  # noqa: F401
from .test_protocols import graphics_payload


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 2">'
       '<rect width="2" height="2" fill="#22cc55"/></svg>')


def test_svg_wire_accepts_intrinsic_dimensions_and_large_stream():
    """An SVG document streams without nominal dimensions or a total size limit."""
    document = SVG.replace('<rect', '<!--' + 'text' * 25000 + '--><rect')
    frames = list(graphics.svg(document, image_id=4))
    assert len(frames) > 20
    event = parse_event(frames[0])
    assert event.fields['f'] == '201' and 's' not in event.fields and 'v' not in event.fields
    assert graphics_payload(frames) == document.encode('utf-8')
    with pytest.raises(ValueError, match='both width and height'):
        list(graphics.svg(SVG, image_id=4, width=2))


@pytest.mark.parametrize('backend', ['silkie', 'kitty', 'sixel'])
def test_svg_prefers_native_and_falls_back(terminal, monkeypatch, backend):
    """SVG passes through on Silkie, rasterizes on Kitty, and becomes sixels when needed."""
    monkeypatch.setattr(terminal, 'does_kitty_graphics', lambda *_: backend != 'sixel')
    if backend == 'silkie':
        def answer(payload, **fields):
            assert fields['f'] == 201 and 's' not in fields
            assert payload.startswith(b'<svg')
            return ProtocolEvent('graphics', {}, 'OK')

        monkeypatch.setattr(terminal.graphics, 'query', answer)
    frames = list(graphics.svg(SVG, image_id=2, compress=True))
    assert terminal.graphics.supports(201)
    terminal.graphics.send(frames)
    output = terminal.stream.getvalue()
    if backend == 'silkie':
        assert output == ''.join(frames)
    elif backend == 'kitty':
        event = parse_event(output)
        assert event.fields['f'] == '100'
        raster = Image.open(io.BytesIO(event.data)).convert('RGBA')
        assert raster.size == (4, 2)
        assert raster.getpixel((0, 0)) == (34, 204, 85, 255)
        assert raster.getpixel((3, 0))[3] == 0
    else:
        assert output.startswith('\x1bP0;1;0q"1;1;4;2#0;2;13;80;33')


def test_svg_fallback_blocks_external_images_and_entities(tmp_path):
    """Local rendering follows Silkie's static resource policy."""
    path = tmp_path / 'image.png'
    Image.new('RGB', (1, 1), 'red').save(path)
    source = (f'<svg width="3" height="2"><image href="{path}" width="3" height="2"/></svg>')
    decoded = rasterize_svg(source.encode(), 6, 4)
    image = Image.open(io.BytesIO(decoded)).convert('RGBA')
    assert image.size == (6, 4) and image.getbbox() is None
    with pytest.raises(ValueError, match='document type'):
        rasterize_svg(b'<!DOCTYPE svg [<!ENTITY a "bad">]><svg>&a;</svg>')
