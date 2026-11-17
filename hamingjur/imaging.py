"""Adaptive Pillow image preparation and owned graphics transport resources."""
# Pillow and shared-memory imports are optional, loaded only by the APIs that need them.
# pylint: disable=import-outside-toplevel
from __future__ import annotations

# std imports
import io
import os
import zlib
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
    """An acknowledged image placement and its chosen representation."""

    image_id: int
    image: PreparedImage
    transfer: Transfer


def _precision(image: Any, display: Mapping[str, str]) -> Any:
    """Match known output precision; absent information never implies eight-bit output."""
    # Pillow is optional; importing Hamingjur does not load it.
    # 3rd party
    from PIL import Image

    model = display.get('color')
    legacy = int(display.get('bits', '0'))
    alpha = image.getchannel('A') if image.mode == 'RGBA' else None
    if model in {'gray', 'mono'}:
        result = image.convert('L')
        bits = 1 if model == 'mono' else int(display.get('gray_bits', str(legacy)))
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
        for name, channel in zip(('red', 'green', 'blue'), image.convert('RGB').split()):
            bits = int(display.get(name + '_bits', str(legacy)))
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


def prepare_image(source: Any, formats: Iterable[int], *, width: Optional[int] = None,
                  height: Optional[int] = None, display: Optional[Mapping[str, str]] = None,
                  match_display: bool = True) -> PreparedImage:
    """
    Fit a PIL image without upscaling, then compare cheap lossless encodings.

    Display matching is the only color reduction; set match_display=False to keep
    source precision. Alpha is retained. PNG palettes, packed grayscale, RGB565,
    raw RGB/RGBA and fast zlib are compared where accepted, without JPEG or dithering.
    The original image is never modified. Pillow is required only for this API.

    :raises TypeError: If source is not a PIL image.
    :raises ValueError: If no accepted format represents the resulting pixels losslessly.
    """
    # 3rd party
    from PIL import Image, ImageOps

    if not isinstance(source, Image.Image):
        raise TypeError('Expected a PIL Image; install hamingjur[images] for Pillow support')
    formats = frozenset(formats)
    for name, value in (('width', width), ('height', height)):
        if value is not None:
            integer(value, name, 1, 0xffffffff)
    image = ImageOps.exif_transpose(source).convert('RGBA')
    image.thumbnail((width if width is not None else image.width,
                     height if height is not None else image.height), Image.Resampling.LANCZOS)
    if image.getchannel('A').getextrema() == (255, 255):
        image = image.convert('RGB')
    if match_display and display:
        image = _precision(image, display)
    image.info.clear()  # Ancillary source metadata is not needed to render the prepared pixels.
    best = min(_representations(image, formats), key=lambda value: value.wire_size, default=None)
    if best is None:
        raise ValueError('No supported format can represent this image without further loss')
    return best


def _representations(image: Any, formats: frozenset[int]) -> Iterator[PreparedImage]:
    """Compare compression for each lossless representation without retaining all candidates."""
    size = Size(*image.size)
    for data, pixel_format in _encodings(image, formats):
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
