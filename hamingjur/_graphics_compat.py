"""Connection-local translation between Silkie, Kitty and sixel graphics."""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from . import _sixel, _vector_raster
from .imaging import FALLBACK_FORMATS, decode_image
from .protocols import ProtocolEvent, integer, parse_event

if TYPE_CHECKING:
    from .graphics import Graphics

# Skip entire control strings, including DCS passthrough with doubled ESC bytes.
_CONTROL_STRING = re.compile(
    r'\x1b(?:(?:P|X|\^|_)(?:[^\x1b]|\x1b\x1b)*\x1b\\|\][^\x1b\x07]*(?:\x1b\\|\x07))')
KITTY_FORMATS = frozenset({24, 32, 100})
_KITTY_PLACEMENT = frozenset('ixywhXYcrCzpq')
_SIXEL_PLACEMENT = _KITTY_PLACEMENT - {'p', 'z'}
_UPLOAD = frozenset('aifsvoqtm')
_NATIVE_EXTENSIONS = frozenset('EGTHVPQ')


@dataclass
class _Transmission:
    """Accumulate one translated direct upload until its final packet arrives."""
    fields: Mapping[str, str]
    data: bytearray


@dataclass(frozen=True)
class _Scene:
    width: int
    height: int
    records: _vector_raster.Records


