"""Adaptive Pillow image preparation and owned graphics transport resources."""
# Load Pillow and shared-memory modules only when the corresponding APIs use them.
# pylint: disable=import-outside-toplevel
from __future__ import annotations

# std imports
import io
import os
import zlib
import struct
import tempfile
import contextlib
from enum import Enum
from typing import Any, Mapping, Iterable, Iterator, Optional
from dataclasses import dataclass

# local
from .window import Size
from .protocols import integer


class Transfer(str, Enum):
    """Kitty/Silkie image transfer media, using their wire values."""

    DIRECT = 'd'
    FILE = 'f'
    TEMPORARY_FILE = 't'
    SHARED_MEMORY = 's'
    GPU_SURFACE = 'g'


@dataclass(frozen=True)
class ImageSupport:
    """Accepted source formats and usable transports for this terminal connection."""

    formats: frozenset[int]
    transfers: frozenset[Transfer]


@dataclass(frozen=True)
class PreparedImage:
    """A fitted image and the smallest measured cheap lossless wire encoding."""

    data: bytes
    format: int
    size: Size
    compressed: bool

    @property
    def wire_size(self) -> int:
        """Direct upload bytes with image ID 1, including base64, fields and chunk framing."""
        encoded = ((len(self.data) + 2) // 3) * 4
        # The graphics protocol limits base64 packets to 4096 bytes.
        packets = max(1, (encoded + 4095) // 4096)
        header = (f'\x1b_Ga=T,i=1,f={self.format},s={self.size.width},v={self.size.height},'
                  f'q=0,m=0{",o=z" if self.compressed else ""};\x1b\\')
        return encoded + len(header) + (packets - 1) * len('\x1b_Gq=0,m=0;\x1b\\')


@dataclass(frozen=True)
class DrawnImage:
    """A written image placement and its chosen representation."""

    image_id: int
    image: PreparedImage
    transfer: Transfer
    acknowledged: bool = True


def _display_widths(display: Mapping[str, str]) -> tuple[int, ...]:
    """Read active channel widths, using legacy precision for omitted fields."""
    model = display.get('color', '')
    if model == 'mono':
        return (1,)
    channels = {'gray': ('gray',), 'rgb': ('red', 'green', 'blue')}.get(model, ())
    return tuple(int(display.get(channel + '_bits', display.get('bits', '0')))
                 for channel in channels)


def _precision(image: Any, display: Mapping[str, str]) -> Any:
    """Match known output precision; absent information never implies eight-bit output."""
    # 3rd party
    from PIL import Image

    model = display.get('color')
    widths = _display_widths(display)
    alpha = image.getchannel('A') if image.mode == 'RGBA' else None
    if model in {'gray', 'mono'}:
        result = image.convert('L')
        bits, = widths
        if 0 < bits < 8:
            levels = (1 << bits) - 1
            result = result.point([((value * levels + 127) // 255 * 255 + levels // 2)
                                   // levels for value in range(256)])
        if alpha is not None:
            result.putalpha(alpha)
            result = result.convert('RGBA')
        return result
    if model == 'rgb':
        channels = []
        for bits, channel in zip(widths, image.convert('RGB').split()):
            adjusted = channel
            if 0 < bits < 8:
                levels = (1 << bits) - 1
                adjusted = channel.point([((value * levels + 127) // 255 * 255 + levels // 2)
                                          // levels for value in range(256)])
            channels.append(adjusted)
        result = Image.merge('RGB', channels)
        if alpha is not None:
            result.putalpha(alpha)
        return result
    return image


def _packed_gray(image: Any, bits: int) -> bytes:
    """Pack each row independently with the leftmost sample in the most significant bits."""
    if bits == 8:
        return image.tobytes()
    if bits == 1:
        # 3rd party
        from PIL import Image
        return image.convert('1', dither=Image.Dither.NONE).tobytes()
    pixels = image.tobytes()
    width, height = image.size
    stride = (width * bits + 7) // 8
    result = bytearray(stride * height)
    step = 255 // ((1 << bits) - 1)
    for y in range(height):
        for x in range(width):
            result[y * stride + x * bits // 8] |= (
                pixels[y * width + x] // step) << (8 - bits - x * bits % 8)
    return bytes(result)


def _rgb565(image: Any) -> Optional[bytes]:
    """Use RGB565 only when expanding it reproduces every channel exactly."""
    red_blue = {(sample * 255 + 15) // 31: sample for sample in range(32)}
    green = {(sample * 255 + 31) // 63: sample for sample in range(64)}
    raw = image.convert('RGB').tobytes()
    result = bytearray(len(raw) // 3 * 2)
    for offset in range(0, len(raw), 3):
        r, g, b = raw[offset:offset + 3]
        if r not in red_blue or g not in green or b not in red_blue:
            return None
        word = red_blue[r] << 11 | green[g] << 5 | red_blue[b]
        index = offset // 3 * 2
        result[index:index + 2] = word.to_bytes(2, 'little')
    return bytes(result)


def _png(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', compress_level=1)
    return buffer.getvalue()


def _palette(image: Any) -> Optional[Any]:
    """Build an exact PNG palette when all colors fit the format's 256-entry alphabet."""
    # 3rd party
    from PIL import Image

    colors = image.getcolors(maxcolors=257)
    if colors is None or len(colors) > 256:
        return None
    values = [color for _, color in colors]
    lookup = {color: index for index, color in enumerate(values)}
    channels = len(image.getbands())
    raw = image.tobytes()
    indexed = Image.new('P', image.size)
    indexed.putpalette([component for color in values for component in color[:3]])
    indexed.putdata([lookup[tuple(raw[offset:offset + channels])]
                     for offset in range(0, len(raw), channels)])
    if image.mode == 'RGBA':
        indexed.info['transparency'] = bytes(color[3] for color in values)
    return indexed


def _fit_gray16(image: Any, bounds: tuple[int, int]) -> tuple[Any, Optional[Any]]:
    """Fit unsigned grayscale using Pillow's integer Lanczos path and keep PNG transparency."""
    from PIL import Image

    transparent = image.info.get('transparency')
    # Converting via I is lossless for either byte order and supports Lanczos on older Pillow.
    image = image.convert('I')
    alpha = None
    if isinstance(transparent, int):
        samples = struct.iter_unpack('<H', image.convert('I;16').tobytes())
        mask = bytes(0 if value == transparent else 255 for (value,) in samples)
        if 0 in mask:
            alpha = Image.frombytes('L', image.size, mask)
            alpha.thumbnail(bounds, Image.Resampling.LANCZOS)
    image.thumbnail(bounds, Image.Resampling.LANCZOS)
    # Converting to I;16 clamps any filter overshoot and normalizes to little-endian.
    return image.convert('I;16'), alpha


def _gray16_encodings(image: Any, alpha: Optional[Any], formats: frozenset[int],
                      display: Mapping[str, str]) -> Iterator[tuple[bytes, int]]:
    """Offer native 16-bit samples and any exact (or explicitly display-matched) 8-bit form."""
    from PIL import Image

    data = image.tobytes()
    widths = _display_widths(display)
    if widths and all(0 < bits <= 8 for bits in widths):
        channels = {}
        for bits in dict.fromkeys(widths):
            levels = (1 << bits) - 1
            samples = struct.iter_unpack('<H', data)
            pixels = bytes(((value * levels + 32767) // 65535 * 255 + levels // 2) // levels
                           for (value,) in samples)
            channels[bits] = Image.frombytes('L', image.size, pixels)
        raster = (channels[widths[0]] if len(widths) == 1 else
                  Image.merge('RGB', tuple(channels[bits] for bits in widths)))
        if alpha is not None:
            raster = raster.convert('RGB')
            raster.putalpha(alpha)
        matched = _encodings(raster, formats)
        first = next(matched, None)
        if first is not None:
            yield first
            yield from matched
            return
    if alpha is None and 316 in formats:
        yield data, 316
    low, high = data[::2], data[1::2]
    if 664 in formats and (alpha is not None or 316 not in formats):
        # Silkie uses little-endian 16-bit R, G, B and A. Duplicating each
        # 8-bit alpha byte scales it to the corresponding 16-bit value.
        opacity = alpha.tobytes() if alpha is not None else b'\xff' * len(low)
        rgba = bytearray(4 * len(data))
        for offset in (0, 2, 4):
            rgba[offset::8], rgba[offset + 1::8] = low, high
        rgba[6::8], rgba[7::8] = opacity, opacity
        yield bytes(rgba), 664
    if low != high:  # The exact expansion of an 8-bit sample is value * 257.
        return
    raster = Image.frombytes('L', image.size, low)
    if alpha is not None:
        raster = raster.convert('RGB')
        raster.putalpha(alpha)
    yield from _encodings(raster, formats)


def prepare_image(source: Any, formats: Iterable[int], *, width: Optional[int] = None,
                  height: Optional[int] = None, display: Optional[Mapping[str, str]] = None,
                  match_display: bool = True) -> PreparedImage:
    """
    Fit a PIL image without upscaling, then compare cheap lossless encodings.

    Display matching is the only color reduction; set match_display=False to keep
    source precision. Alpha is retained. PNG palettes, packed grayscale, RGB565,
    gray16, raw RGB/RGBA and fast zlib are compared where accepted, without JPEG or dithering.
    The original image is never modified.

    :raises TypeError: If source is not a PIL image.
    :raises ValueError: If no accepted format represents the resulting pixels losslessly.
    """
    # 3rd party
    from PIL import Image, ImageOps

    if not isinstance(source, Image.Image):
        raise TypeError('Expected a PIL Image')
    formats = frozenset(formats)
    for name, value in (('width', width), ('height', height)):
        if value is not None:
            integer(value, name, 1, 0xffffffff)
    image = ImageOps.exif_transpose(source)
    bounds = (width if width is not None else image.width,
              height if height is not None else image.height)
    display = display if match_display and display else {}
    # Older Pillow opens unsigned 16-bit grayscale PNG as mode I.
    if image.mode in {'I;16', 'I;16L', 'I;16B', 'I;16N'} or (
            image.mode == 'I' and source.format == 'PNG'):
        image, alpha = _fit_gray16(image, bounds)
        encodings = _gray16_encodings(image, alpha, formats, display)
    else:
        image = image.convert('RGBA')
        image.thumbnail(bounds, Image.Resampling.LANCZOS)
        if image.getchannel('A').getextrema() == (255, 255):
            image = image.convert('RGB')
        if display:
            image = _precision(image, display)
        image.info.clear()  # Ancillary metadata is unnecessary for rendering the prepared pixels.
        encodings = _encodings(image, formats)
    best = min(_representations(Size(*image.size), encodings),
               key=lambda value: value.wire_size, default=None)
    if best is None:
        raise ValueError('No supported format can represent this image without further loss')
    return best


def _representations(size: Size, encodings: Iterable[tuple[bytes, int]]) -> Iterator[PreparedImage]:
    """Compare compression for each lossless representation without retaining all candidates."""
    for data, pixel_format in encodings:
        yield PreparedImage(data, pixel_format, size, False)
        yield PreparedImage(zlib.compress(data, level=1), pixel_format, size, True)


def _encodings(image: Any, formats: frozenset[int]) -> Iterator[tuple[bytes, int]]:
    """Yield only formats that exactly represent the prepared pixel values."""
    # 3rd party
    from PIL import ImageChops

    if 100 in formats:
        yield _png(image), 100
        indexed = _palette(image.convert('RGBA') if image.mode == 'RGBA' else image.convert('RGB'))
        if indexed is not None:
            yield _png(indexed), 100
    opaque = image.mode != 'RGBA'
    raw_format = 24 if opaque else 32
    if raw_format in formats:
        yield image.convert('RGB' if opaque else 'RGBA').tobytes(), raw_format
    if opaque:
        rgb = image.convert('RGB')
        red, green, blue = rgb.split()
        if (ImageChops.difference(red, green).getbbox() is None
                and ImageChops.difference(red, blue).getbbox() is None):
            histogram = red.histogram()
            for bits in (1, 2, 4, 8):
                if 300 + bits in formats:
                    step = 255 // ((1 << bits) - 1)
                    if all(not count or value % step == 0
                           for value, count in enumerate(histogram)):
                        yield _packed_gray(red, bits), 300 + bits
        if 516 in formats:
            packed = _rgb565(rgb)
            if packed is not None:
                yield packed, 516


@contextlib.contextmanager
def image_resource(data: bytes, transfer: Transfer) -> Iterator[bytes]:
    """
    Own a local resource until its query/upload is acknowledged, including error cleanup.

    :raises ValueError: If the transfer does not use a local file or SHM allocation.
    """
    if transfer in {Transfer.FILE, Transfer.TEMPORARY_FILE}:
        descriptor, path = tempfile.mkstemp(prefix='tty-graphics-protocol-')
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
            yield os.fsencode(path)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass  # The terminal deletes temporary-file transports itself.
    elif transfer == Transfer.SHARED_MEMORY:
        # std imports
        from multiprocessing.shared_memory import SharedMemory
        memory = SharedMemory(create=True, size=len(data))
        try:
            # macOS can round allocations to a page; S carries the actual byte count.
            view = memory.buf
            assert view is not None
            view[:len(data)] = data
            name = memory.name if os.name == 'nt' else '/' + memory.name.lstrip('/')
            yield name.encode('ascii')
        finally:
            memory.close()
            try:
                memory.unlink()
            except FileNotFoundError:
                # Kitty unlinks POSIX SHM after reading. Unregister our own allocation
                # when it is already gone, so Python does not report a resource leak.
                # std imports
                from multiprocessing import resource_tracker

                # pylint: disable-next=protected-access
                resource_tracker.unregister(memory._name, 'shared_memory')
    else:
        raise ValueError('This transfer requires an application-owned GPU surface or direct bytes')


# Formats whose colors are independent of terminal state and fit in an SDR raster.
FALLBACK_FORMATS = frozenset({24, 32, 100, 301, 302, 304, 308, 401, 402, 404, 408, 516})


def decode_image(data: bytes, pixel_format: int, width: int, height: int) -> Any:
    """Decode a portable wire image to RGBA for another graphics backend."""
    from PIL import Image

    if pixel_format == 100:
        with Image.open(io.BytesIO(data), formats=['PNG']) as source:
            return source.convert('RGBA')
    if pixel_format not in FALLBACK_FORMATS:
        raise NotImplementedError(f'Image format {pixel_format} requires native terminal support')
    integer(width, 'width', 1, 0xffffffff)
    integer(height, 'height', 1, 0xffffffff)
    if pixel_format in (24, 32):
        mode = 'RGB' if pixel_format == 24 else 'RGBA'
        if len(data) != width * height * len(mode):
            raise ValueError('Raw pixel data does not match the image dimensions')
        return Image.frombytes(mode, (width, height), data).convert('RGBA')
    if pixel_format == 516:
        if len(data) != width * height * 2:
            raise ValueError('RGB565 data does not match the image dimensions')
        rgb = bytearray()
        for low, high in zip(data[::2], data[1::2]):
            word = low | high << 8
            rgb.extend((((word >> 11) * 255 + 15) // 31,
                        ((word >> 5 & 63) * 255 + 31) // 63,
                        ((word & 31) * 255 + 15) // 31))
        return Image.frombytes('RGB', (width, height), bytes(rgb)).convert('RGBA')
    bits = pixel_format % 100
    slots = 1 << bits
    palette_size = 4 * slots if pixel_format >= 400 else 0
    stride = (width * bits + 7) // 8
    if len(data) != palette_size + stride * height:
        raise ValueError('Packed pixel data does not match the image dimensions')
    indices = bytearray(width * height)
    for y in range(height):
        row = palette_size + y * stride
        for x in range(width):
            bit = x * bits
            indices[y * width + x] = (data[row + bit // 8] >> (8 - bits - bit % 8)) & (slots - 1)
    if palette_size:
        indexed = Image.frombytes('P', (width, height), bytes(indices))
        indexed.putpalette(data[:palette_size], rawmode='RGBA')
        return indexed.convert('RGBA')
    gray = bytes(value * 255 // (slots - 1) for value in indices)
    return Image.frombytes('L', (width, height), gray).convert('RGBA')
