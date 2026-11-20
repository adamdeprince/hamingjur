"""Sixel raster encoding and the subset of Kitty placement that sixel can express."""
from __future__ import annotations

from collections import defaultdict
from itertools import groupby, islice
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from .protocols import ST

if TYPE_CHECKING:
    from .terminal import Terminal


RGB = tuple[int, int, int]
_PERCENT = tuple((channel * 100 + 127) // 255 for channel in range(256))


def _pixels(data: bytes) -> Iterator[RGB | None]:
    channels = iter(data)
    for red, green, blue, alpha in zip(channels, channels, channels, channels):
        yield (_PERCENT[red], _PERCENT[green], _PERCENT[blue]) if alpha >= 128 else None


def _reduce(pixel: RGB, levels: int) -> RGB:
    if not levels:
        return 0, 0, 0
    red, green, blue = (((value * levels + 50) // 100 * 100 + levels // 2) // levels
                        for value in pixel)
    return red, green, blue


def _palette(data: bytes, colors: int) -> tuple[dict[RGB, int], dict[RGB, int]]:
    """Keep exact sixel colors when possible and reduce RGB precision to fit the palette."""
    # Sixels encode channel intensities in integer percentages and have binary transparency.
    distinct = dict.fromkeys(pixel for pixel in _pixels(data) if pixel is not None)
    # Each pass reduces all three channel precisions, down to one color if necessary.
    for levels in (100, 63, 31, 15, 7, 3, 1, 0):
        mapped = {pixel: _reduce(pixel, levels) for pixel in distinct}
        palette = {pixel: index for index, pixel in enumerate(dict.fromkeys(mapped.values()))}
        if len(palette) <= colors:
            return palette, {pixel: palette[color] for pixel, color in mapped.items()}
    raise ValueError('Sixel requires at least one color register')


def _runs(columns: dict[int, int]) -> Iterator[str]:
    """Encode a color plane, skipping transparent gaps with sixel repeat counts."""
    column = 0
    for mask, run in groupby(sorted(columns), key=columns.__getitem__):
        start = previous = next(run)
        for following in run:
            if following != previous + 1:
                yield _repeat(start - column, '?')
                yield _repeat(previous + 1 - start, chr(63 + mask))
                column, start = previous + 1, following
            previous = following
        yield _repeat(start - column, '?')
        yield _repeat(previous + 1 - start, chr(63 + mask))
        column = previous + 1


def _repeat(count: int, value: str) -> str:
    return f'!{count}{value}' if count > 3 else value * count


def encode(image: Any, colors: int) -> Iterator[str]:
    """Stream sixel bands with a transparent background and square pixels."""
    if colors < 1:
        raise ValueError('Sixel requires at least one color register')
    width, height = image.size
    data = image.tobytes()
    palette, lookup = _palette(data, colors)
    indices = (lookup[color] if color is not None else -1 for color in _pixels(data))
    yield f'\x1bP0;1;0q"1;1;{width};{height}'
    for (red, green, blue), index in palette.items():
        yield f'#{index};2;{red};{green};{blue}'
    # One sixel character encodes a vertical band of six pixels by definition.
    for top in range(0, height, 6):
        planes: dict[int, dict[int, int]] = defaultdict(dict)
        for row in range(min(6, height - top)):
            for col, index in enumerate(islice(indices, width)):
                if index >= 0:
                    plane = planes[index]
                    plane[col] = plane.get(col, 0) | (1 << row)
        if top:
            yield '-'
        for offset, (index, columns) in enumerate(planes.items()):
            yield ('$' if offset else '') + f'#{index}' + ''.join(_runs(columns))
    yield ST


def placement(image: Any, fields: Mapping[str, str], terminal: Terminal,
              timeout: float | None) -> Any:
    """Crop, resize and offset an image using Kitty's physical placement fields."""
    from PIL import Image  # pylint: disable=import-outside-toplevel

    x, y, width, height, columns, rows, dx, dy = (
        int(fields.get(key, '0')) for key in ('x', 'y', 'w', 'h', 'c', 'r', 'X', 'Y'))
    if min(x, y, width, height, columns, rows, dx, dy) < 0:
        raise ValueError('Image placement dimensions must be nonnegative')
    right = min(x + width, image.width) if width else image.width
    bottom = min(y + height, image.height) if height else image.height
    if x >= right or y >= bottom:
        raise ValueError('Image placement crop is outside the source')
    if (x, y, right, bottom) != (0, 0, image.width, image.height):
        image = image.crop((x, y, right, bottom))
    if columns or rows:
        cell = terminal.get_cell_size(timeout)
        if cell is None:
            raise RuntimeError('Cell pixel dimensions are required for sixel image placement')
        max_width = columns * cell.width if columns else None
        max_height = rows * cell.height if rows else None
        if max_height is None or (max_width is not None
                                  and max_width * image.height <= max_height * image.width):
            assert max_width is not None
            size = (max_width, max(1, (max_width * image.height + image.width // 2) // image.width))
        else:
            scaled_width = (max_height * image.width + image.height // 2) // image.height
            size = (max(1, scaled_width), max_height)
        if size != image.size:
            image = image.resize(size, Image.Resampling.LANCZOS)
    if dx or dy:
        placed = Image.new('RGBA', (image.width + dx, image.height + dy))
        placed.paste(image, (dx, dy))
        image = placed
    return image