class GraphicsCompatibility:
    """Know which operations need translating and retain their source data."""

    def __init__(self, graphics: Graphics):
        self.graphics = graphics
        self.terminal = graphics.terminal
        self.native: bool | None = None
        self.sixel: bool | None = None
        self.native_formats: dict[int, bool] = {}
        self._pending: _Transmission | None = None
        self._native_pending = False
        self._rasters: dict[int, Any] = {}
        self._scenes: dict[int, _Scene] = {}
        self._displayed: set[int] = set()

    def has_native(self, timeout: float | None) -> bool:
        if self.native is None:
            self.native = self.terminal.does_kitty_graphics(timeout)
        return self.native

    def has_sixel(self, timeout: float | None) -> bool:
        if self.sixel is None:
            self.sixel = self.terminal.does_sixel(timeout)
        return self.sixel

    def native_format(self, pixel_format: int, timeout: float | None) -> bool:
        if pixel_format not in self.native_formats:
            supported = self.graphics._supports_native(pixel_format, timeout)
            self.native_formats[pixel_format] = supported
            if self.native_formats[pixel_format]:
                self.native = True
        return self.native_formats[pixel_format]

    def packets(self, sequence: str, timeout: float | None = 1.0) -> Iterator[str]:
        """Translate complete top-level graphics packets and preserve surrounding output."""
        offset = 0
        for match in _CONTROL_STRING.finditer(sequence):
            if not match.group().startswith('\x1b_G'):
                continue
            if match.start() != offset:
                yield sequence[offset:match.start()]
            yield from self._packet(parse_event(match.group()), timeout)
            offset = match.end()
        if offset != len(sequence):
            yield sequence[offset:]

    def _packet(self, event: ProtocolEvent, timeout: float | None) -> Iterator[str]:
        fields = event.fields
        more = fields.get('m') == '1'
        pending, self._pending = self._pending, None
        if pending is not None and fields.keys() <= {'m', 'q'}:
            # A fresh header can replace an upload abandoned by its producer.
            pending.data.extend(event.data)
        elif self._native_pending or self._pass_through(fields, timeout):
            self._native_pending = more
            yield event.raw
            self._forget_scene(fields)
            return
        else:
            pending = _Transmission(fields, bytearray(event.data))
        if more:
            self._pending = pending
        else:
            yield from self._translate(pending, timeout)

    def _pass_through(self, fields: Mapping[str, str], timeout: float | None) -> bool:
        if not self.has_native(timeout):
            return False
        action = fields.get('a', 't')
        if action == 'q':
            return True
        if fields.get('t') == 'g' or _NATIVE_EXTENSIONS.intersection(fields):
            caps = self.terminal.silkie.capabilities(timeout=timeout)
            if caps is None or (fields.get('t') == 'g' and 'g' not in caps.media):
                raise NotImplementedError('This graphics extension requires native Silkie')
        pixel_format = 200 if action in {'e', 'v'} else int(fields.get('f', '32'))
        if pixel_format in KITTY_FORMATS:
            return True
        return self.native_format(pixel_format, timeout)

    def _forget_scene(self, fields: Mapping[str, str]) -> None:
        """Discard local sources when their Kitty image data is replaced or freed."""
        action = fields.get('a', 't')
        identifier = fields.get('i', '')
        if action in {'t', 'T'} and identifier.isascii() and identifier.isdecimal():
            image_id = int(identifier)
            self.graphics._reserve_image_id(image_id)
            self._scenes.pop(image_id, None)
        elif action == 'd' and 'p' not in fields:
            if fields.get('d') == 'A':
                self._scenes.clear()
            elif fields.get('d') == 'I' and identifier.isascii() and identifier.isdecimal():
                self._scenes.pop(int(identifier), None)

    def _translate(self, upload: _Transmission, timeout: float | None) -> Iterator[str]:
        fields = upload.fields
        action = fields.get('a', 't')
        kitty = self.has_native(timeout)
        if not kitty and not self.has_sixel(timeout):
            raise NotImplementedError('No supported graphics backend is available')
        if not kitty and action == 'd':
            self._delete(fields)
            return
        if not kitty and action == 'p':
            _require_fields(fields, _SIXEL_PLACEMENT | {'a'})
            image_id = _image_id(fields, required=True)
            if image_id not in self._rasters:
                raise ValueError(f'No retained image with ID {image_id}')
            yield from self.show_sixel(self._rasters[image_id], fields, timeout)
            self._displayed.add(image_id)
            return
        if action not in {'t', 'T', 'e'} or fields.get('t', 'd') != 'd':
            raise NotImplementedError(
                f'Graphics action {action!r} or transport requires native support')
        pixel_format = 200 if action == 'e' else int(fields.get('f', '32'))
        if pixel_format not in {200, 201} and pixel_format not in FALLBACK_FORMATS:
            raise NotImplementedError(f'Image format {pixel_format} requires native support')
        placement = _KITTY_PLACEMENT if kitty else _SIXEL_PLACEMENT
        accepted = (_UPLOAD | placement) if action != 'e' else {'a', 'i', 'q', 'o', 'm'}
        _require_fields(fields, accepted)
        if fields.get('o') not in {None, 'z'}:
            raise NotImplementedError('This graphics compression requires native support')
        data = zlib.decompress(upload.data) if fields.get('o') == 'z' else bytes(upload.data)
        image_id = _image_id(fields, required=action == 'e' or (not kitty and action == 't'))
        width, height = int(fields.get('s', '0')), int(fields.get('v', '0'))
        scene = None
        if pixel_format == 200:
            scene, data = self._rasterize(data, action, image_id, width, height, kitty)
            pixel_format = 100
        elif pixel_format == 201:
            data = _vector_raster.rasterize_svg(data, width, height)
            pixel_format = 100
        if kitty:
            yield from self._send_kitty(data, fields, pixel_format)
        else:
            raster = decode_image(data, pixel_format, width, height)
            if action == 'T':
                yield from self.show_sixel(raster, fields, timeout)
                if image_id:
                    self._displayed.add(image_id)
            if image_id:
                self._rasters[image_id] = raster
        if scene is not None and image_id:
            self._scenes[image_id] = scene
        else:
            self._scenes.pop(image_id, None)
        self.graphics._reserve_image_id(image_id)

    def _rasterize(self, data: bytes, action: str, image_id: int, width: int, height: int,
                   kitty: bool) -> tuple[_Scene, bytes]:
        current = None
        if action == 'e':
            if not kitty and image_id in self._displayed:
                raise NotImplementedError('Sixel cannot update a displayed scene by image ID')
            if image_id not in self._scenes:
                raise ValueError(f'No retained vector scene with ID {image_id}')
            scene = self._scenes[image_id]
            width, height, current = scene.width, scene.height, scene.records
        integer(width, 'width', 1, 0xffffffff)
        integer(height, 'height', 1, 0xffffffff)
        records = _vector_raster.edit(data, current)
        return _Scene(width, height, records), _vector_raster.rasterize(records, width, height)

    @staticmethod
    def _send_kitty(data: bytes, fields: Mapping[str, str], pixel_format: int) -> Iterator[str]:
        from .graphics import transmit  # pylint: disable=import-outside-toplevel

        if pixel_format != 100:
            raster = decode_image(data, pixel_format, int(fields['s']), int(fields['v']))
            header = _wire_fields(fields, omit={'f', 'm', 'o', 't'})
            yield from transmit(zlib.compress(raster.tobytes(), level=1), compress=False,
                                o='z', f=32, **header)
        elif fields.get('a') == 'e':
            # Edit the root animation frame in place, retaining Kitty's placements.
            header = _wire_fields(fields, omit={'a', 'm', 'o'})
            yield from transmit(data, compress=False, a='f', f=100, r=1, X=1, **header)
        else:
            header = _wire_fields(fields, omit={'f', 'm', 'o', 't'})
            yield from transmit(data, compress=False, f=100, **header)

    def show_sixel(self, raster: Any, fields: Mapping[str, str],
                   timeout: float | None) -> Iterator[str]:
        """Render an image at the cursor with available sixel color registers."""
        colors = self.terminal.get_sixel_colors(timeout)
        raster = _sixel.placement(raster, fields, self.terminal, timeout)
        cursor = None
        if fields.get('C') == '1':
            # Leave the terminal's saved-cursor slot available to the application.
            cursor = self.terminal.get_location(timeout)
            if min(cursor) < 0:
                raise RuntimeError('Cursor coordinates are required for sixel C=1 placement')
        yield from _sixel.encode(raster, colors)
        if cursor is not None:
            yield self.terminal.move(*cursor)

    def store_sixel(self, image_id: int, raster: Any) -> None:
        self._scenes.pop(image_id, None)
        self._rasters[image_id] = raster
        self._displayed.add(image_id)

    def forget(self, image_id: int) -> None:
        """Drop locally retained state without touching the terminal."""
        self._rasters.pop(image_id, None)
        self._scenes.pop(image_id, None)
        self._displayed.discard(image_id)

    def _delete(self, fields: Mapping[str, str]) -> None:
        _require_fields(fields, {'a', 'd', 'i', 'q'})
        selector = fields.get('d', 'a')
        if selector.lower() != 'i':
            raise NotImplementedError('Sixel cannot delete graphics using this selector')
        image_id = _image_id(fields, required=True)
        if image_id in self._displayed or image_id not in self._rasters:
            raise NotImplementedError('Sixel cannot delete displayed pixels by image ID')
        if selector.isupper():
            self.forget(image_id)


def _wire_fields(fields: Mapping[str, str], omit: set[str]) -> dict[str, int | str]:
    return {key: int(value) if value.lstrip('-').isdigit() else value
            for key, value in fields.items() if key not in omit}


def _require_fields(fields: Mapping[str, str], accepted: frozenset[str] | set[str]) -> None:
    unsupported = fields.keys() - accepted
    if unsupported:
        raise NotImplementedError(f'Graphics fields require native support: {sorted(unsupported)}')


def _image_id(fields: Mapping[str, str], *, required: bool) -> int:
    return integer(int(fields.get('i', '0')), 'image_id', int(required), 0xffffffff)
