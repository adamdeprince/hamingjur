"""World coordinates, sprite sheets, and declarative game frames."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite
from typing import Callable, Hashable, Iterable

from PIL.Image import Image

from ..ui import Rect

Point = tuple[float, float]


@dataclass(frozen=True)
class Camera:
    """World rectangle mapped to a viewport, in logical terminal-cell units."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        if (not all(map(isfinite, (self.x, self.y, self.width, self.height)))
                or min(self.width, self.height) <= 0):
            raise ValueError('Camera requires finite coordinates and positive dimensions')

    @classmethod
    def fit(cls, bounds: Rect) -> Camera:
        """Show the entire supplied world rectangle."""
        return cls(bounds.x, bounds.y, bounds.width, bounds.height)

    def follow(self, target: Point, *, bounds: Rect) -> Camera:
        """Center on a target, clamping the view inside the supplied world."""
        w, h = min(self.width, bounds.width), min(self.height, bounds.height)
        return Camera(max(bounds.x, min(bounds.right - w, target[0] - w / 2)),
                      max(bounds.y, min(bounds.bottom - h, target[1] - h / 2)), w, h)

    def transform(self, viewport: Rect) -> tuple[float, float, float]:
        """Return the origin and uniform world-to-cell scale."""
        scale = min(viewport.width / self.width, viewport.height / self.height)
        return (viewport.x + (viewport.width - self.width * scale) / 2 - self.x * scale,
                viewport.y + (viewport.height - self.height * scale) / 2 - self.y * scale,
                scale)

    def project(self, point: Point, viewport: Rect) -> Point:
        """Map a world point to absolute screen cells, retaining its fractional offset."""
        x, y, scale = self.transform(viewport)
        return x + point[0] * scale, y + point[1] * scale

    def unproject(self, point: Point, viewport: Rect) -> Point:
        """Convert a screen cell position back to world coordinates."""
        x, y, scale = self.transform(viewport)
        if not scale:
            raise ValueError('Cannot pick inside an empty viewport')
        return (point[0] - x) / scale, (point[1] - y) / scale


@dataclass(frozen=True, eq=False)
class Atlas:
    """A Pillow sheet of equal-sized frames in row-major order. Treat pixels as immutable."""

    image: Image
    width: int
    height: int

    def __post_init__(self):
        if (min(self.width, self.height, *self.image.size) <= 0
                or self.image.width % self.width or self.image.height % self.height):
            raise ValueError('Atlas dimensions must be multiples of its frame size')

    def origin(self, index: int) -> tuple[int, int]:
        cols = self.image.width // self.width
        if not 0 <= index < cols * (self.image.height // self.height):
            raise ValueError('Atlas frame outside sheet')
        return index % cols, index // cols


@dataclass(frozen=True)
class Sprite:
    key: Hashable
    at: Point
    size: Point
    glyph: str
    atlas: Atlas | None
    index: int
    layer: int
    sort_y: float


class Frame:
    """One desired scene. Sprite positions are top-left points in world units."""

    def __init__(self, camera: Camera):
        self.camera = camera
        self.sprites: dict[Hashable, Sprite] = {}
        self.labels: list[tuple[Point, str, str]] = []

    def sprite(self, key: Hashable, at: Point, *, atlas: Atlas | None = None,
               index: int = 0, size: Point = (1, 1), glyph: str = '*',
               layer: int = 0, sort_y: float | None = None) -> None:
        """Place an atlas frame or text glyph under a unique key for this draw."""
        if key in self.sprites:
            raise ValueError(f'Duplicate sprite key {key!r}')
        if min(size) <= 0 or not all(map(isfinite, (*at, *size))):
            raise ValueError('Sprite coordinates must be finite and size positive')
        if atlas is not None:
            atlas.origin(index)
        self.sprites[key] = Sprite(key, at, size, glyph, atlas, index, layer,
                                   at[1] + size[1] if sort_y is None else sort_y)

    def label(self, at: Point, text: str, role: str = 'text') -> None:
        """Draw a themed text label above the scene at a world point."""
        self.labels.append((at, text, role))

    def tiles(self, key: Hashable, visible: Callable[[Rect], Iterable[tuple[int, int, int]]],
              *, atlas: Atlas, glyph: str | Callable[[int], str] = '.', layer: int = -1) -> None:
        """Request only the visible world tiles from a caller-owned map or chunk cache."""
        cam = self.camera
        x, y = floor(cam.x), floor(cam.y)
        area = Rect(x, y, ceil(cam.x + cam.width) - x, ceil(cam.y + cam.height) - y)
        for col, row, index in visible(area):
            self.sprite((key, col, row), (col, row), atlas=atlas, index=index,
                        glyph=glyph(index) if callable(glyph) else glyph, layer=layer)
