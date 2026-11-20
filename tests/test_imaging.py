"""Image planning, lossless packing, negotiated transport and resource lifetimes."""
# std imports
import io
import os
import zlib
import random
import struct
from unittest import mock
from multiprocessing.shared_memory import SharedMemory

# 3rd party
import pytest
from PIL import Image

# local
from hamingjur import silkie, graphics
from hamingjur.imaging import Transfer, ImageSupport, prepare_image, image_resource
from hamingjur.protocols import FRAME_RE, ProtocolEvent, parse_event
from .accessories import TestTerminal


@pytest.fixture(name='term')
def terminal_fixture(monkeypatch):
    """Use an isolated terminal with a negotiated direct transport."""
    monkeypatch.delenv('NO_COLOR', raising=False)
    terminal = TestTerminal(stream=io.StringIO(), force_styling=True)
    terminal.graphics._support = ImageSupport(frozenset((301, 302, 304, 308, 516, 24, 32, 100)),
                                              frozenset((Transfer.DIRECT,)))
    monkeypatch.setattr(terminal.silkie, 'capabilities', lambda *args, **kwargs: None)
    return terminal


def decoded(plan):
    """Expand a prepared wire payload into RGBA for pixel-exact comparisons."""
    data = zlib.decompress(plan.data) if plan.compressed else plan.data
    width, height = plan.size
    if plan.format == 100:
        return Image.open(io.BytesIO(data)).convert('RGBA')
    if plan.format in (24, 32):
        mode = 'RGB' if plan.format == 24 else 'RGBA'
        return Image.frombytes(mode, plan.size, data).convert('RGBA')
    pixels = []
    if plan.format == 516:
        for offset in range(0, len(data), 2):
            word = int.from_bytes(data[offset:offset + 2], 'little')
            pixels.append((((word >> 11) * 255 + 15) // 31,
                           (((word >> 5) & 63) * 255 + 31) // 63,
                           ((word & 31) * 255 + 15) // 31, 255))
    else:
        bits = plan.format - 300
        stride = (width * bits + 7) // 8
        for y in range(height):
            for x in range(width):
                sample = data[y * stride + x * bits // 8] >> (8 - bits - x * bits % 8)
                value = (sample & ((1 << bits) - 1)) * 255 // ((1 << bits) - 1)
                pixels.append((value, value, value, 255))
    result = Image.new('RGBA', plan.size)
    result.putdata(pixels)
    return result


@pytest.mark.parametrize('bits', [1, 2, 4, 8])
def test_exact_gray_packing_and_row_padding(bits):
    """Every supported grayscale precision round trips with non-byte-aligned rows."""
    image = Image.new('L', (19, 7))
    levels = (1 << bits) - 1
    image.putdata([(index % (levels + 1)) * 255 // levels for index in range(19 * 7)])
    plan = prepare_image(image, (300 + bits,))
    assert plan.format == 300 + bits and decoded(plan).tobytes() == image.convert('RGBA').tobytes()


def test_rgb565_is_lossless_after_display_matching():
    """Unequal framebuffer channel widths select 565 only after quantizing to that display."""
    source = Image.new('RGB', (20, 10), (118, 149, 203))
    display = dict(color='rgb', bits='0', red_bits='5', green_bits='6', blue_bits='5')
    plan = prepare_image(source, (516,), display=display)
    expected = tuple(((value * levels + 127) // 255 * 255 + levels // 2) // levels
                     for value, levels in zip((118, 149, 203), (31, 63, 31)))
    assert decoded(plan).getpixel((0, 0)) == expected + (255,)
    with pytest.raises(ValueError, match='further loss'):
        prepare_image(source, (516,), display=display, match_display=False)
    assert source.getpixel((0, 0)) == (118, 149, 203)


@pytest.mark.parametrize('pixel_format', [1, 2, 4, 8, 16])
def test_old_packed_numbers_are_not_planner_encodings(pixel_format):
    """Terminal palette indices cannot stand in for grayscale or RGB565 samples."""
    with pytest.raises(ValueError, match='further loss'):
        prepare_image(Image.new('RGB', (9, 2), 'white'), (pixel_format,))


def test_fit_preserves_original_and_aspect_ratio():
    """A target is a bounding box, without upscaling or mutating the input."""
    source = Image.new('RGB', (600, 300), 'red')
    assert prepare_image(source, (24,), width=100, height=100).size == (100, 50)
    assert prepare_image(source, (24,), width=1200, height=1200).size == source.size
    assert source.size == (600, 300)
    with pytest.raises(ValueError):
        prepare_image(source, (24,), width=0)


@pytest.mark.parametrize('mode', ['RGB', 'RGBA', 'P', 'L', 'LA', '1'])
def test_lossless_candidates_preserve_pixels_and_alpha(mode):
    """Palette PNG and raw candidates preserve exact pixels, including partial alpha."""
    source = Image.new('RGBA', (19, 11))
    source.putdata([(index % 5 * 50, index % 3 * 70, index % 7 * 30, index % 4 * 85)
                    for index in range(19 * 11)])
    source = source.convert(mode)
    plan = prepare_image(source, (301, 302, 304, 308, 516, 24, 32, 100))
    assert decoded(plan).tobytes() == source.convert('RGBA').tobytes()


def test_cheap_compression_and_packing_are_measured():
    """Tiny noise stays uncompressed; repeated data uses zlib with no user compression switch."""
    tiny = Image.new('RGB', (1, 1), (17, 83, 201))
    plan = prepare_image(tiny, (24, 100))
    assert plan.format == 24 and not plan.compressed and plan.data == b'\x11\x53\xc9'
    repeated = prepare_image(Image.new('RGB', (1024, 512), 'blue'), (24,))
    assert repeated.compressed and len(repeated.data) < 1024 * 512 * 3 // 100
    assert decoded(repeated).getpixel((999, 511)) == (0, 0, 255, 255)
    randomizer = random.Random(42)
    source = Image.frombytes('RGB', (100, 100), randomizer.randbytes(30000))
    packed = prepare_image(source, (24, 100))
    assert not packed.compressed and decoded(packed).tobytes() == source.convert('RGBA').tobytes()


def test_grayscale_display_and_precision_override():
    """A known gray display reduces color once; packing adds no further loss."""
    source = Image.new('RGBA', (32, 32), (110, 80, 190, 127))
    plan = prepare_image(source, (32, 100), display=dict(color='gray', gray_bits='4'))
    r, g, b, a = decoded(plan).getpixel((0, 0))
    assert r == g == b and r % 17 == 0 and a == 127
    lossless = prepare_image(source, (32,), display=dict(color='mono'), match_display=False)
    assert decoded(lossless).getpixel((0, 0)) == (110, 80, 190, 127)
    opaque = Image.new('RGB', (9, 2), (80, 120, 200))
    mono = prepare_image(opaque, (301,), display=dict(color='mono'))
    assert set(decoded(mono).tobytes()) <= {0, 255}


@pytest.mark.parametrize(('mode', 'byte_order'), [('I;16', '<'), ('I;16B', '>'), ('I;16N', '=')])
def test_draw_preserves_gray16_samples(term, mode, byte_order):
    """Drawing uploads the exact source intensities in Silkie's little-endian format."""
    values = (0, 256, 32768, 65535)
    source = Image.frombytes(mode, (4, 1), struct.pack(byte_order + '4H', *values))
    term.graphics._support = ImageSupport(
        frozenset({24, 100, 308, 316}), frozenset({Transfer.DIRECT}))
    term.graphics.observe(parse_event(silkie.command('display', 'event', color='mono')))

    def acknowledge(sequence, image_id, timeout):
        frame = parse_event(sequence)
        assert frame.fields['f'] == '316'
        data = zlib.decompress(frame.data) if frame.fields.get('o') == 'z' else frame.data
        assert data == struct.pack('<4H', *values)
        return ProtocolEvent('graphics', {'i': str(image_id)}, 'OK')

    with mock.patch.object(term.graphics, '_request', side_effect=acknowledge):
        result = term.graphics.draw(source, width=4, match_display=False)
    assert result.acknowledged
    assert source.tobytes() == struct.pack(byte_order + '4H', *values)


def test_gray16_resizes_before_matching_display():
    """Lanczos retains the sample range, and display matching uses its original scale."""
    values = (0, 256, 32768, 65535)
    source = Image.frombytes('I;16', (4, 1), struct.pack('<4H', *values))
    fitted = prepare_image(source, (316,), width=2)
    data = zlib.decompress(fitted.data) if fitted.compressed else fitted.data
    expected = source.convert('I').resize((2, 1), Image.Resampling.LANCZOS)
    assert fitted.size == (2, 1) and data == expected.convert('I;16').tobytes()
    assert max(struct.unpack('<2H', data)) > 255
    matched = prepare_image(source, (308,), display=dict(color='gray', gray_bits='8'))
    assert [decoded(matched).getpixel((x, 0))[0] for x in range(4)] == [0, 1, 128, 255]
    with pytest.raises(ValueError, match='further loss'):
        prepare_image(source, (24, 100, 308), match_display=False)
    exact = Image.frombytes('I;16', (2, 1), struct.pack('<2H', 257, 65535))
    assert prepare_image(exact, (308,), match_display=False).data == b'\x01\xff'


def test_gray16_png_preserves_transparent_sample():
    """A PNG transparency key matches the original 16-bit sample and retains gray depth."""
    source = Image.frombytes('I;16', (3, 1), struct.pack('<3H', 256, 32768, 65535))
    with io.BytesIO() as stream:
        source.save(stream, format='PNG', transparency=32768)
        stream.seek(0)
        with Image.open(stream) as reopened:
            plan = prepare_image(reopened, (316, 664, 100), match_display=False)
    data = zlib.decompress(plan.data) if plan.compressed else plan.data
    assert plan.format == 664
    assert list(struct.iter_unpack('<4H', data)) == [
        (256, 256, 256, 65535), (32768, 32768, 32768, 0), (65535, 65535, 65535, 65535)]


def test_target_geometry_and_live_display_changes(term):
    """New resize/display events update the next plan, without caching a stale fitted image."""
    source = Image.new('RGB', (1000, 1000), (80, 120, 200))
    term._dec_mode_cache[2048] = 1
    term.ungetch('\x1b[48;20;40;400;400t')
    assert term.inkey(timeout=0).name == 'RESIZE_EVENT'
    assert term.graphics.prepare(source, columns=10, rows=2).size == (40, 40)
    term.ungetch('\x1b[48;20;40;800;800t' + silkie.command(
        'display', 'event', color='mono', bits=1, gray_bits=1, precision='configured'))
    assert term.inkey(timeout=0).name == 'RESIZE_EVENT'
    assert term.inkey(timeout=0).protocol_event.fields['color'] == 'mono'
    plan = term.graphics.prepare(source, columns=10, rows=2)
    assert plan.size == (80, 80) and set(decoded(plan).tobytes()) <= {0, 255}
    with pytest.raises(ValueError, match='not both'):
        term.graphics.prepare(source, width=10, rows=2)


def test_negotiate_probes_real_local_access_and_caches(term):
    """Advertised media are narrowed by actual resource reads, including remote rejection."""
    caps = silkie.Capabilities(parse_event(silkie.command(
        'caps', 'cap', ext='caps.1', format='301,302,304,308,516,24,32,100', media='d,f,t,s,g')))
    with mock.patch.object(term.silkie, 'capabilities', return_value=caps):
        def answer(data, **fields):
            medium = fields.get('t', 'd')
            if medium in ('f', 't'):
                assert os.path.isfile(os.fsdecode(data))
                with open(data, 'rb') as source:
                    assert source.read() == b'\0'
            elif medium == 's':
                memory = SharedMemory(name=data.decode().lstrip('/'))
                try:
                    assert bytes(memory.buf[:fields['S']]) == b'\0'
                finally:
                    memory.close()
            return ProtocolEvent('graphics', {}, 'OK')

        with mock.patch.object(term.graphics, 'query', side_effect=answer) as query:
            support = term.graphics.negotiate(force=True)
            assert support.transfers == frozenset(Transfer)
            assert term.graphics.transfers() is support.transfers and query.call_count == 4

        def remote(data, **fields):
            return ProtocolEvent('graphics', {}, 'OK' if 't' not in fields else 'EBADF')

        with mock.patch.object(term.graphics, 'query', side_effect=remote):
            support = term.graphics.negotiate(force=True)
            assert support.transfers == {Transfer.DIRECT, Transfer.GPU_SURFACE}


@pytest.mark.parametrize('medium', [Transfer.FILE, Transfer.TEMPORARY_FILE, Transfer.SHARED_MEMORY])
def test_local_draw_lifetime_cleanup_and_failures(term, medium):
    """Local bytes remain readable until acknowledgement, including timeout and refusal cleanup."""
    term.graphics._support = ImageSupport(frozenset((24,)), frozenset((medium, Transfer.DIRECT)))
    names = []

    def acknowledge(sequence, image_id, timeout):
        frame = parse_event(sequence)
        name = frame.data
        names.append(name)
        if medium == Transfer.SHARED_MEMORY:
            memory = SharedMemory(name=name.decode().lstrip('/'))
            try:
                data = bytes(memory.buf[:int(frame.fields['S'])])
            finally:
                memory.close()
        else:
            with open(name, 'rb') as resource:
                data = resource.read()
        if frame.fields.get('o') == 'z':
            data = zlib.decompress(data)
        assert data == b'\xff\0\0' * 90 * 70
        return ProtocolEvent('graphics', {'i': str(image_id)}, 'OK')

    source = Image.new('RGB', (90, 70), 'red')
    with mock.patch.object(term.graphics, '_request', side_effect=acknowledge):
        for _ in range(10):
            result = term.graphics.draw(source, transfer=medium, width=90, height=70)
            assert result.transfer == medium
    for name in names:
        if medium == Transfer.SHARED_MEMORY:
            with pytest.raises(FileNotFoundError):
                SharedMemory(name=name.decode().lstrip('/'))
        else:
            assert not os.path.exists(name)
    failures = [(None, TimeoutError), (ProtocolEvent('graphics', {}, 'EINVAL'), RuntimeError)]
    for reply, error in failures:
        with mock.patch.object(term.graphics, '_request', side_effect=lambda *args:
                               acknowledge(*args) and reply):
            with pytest.raises(error):
                term.graphics.draw(source, transfer=medium, width=90, height=70)
    if medium != Transfer.SHARED_MEMORY:
        assert all(not os.path.exists(name) for name in names)


def test_direct_drawing_and_redirection(term):
    """Draw sends prepared bytes, respects explicit media and allocates increasing image IDs."""
    source = Image.new('RGB', (5, 3), (7, 23, 91))
    observed = []

    def acknowledge(sequence, image_id, timeout):
        events = [parse_event(match.group()) for match in FRAME_RE.finditer(sequence)]
        observed.append(events)
        return ProtocolEvent('graphics', {'i': str(image_id)}, 'OK')

    with mock.patch.object(term.graphics, '_request', side_effect=acknowledge):
        one = term.graphics.draw(source, width=5, height=3, transfer=Transfer.DIRECT)
        two = term.graphics.draw(source, width=5, height=3, transfer=Transfer.DIRECT)
    assert two.image_id == one.image_id + 1
    assert decoded(one.image).tobytes() == source.convert('RGBA').tobytes()
    assert observed[0][0].fields['f'] == str(one.image.format)
    with pytest.raises(ValueError, match='unavailable'):
        term.graphics.draw(source, width=5, height=3, transfer=Transfer.SHARED_MEMORY)
    term._does_styling = False
    assert term.graphics.draw(source) is None and term.stream.getvalue() == ''


def test_terminal_unlink_of_owned_resources():
    """A terminal may unlink the resource before acknowledgement without double-unlink errors."""
    with image_resource(b'pixels', Transfer.TEMPORARY_FILE) as name:
        os.unlink(name)
    if os.name == 'posix':
        import _posixshmem
        with image_resource(b'pixels', Transfer.SHARED_MEMORY) as name:
            _posixshmem.shm_unlink(name.decode())


def test_wire_size_counts_chunk_headers():
    """Candidate costs match real direct packets, including continuation and compression keys."""
    randomizer = random.Random(8)
    source = Image.frombytes('RGB', (400, 80), randomizer.randbytes(400 * 80 * 3))
    for image in (source, Image.new('RGB', source.size, 'red')):
        plan = prepare_image(image, (24,))
        fields = dict(a='T', i=1, f=plan.format, s=plan.size.width, v=plan.size.height, q=0)
        if plan.compressed:
            fields['o'] = 'z'
        packets = graphics.transmit(plan.data, **fields)
        assert plan.wire_size == sum(len(packet) for packet in packets)


def test_automatic_local_vs_direct_and_prepared_upload(term):
    """Automatic selection compares real resource references; prepared bytes can be reused."""
    term.graphics._support = ImageSupport(frozenset((24,)), frozenset(
        (Transfer.DIRECT, Transfer.TEMPORARY_FILE)))
    reply = ProtocolEvent('graphics', {}, 'OK')
    with mock.patch.object(term.graphics, '_request', return_value=reply):
        tiny = prepare_image(Image.new('RGB', (1, 1), 'red'), (24,))
        assert term.graphics.draw(tiny).transfer == Transfer.DIRECT
        randomizer = random.Random(51)
        source = Image.frombytes('RGB', (100, 100), randomizer.randbytes(30000))
        large = prepare_image(source, (24,))
        assert term.graphics.draw(large).transfer == Transfer.TEMPORARY_FILE
        assert term.graphics.draw(tiny, transfer=Transfer.TEMPORARY_FILE).transfer == (
            Transfer.TEMPORARY_FILE)
        with pytest.raises(ValueError, match='already have'):
            term.graphics.draw(tiny, width=1)
        # Choosing the largest legal explicit ID must not exhaust automatic allocation.
        term.graphics.draw(tiny, image_id=2**32 - 1)
        assert term.graphics.draw(tiny).image_id < 2**32 - 1


def test_adaptive_context_restores_reporting(term):
    """The image loop scopes both resize and display reporting without retaining image data."""
    term.graphics._has_display = True
    term._dec_mode_cache[2048] = 0
    with mock.patch.object(term.display, 'read_mode', side_effect=[0, 1]):
        with term.graphics.adaptive(timeout=0) as drawing:
            assert drawing is term.graphics
        assert term._resize_read_fd is None
    assert term.stream.getvalue().endswith(silkie.command('display', 'mode', mode=0))


def test_negotiate_png_only_and_real_allocation_failure(term):
    """PNG-only peers work, while genuine resource capacity failures are never hidden."""
    import errno

    caps = silkie.Capabilities(parse_event(silkie.command(
        'caps', 'cap', ext='caps.1', format='100', media='d,s')))
    with mock.patch.object(term.silkie, 'capabilities', return_value=caps):
        reply = ProtocolEvent('graphics', {}, 'OK')
        with mock.patch.object(term.graphics, 'query', return_value=reply):
            failure = OSError(errno.ENOMEM, 'oom')
            with mock.patch('hamingjur.graphics.image_resource', side_effect=failure):
                with pytest.raises(OSError, match='oom'):
                    term.graphics.negotiate(force=True)
            with mock.patch('hamingjur.graphics.image_resource', side_effect=PermissionError(
                    errno.EPERM, 'denied')):
                support = term.graphics.negotiate(force=True)
                assert support.formats == {100} and support.transfers == {Transfer.DIRECT}
