"""Streaming Kitty raster, animation, placement and Silkie vector/GPU commands."""
# std imports
import re
import json
import zlib
import errno
import contextlib
from typing import TYPE_CHECKING, Any, Union, Mapping, BinaryIO, Iterable, Iterator, Optional
from functools import lru_cache
from importlib.resources import files

# local
from .imaging import (Transfer,
                      DrawnImage,
                      FALLBACK_FORMATS,
                      ImageSupport,
                      PreparedImage,
                      decode_image,
                      prepare_image,
                      image_resource)
from ._graphics_compat import GraphicsCompatibility, KITTY_FORMATS
from .protocols import ST, Field, ProtocolEvent, b64, safe, unb64, integer, parse_event

Data = Union[bytes, bytearray, memoryview, BinaryIO, Iterable[bytes]]
# Kitty's wire format permits at most 4096 base64 bytes per direct-data packet.
GRAPHICS_CHUNK_BYTES = 4096 // 4 * 3
# Format identifiers are not bit depths: 8/16 index the terminal palette,
# while Silkie's raw format families encode their bit depth in the last digits.
_RAW_FORMAT_BITS = {
    301: 1, 302: 2, 304: 4, 308: 8, 516: 16,
    8: 8, 16: 16, 24: 24, 32: 32, 64: 64,
    316: 16, 632: 32, 664: 64,
    401: 1, 402: 2, 404: 4, 408: 8,
}
_PALETTE_BYTES = {400 + bits: 4 * (1 << bits) for bits in (1, 2, 4, 8)}
_FORMAT_SAMPLES = {
    pixel_format: bytes(_PALETTE_BYTES.get(pixel_format, 0) + (bits + 7) // 8)
    for pixel_format, bits in _RAW_FORMAT_BITS.items()
}
_FORMAT_SAMPLES.update({
    100: unb64('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe'
               'AAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC'),
    200: b'p id=1 d=M0,0L1,0L1,1Z',
    201: b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
})


def chunks(data: Data, size: int) -> Iterator[bytes]:
    """
    Reblock input without a total-size limit, including streams with short reads.

    :raises TypeError: If a payload has the wrong type.
    """
    integer(size, 'chunk size', 1)
    source: Iterator[Any]
    if isinstance(data, (bytes, bytearray, memoryview)):
        source = (memoryview(data)[offset:offset + size]
                  for offset in range(0, len(data), size))
    elif hasattr(data, 'read'):
        source = iter(lambda: data.read(size), b'')
    else:
        source = iter(data)
    pending = bytearray()
    for block in source:
        if not isinstance(block, (bytes, bytearray, memoryview)):
            raise TypeError('Payload sources must yield bytes')
        view = memoryview(block)
        offset = 0
        while offset < len(view):
            take = min(size - len(pending), len(view) - offset)
            pending.extend(view[offset:offset + take])
            offset += take
            if len(pending) == size:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def _compressed(data: Data) -> Iterator[bytes]:
    compressor = zlib.compressobj()
    for block in chunks(data, GRAPHICS_CHUNK_BYTES):
        yield compressor.compress(block)
    yield compressor.flush()


def _header(fields: Mapping[str, Field]) -> str:
    values = []
    for key, value in fields.items():
        if not re.fullmatch('[a-zA-Z]', key):
            raise ValueError('Graphics keys are single ASCII letters')
        if isinstance(value, int):
            # z and relative placement offsets H/V are signed 32-bit fields.
            signed = key in {'z', 'H', 'V'}
            integer(value, key, -(1 << 31) if signed else 0,
                    (1 << 31) - 1 if signed else (1 << 32) - 1)
        elif not re.fullmatch('[a-zA-Z]', value):
            raise ValueError('Graphics values must be integers or single ASCII letters')
        values.append(f'{key}={value}')
    return ','.join(values)


def command(**fields: Field) -> str:
    """Encode a control-only command, including placement, animation and deletion."""
    return '\x1b_G' + _header(fields) + ST


def transmit(data: Data, *, compress: bool = False, **fields: Field) -> Iterator[str]:
    """
    Encode all graphics actions and transports, streaming direct payloads.

    Keys use the protocol's spelling. This supports raster formats 24/32/100, Silkie
    packed grayscale (301/302/304/308), RGB565 (516), palette (401/402/404/408),
    high precision (316/632/664), HDR (64), vectors (200) and SVG (201),
    animation frames (a=f), vector patches (a=e), and services (a=v). For t=f/t=t/t=s/t=g, data
    names a resource already owned by the caller; this function never allocates or deletes
    operating-system resources.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if 'm' in fields:
        raise ValueError('Chunk continuation is managed by transmit()')
    fields = dict(fields)
    if fields.get('t', 'd') != 'd':
        yield _resource(data, fields, compress)
        return
    if compress:
        if 'o' in fields:
            raise ValueError('Do not specify o when compress=True')
        fields['o'] = 'z'
        data = _compressed(data)
    _header(fields)  # Validate before consuming the source.
    parts = iter(chunks(data, GRAPHICS_CHUNK_BYTES))
    current = next(parts, b'')
    continuation: dict[str, Field] = {}
    if fields.get('a') == 'f':
        continuation['a'] = 'f'
    if 'q' in fields:
        continuation['q'] = fields['q']
    while True:
        following = next(parts, None)
        yield '\x1b_G' + _header({**fields, 'm': int(following is not None)}) + (
            ';' + b64(current) + ST)
        if following is None:
            break
        current, fields = following, continuation


def _resource(data: Data, fields: Mapping[str, Field], compress: bool) -> str:
    """
    Encode a single resource reference; only direct transfers may be chunked.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if compress:
        raise ValueError('compress applies to direct payloads, not resource names')
    if fields.get('t') == 'g' and 'o' in fields:
        raise ValueError('GPU surfaces do not support compression')
    parts = iter(chunks(data, GRAPHICS_CHUNK_BYTES))
    first = next(parts, b'')
    if next(parts, None) is not None:
        raise ValueError('Resource name exceeds the 4096-byte encoded packet')
    return '\x1b_G' + _header(fields) + ';' + b64(first) + ST


def image(data: Data, *, image_id: int, format: int = 100,
          width: Optional[int] = None, height: Optional[int] = None,
          display: bool = True, compress: bool = False, **placement: Field) -> Iterator[str]:
    """
    Transmit an image. PNG and SVG can use their intrinsic dimensions.

    Grayscale formats 301/302/304/308 pack pixels most-significant first, padding
    each row to a byte boundary. Format 516 is little-endian RGB565. Palette
    formats 401/402/404/408 start with all 2**bits straight RGBA palette slots,
    followed by rows of packed indices. Formats 8/16 index the terminal palette,
    with per-pixel alpha in format 16. High precision formats 316/632/664 carry
    little-endian gray16/RGB10A2/RGBA16. Data is already packed; no conversion is
    performed. Source formats do not depend on the display's color precision.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    # pylint: disable=redefined-builtin
    integer(image_id, 'image_id', 1, 0xffffffff)
    if format not in _FORMAT_SAMPLES:
        raise ValueError('Use transmit() with a sample for an unknown image format')
    fields: dict[str, Field] = {'a': 'T' if display else 't', 'i': image_id, 'f': format}
    if (width is None) != (height is None) or (format not in {100, 201} and width is None):
        raise ValueError('This image requires both width and height')
    if width is not None and height is not None:
        fields.update(s=integer(width, 'width', 1, 0xffffffff),
                      v=integer(height, 'height', 1, 0xffffffff))
        if (format in _RAW_FORMAT_BITS and placement.get('t', 'd') == 'd'
                and isinstance(data, (bytes, bytearray, memoryview))):
            bits = _RAW_FORMAT_BITS[format]
            expected = _PALETTE_BYTES.get(format, 0) + ((width * bits + 7) // 8) * height
            if memoryview(data).nbytes != expected:
                raise ValueError('Raw pixel data does not match the image dimensions')
    if fields.keys() & placement.keys():
        raise ValueError('Placement options cannot override image parameters')
    yield from transmit(data, compress=compress, **fields, **placement)


def place(image_id: int, **fields: Field) -> str:
    """Place a stored image; supports cropping, offsets, z-order and relative placements."""
    return command(a='p', i=integer(image_id, 'image_id', 1, 0xffffffff), **fields)


def delete(image_id: Optional[int] = None, *, free: bool = True, **fields: Field) -> str:
    """Delete an image's placements or all visible placements, optionally freeing data."""
    selector = 'a' if image_id is None else 'i'
    if image_id is not None:
        fields['i'] = integer(image_id, 'image_id', 1, 0xffffffff)
    return command(a='d', d=selector.upper() if free else selector, **fields)


def json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode UTF-8 JSON, rejecting non-finite values."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def vector_record(kind: str, **fields: Any) -> str:
    """
    Build any vector record, including groups, paths, symbols, ink and gradients.

    Sequence values become comma-separated lists. Use text_record() for text; path data must already
    use the protocol's whitespace-free SVG syntax.

    :raises ValueError: If arguments or protocol data are invalid.
    """
    if not re.fullmatch('[a-z]', kind):
        raise ValueError('Vector record types are single lowercase letters')
    parts = [kind]
    for key, value in fields.items():
        if not re.fullmatch('[a-z][a-z0-9_]*', key):
            raise ValueError('Invalid vector field name')
        encoded = ','.join(map(str, value)) if isinstance(value, (tuple, list)) else value
        parts.append(f'{key}={safe(encoded, "")}')
    return ' '.join(parts)


def text_record(element_id: int, layout: Mapping[str, Any], *, group: int = 0,
                **fields: Any) -> str:
    """Encode shaped/styled vector text with arbitrary Unicode and layout properties."""
    return vector_record('t', id=integer(element_id, 'element_id', 1, 0xffffffff),
                         g=integer(group, 'group', 0, 0xffffffff),
                         text=b64(json_bytes(layout)), **fields)


def scene(records: Iterable[str], *, image_id: int, width: int,
          height: int, compress: bool = False, **fields: Field) -> Iterator[str]:
    """Stream a vector scene without accumulating all its records in memory."""
    data = ((record + '\n').encode('utf-8') for record in records)
    yield from image(data, image_id=image_id, format=200, width=width, height=height,
                     compress=compress, **fields)


def svg(document: Union[str, Data], *, image_id: int, width: Optional[int] = None,
        height: Optional[int] = None, display: bool = True, compress: bool = False,
        **fields: Field) -> Iterator[str]:
    """Stream a static SVG document in Silkie's retained graphics format."""
    data = document.encode('utf-8') if isinstance(document, str) else document
    yield from image(data, image_id=image_id, format=201, width=width, height=height,
                     display=display, compress=compress, **fields)


def patch(image_id: int, records: Iterable[str], *, compress: bool = False,
          **fields: Field) -> Iterator[str]:
    """Atomically edit a stored vector scene, including appending live pen strokes."""
    data = ((record + '\n').encode('utf-8') for record in records)
    yield from transmit(data, a='e', i=integer(image_id, 'image_id', 1, 0xffffffff),
                        compress=compress, **fields)


def vector_service(request_id: int, operation: str, **fields: Any) -> Iterator[str]:
    """List/inspect/upload/release fonts or measure text through Silkie's vector service."""
    yield from transmit(json_bytes({'op': operation, **fields}), a='v', f=200, q=0,
                        i=integer(request_id, 'request_id', 1, 0xffffffff))


def service_result(response: str) -> Mapping[str, Any]:
    """
    Decode a vector service reply, raising ValueError for terminal errors.

    :raises ValueError: If the terminal reports an error or the result is malformed.
    """
    event = parse_event(response)
    if event.protocol != 'graphics':
        raise ValueError('Expected a graphics response')
    if event.payload.startswith('OK;'):
        result = json.loads(unb64(event.payload[3:]))
        if isinstance(result, dict):
            return result
        raise ValueError('Expected a JSON object in vector service reply')
    if event.payload.startswith('EINVAL:'):
        try:
            error = json.loads(unb64(event.payload[7:]))
        except ValueError:
            error = {'error': event.payload}
        raise ValueError(error)
    raise ValueError(event.payload)


@lru_cache(maxsize=1)
def _diacritics() -> tuple[str, ...]:
    """Load the protocol's fixed Unicode row/column alphabet, not a buffer limit."""
    data = files('hamingjur').joinpath('graphics-diacritics.txt').read_text(encoding='ascii')
    return tuple(chr(int(line, 16)) for line in data.splitlines()
                 if line and not line.startswith('#'))


def placeholder(image_id: int, rows: int, columns: int, *, placement_id: int = 0
                ) -> Iterator[str]:
    """Yield Unicode placeholder rows for a virtual placement (a=p,U=1)."""
    integer(image_id, 'image_id', 1, 0xffffffff)
    # Placement IDs are carried in the 24-bit underline color in this protocol.
    integer(placement_id, 'placement_id', 0, 0xffffff)
    alphabet = _diacritics()
    integer(rows, 'rows', 1, len(alphabet))
    integer(columns, 'columns', 1)

    def rgb(value: int) -> str:
        return f'{(value >> 16) & 255};{(value >> 8) & 255};{value & 255}'
    color = '\x1b[38;2;' + rgb(image_id) + 'm\x1b[58;2;' + rgb(placement_id) + 'm'
    high = alphabet[image_id >> 24]
    for row in range(rows):
        # Following cells inherit row/high ID and increment the column. This
        # supports widths beyond the fixed alphabet without changing placement.
        yield (color + '\U0010eeee' + alphabet[row] + alphabet[0] + high
               + '\U0010eeee' * (columns - 1) + '\x1b[39;59m')


if TYPE_CHECKING:
    # local
    from .terminal import Terminal


# Bound graphics queries share Terminal's input-preserving transport.
# pylint: disable=protected-access
class Graphics:
    """Image encoders and terminal queries, available as ``term.graphics``."""

    def __init__(self, terminal: 'Terminal'):
        """Bind graphics output and queries to a terminal."""
        self.terminal = terminal
        self._support: Optional[ImageSupport] = None
        self._display_report: Optional[ProtocolEvent] = None
        self._display_probed = False
        self._has_display = False
        self._image_id = 0
        self._explicit_image_ids: set[int] = set()
        self._compatibility = GraphicsCompatibility(self)

    def negotiate(self, timeout: Optional[float] = 1.0, *, force: bool = False) -> ImageSupport:
        """
        Discover formats and probe local file/SHM access on this connection.

        Successful negotiation is cached; force=True refreshes after reconnection.
        GPU support is advertised only: gpu_surface() still requires an external descriptor.
        A remote connection cannot gain a local transport merely by advertising it.

        :raises OSError: If resource allocation fails beyond permission/platform refusal.
        """
        if self._support is not None and not force:
            return self._support
        if force:
            self._support = None
            self._display_report = None
            self._display_probed = False
            self._has_display = False
            self._compatibility.native = None
            self._compatibility.sixel = None
            self._compatibility.native_formats.clear()
        empty = ImageSupport(frozenset(), frozenset())
        if not self.terminal.does_styling:
            return empty
        caps = self.terminal.silkie.capabilities(timeout, force=force)
        self._has_display = caps is not None and caps.supports('display')
        formats = caps.formats if caps and caps.formats else KITTY_FORMATS
        # Probe a format even when a terminal advertises it; a relay may not carry graphics.
        pixel_format = next((value for value in _FORMAT_SAMPLES
                             if value in formats and value not in (64, 200, 201)), None)
        if pixel_format is not None and self._supports_native(pixel_format, timeout):
            self._compatibility.native = True
            media = caps.media if caps else frozenset(('d', 'f', 't', 's'))
            transfers = self._probe_transfers(_FORMAT_SAMPLES[pixel_format], pixel_format,
                                              media, timeout)
            self._support = ImageSupport(formats, frozenset(transfers))
        elif formats != KITTY_FORMATS and self._supports_native(24, timeout):
            self._compatibility.native = True
            self._support = ImageSupport(KITTY_FORMATS, frozenset((Transfer.DIRECT,)))
        else:
            self._compatibility.native = False
            if force:
                self._compatibility.sixel = self.terminal.does_sixel(timeout, force=True)
            if not self._compatibility.has_sixel(timeout):
                return empty
            self._support = ImageSupport(FALLBACK_FORMATS, frozenset((Transfer.DIRECT,)))
        return self._support

    def _probe_transfers(self, sample: bytes, pixel_format: int, media: frozenset[str],
                         timeout: Optional[float]) -> set[Transfer]:
        """
        Verify resource accessibility rather than inferring locality from advertisement.

        :raises OSError: If resource allocation exhausts real OS capacity.
        """
        transfers = {Transfer.DIRECT}
        for medium in (Transfer.FILE, Transfer.TEMPORARY_FILE, Transfer.SHARED_MEMORY):
            if medium.value not in media:
                continue
            try:
                with image_resource(sample, medium) as name:
                    response = self.query(name, t=medium.value, f=pixel_format,
                                          s=1, v=1, S=len(sample), timeout=timeout)
                    if response is not None and response.payload == 'OK':
                        transfers.add(medium)
            except NotImplementedError:
                continue
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EPERM, errno.ENOSYS, errno.ENOTSUP):
                    raise
                # Permission/platform refusal removes this medium; capacity failures propagate.
        if Transfer.GPU_SURFACE.value in media:
            transfers.add(Transfer.GPU_SURFACE)
        return transfers

    def transfers(self, timeout: Optional[float] = 1.0, *, force: bool = False
                  ) -> frozenset[Transfer]:
        """Return usable enum-valued image transfers, probing actual local resource access."""
        return self.negotiate(timeout, force=force).transfers

    def observe(self, event: Optional[ProtocolEvent]) -> None:
        """Update adaptive drawing state from an incoming display report."""
        if event is not None and event.protocol == 'silkie:display':
            self._display_report = event

    def prepare(self, source: Any, *, width: Optional[int] = None,
                height: Optional[int] = None, columns: Optional[int] = None,
                rows: Optional[int] = None, match_display: bool = True,
                timeout: Optional[float] = 1.0) -> PreparedImage:
        """
        Fit a PIL image to pixels or cells and choose its smallest cheap lossless payload.

        With no target, fit the current viewport. Resizes invalidate cell geometry;
        display reports update precision. The source image remains available for redraw.

        :raises ValueError: If pixel and cell units are mixed.
        :raises RuntimeError: If graphics support or required pixel geometry is unavailable.
        """
        cells = columns is not None or rows is not None
        if cells and (width is not None or height is not None):
            raise ValueError('Specify pixel dimensions or cell dimensions, not both')
        support = self.negotiate(timeout)
        if not support.formats:
            raise RuntimeError('No supported graphics backend is available')
        if cells:
            cell = self.terminal.get_cell_size(timeout)
            if cell is None:
                raise RuntimeError('Cell pixel dimensions are unavailable; specify pixels')
            if columns is not None:
                width = integer(columns, 'columns', 1) * cell.width
            if rows is not None:
                height = integer(rows, 'rows', 1) * cell.height
        elif width is None and height is None:
            width, height = self.terminal.pixel_width, self.terminal.pixel_height
            if not width or not height or width < 0 or height < 0:
                cell = self.terminal.get_cell_size(timeout)
                if cell is None:
                    raise RuntimeError('Viewport pixels are unavailable; specify a target size')
                width, height = cell.width * self.terminal.width, cell.height * self.terminal.height
        if (match_display and self._display_report is None and self._has_display
                and not self._display_probed):
            self._display_report = self.terminal.display.query(timeout)
            self._display_probed = True
        return prepare_image(source, support.formats, width=width, height=height,
                             display=self._display_report.fields if self._display_report else None,
                             match_display=match_display)

    def draw(self, source: Any, *, image_id: Optional[int] = None,
             transfer: Optional[Transfer] = None, x: Optional[int] = None,
             y: Optional[int] = None, width: Optional[int] = None, height: Optional[int] = None,
             columns: Optional[int] = None, rows: Optional[int] = None,
             match_display: bool = True, placement_id: Optional[int] = None,
             timeout: Optional[float] = 1.0) -> Optional[DrawnImage]:
        """
        Draw a PIL image, choosing packing, compression and a negotiated transfer.

        x/y are zero-based cell coordinates; omit both to use the cursor. Local resources
        survive until acknowledgement and are cleaned on failures. A timeout raises rather
        than silently redrawing. Returns None without styling.
        Supply a nonzero placement_id to attach Silkie accessibility metadata afterwards.

        :raises ValueError: If geometry, prepared formats or requested transfers are invalid.
        """
        # Separate pixel/cell geometry and placement are intentionally convenient keyword arguments.
        # pylint: disable=too-many-arguments,too-many-locals
        if not self.terminal.does_styling:
            return None
        if placement_id is not None:
            integer(placement_id, 'placement_id', 1, 0xffffffff)
        if (x is None) != (y is None):
            raise ValueError('Specify both x and y, or neither')
        if x is not None and y is not None:
            integer(x, 'x')
            integer(y, 'y')
        if isinstance(source, PreparedImage):
            if any(value is not None for value in (width, height, columns, rows)):
                raise ValueError('Prepared images already have their target dimensions')
            prepared = source
        else:
            prepared = self.prepare(source, width=width, height=height, columns=columns, rows=rows,
                                    match_display=match_display, timeout=timeout)
        support = self.negotiate(timeout)
        if not support.formats:
            raise RuntimeError('No supported graphics backend is available')
        if prepared.format not in support.formats:
            if prepared.format not in FALLBACK_FORMATS or not support.formats & {32, 100}:
                raise ValueError('The prepared image format is unavailable on this connection')
            data = zlib.decompress(prepared.data) if prepared.compressed else prepared.data
            raster = decode_image(data, prepared.format, *prepared.size)
            prepared = prepare_image(raster, support.formats, match_display=False)
        automatic = transfer is None
        if automatic:
            transfer = next((medium for medium in (
                Transfer.SHARED_MEMORY, Transfer.TEMPORARY_FILE, Transfer.FILE)
                if medium in support.transfers), Transfer.DIRECT)
        assert transfer is not None
        transfer = Transfer(transfer)
        if transfer not in support.transfers:
            raise ValueError('The requested image transfer is unavailable on this connection')
        if transfer == Transfer.GPU_SURFACE:
            raise ValueError('Use gpu_surface() with an application-owned GPU descriptor')
        image_id = self._allocate_image_id(image_id)
        location = self.terminal.location(x, y) if x is not None else contextlib.nullcontext()
        if self._compatibility.native is False:
            if placement_id is not None:
                raise NotImplementedError('Sixel does not support placement IDs')
            data = zlib.decompress(prepared.data) if prepared.compressed else prepared.data
            raster = decode_image(data, prepared.format, *prepared.size)
            with location:
                self.terminal.send(self._compatibility.show_sixel(raster, {}, timeout))
            self._compatibility.store_sixel(image_id, raster)
            return DrawnImage(image_id, prepared, transfer, acknowledged=False)
        with location:
            result = self._upload(prepared, image_id, transfer, automatic, timeout, placement_id)
        self._compatibility.forget(image_id)
        return result

    def forget(self, image_id: int) -> None:
        """
        Release a locally retained image or scene after the application is done with it.

        :raises ValueError: If the image ID is outside the protocol's range.
        """
        self._compatibility.forget(integer(image_id, 'image_id', 1, 0xffffffff))

    def _reserve_image_id(self, image_id: int) -> None:
        """Keep a caller-provided wire ID out of the automatic allocation sequence."""
        if image_id > self._image_id:
            self._explicit_image_ids.add(image_id)

    def _allocate_image_id(self, image_id: Optional[int]) -> int:
        """Avoid explicit IDs without exhausting automatic IDs when a caller picks a high value."""
        if image_id is None:
            self._image_id = integer(self._image_id + 1, 'image_id', 1, 0xffffffff)
            while self._image_id in self._explicit_image_ids:
                self._explicit_image_ids.remove(self._image_id)
                self._image_id = integer(self._image_id + 1, 'image_id', 1, 0xffffffff)
            image_id = self._image_id
        else:
            integer(image_id, 'image_id', 1, 0xffffffff)
            self._reserve_image_id(image_id)
        return image_id

    def _upload(self, prepared: PreparedImage, image_id: int, transfer: Transfer,
                automatic: bool, timeout: Optional[float],
                placement_id: Optional[int] = None) -> DrawnImage:
        """
        Keep owned resources alive until the terminal acknowledges their upload.

        :raises TimeoutError: If the terminal does not acknowledge the transfer.
        :raises RuntimeError: If the terminal rejects the image.
        """
        fields: dict[str, Field] = {'a': 'T', 'i': image_id, 'f': prepared.format,
                                    's': prepared.size.width, 'v': prepared.size.height, 'q': 0}
        if placement_id is not None:
            fields['p'] = placement_id
        if prepared.compressed:
            fields['o'] = 'z'
        if transfer == Transfer.DIRECT:
            sequence = ''.join(transmit(prepared.data, **fields))
            result = self._request(sequence, image_id, timeout)
        else:
            with image_resource(prepared.data, transfer) as name:
                sequence = ''.join(transmit(name, t=transfer.value, S=len(prepared.data), **fields))
                if automatic and len(sequence) >= prepared.wire_size:
                    direct = ''.join(transmit(prepared.data, **fields))
                    if len(sequence) >= len(direct):
                        transfer, sequence = Transfer.DIRECT, direct
                result = self._request(sequence, image_id, timeout)
        if result is None:
            raise TimeoutError('The terminal did not acknowledge the image transfer')
        if result.payload != 'OK':
            raise RuntimeError(result.payload)
        return DrawnImage(image_id, prepared, transfer)

    @contextlib.contextmanager
    def adaptive(self, timeout: Optional[float] = 1.0) -> Iterator['Graphics']:
        """Track window and display changes during an input loop, restoring reporting on exit."""
        self.negotiate(timeout)
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.terminal.notify_on_resize(timeout=timeout))
            if self._has_display:
                stack.enter_context(self.terminal.display.enabled(timeout=timeout))
            yield self

    def send(self, sequences: Union[str, Iterable[str]]) -> None:
        """Write graphics packets, translating them when the terminal needs a fallback."""
        self.terminal.send(sequences)

    def _wire_packets(self, sequence: str) -> Iterator[str]:
        """Translate graphics sent through either bound send method."""
        yield from self._compatibility.packets(sequence)

    command = staticmethod(command)
    transmit = staticmethod(transmit)
    image = staticmethod(image)
    place = staticmethod(place)
    delete = staticmethod(delete)
    scene = staticmethod(scene)
    svg = staticmethod(svg)
    patch = staticmethod(patch)
    vector_service = staticmethod(vector_service)
    placeholder = staticmethod(placeholder)

    def query(self, data: Data, *, request_id: int = 31,
              timeout: Optional[float] = 1.0, **fields: Field) -> Optional[ProtocolEvent]:
        """Probe a format/medium/gamut with sample data, without storing an image."""
        request_id = integer(request_id, 'request_id', 1, 0xffffffff)
        sequence = ''.join(transmit(data, a='q', i=request_id, q=0, **fields))
        return self._request(sequence, request_id, timeout)

    def _request(self, sequence: str, request_id: int,
                 timeout: Optional[float]) -> Optional[ProtocolEvent]:
        pattern = re.compile(r'\x1b_G(?=(?:[^;\x1b]*,)?i=' + str(request_id)
                             + r'(?:,|;))[^;\x1b]*;[^\x1b]*\x1b\\')
        match = self.terminal._query_with_boundary(sequence, pattern, timeout)
        return parse_event(match.group()) if match else None

    def supports(self, format: int = 24, *, timeout: Optional[float] = 1.0,
                 **fields: Field) -> bool:
        """
        Check a raster or vector format, including local rendering where available.

        :raises ValueError: If arguments or protocol data are invalid.
        """
        # pylint: disable=redefined-builtin
        if format not in _FORMAT_SAMPLES:
            raise ValueError('Use query() with a sample for this format')
        if not self.terminal.does_styling:
            return False
        if fields:
            return self._supports_native(format, timeout, **fields)
        if self._compatibility.native_format(format, timeout):
            return True
        kitty = self._compatibility.has_native(timeout)
        if not kitty and not self._compatibility.has_sixel(timeout):
            return False
        return format in {200, 201} or format in FALLBACK_FORMATS

    def _supports_native(self, pixel_format: int, timeout: Optional[float],
                         **fields: Field) -> bool:
        if pixel_format not in _FORMAT_SAMPLES or self.terminal._is_apple_terminal(timeout):
            return False
        size = {} if pixel_format == 201 else {'s': 1, 'v': 1}
        event = self.query(_FORMAT_SAMPLES[pixel_format], f=pixel_format,
                           timeout=timeout, **size, **fields)
        return event is not None and event.payload == 'OK'

    def service(self, operation: str, *, request_id: int = 31,
                timeout: Optional[float] = 1.0, **fields: Any) -> Optional[Mapping[str, Any]]:
        """Run a vector font/measurement request and decode its result or terminal error."""
        sequence = ''.join(vector_service(request_id, operation, **fields))
        event = self._request(sequence, request_id, timeout)
        return service_result(event.raw) if event else None

    def upload_font(self, font_id: str, data: bytes, *, index: int = 0,
                    timeout: Optional[float] = 1.0) -> Optional[Mapping[str, Any]]:
        """
        Upload an outline font for this PTY; retained text survives release of the handle.

        :raises ValueError: If arguments or protocol data are invalid.
        """
        if not font_id:
            raise ValueError('font_id must not be empty')
        return self.service('upload', id=font_id, data=b64(data),
                            index=integer(index, 'index'), timeout=timeout)

    def measure_text(self, layout: Mapping[str, Any], *, timeout: Optional[float] = 1.0
                     ) -> Optional[Mapping[str, Any]]:
        """Measure vector text with exactly the layout object used by text_record()."""
        return self.service('measure', timeout=timeout, **layout)

    def gpu_surface(self, descriptor: str, *, image_id: int,
                    **fields: Field) -> Iterator[str]:
        """
        Reference an existing IOSurface/dma-buf; allocation remains with the caller.

        :raises ValueError: If arguments or protocol data are invalid.
        """
        if not descriptor.startswith(('iosurface:', 'dmabuf:')):
            raise ValueError('Expected an IOSurface or dma-buf descriptor')
        yield from transmit(descriptor.encode('ascii'), a='T', t='g',
                            i=integer(image_id, 'image_id', 1, 0xffffffff), **fields)
