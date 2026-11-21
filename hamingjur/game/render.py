"""Retained Kitty atlas placements clipped against the desktop's final paint order."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from typing import Hashable

from PIL import Image

from ..ui.layout import Canvas, GraphicLayer, Rect
from .scene import Atlas, Sprite


def fragments(box, clip, cell, cover, stamp):
    """Merge contiguous visible cell runs into pixel rectangles."""
    if not box.width or not box.height:
        return
    w, h = cell
    cells = Rect(box.x // w, box.y // h, (box.right - 1) // w - box.x // w + 1,
                 (box.bottom - 1) // h - box.y // h + 1).intersection(clip)
    pending: dict[tuple[int, int], int] = {}
    for y in range(cells.y, cells.bottom):
        visible = (x for x in range(cells.x, cells.right) if cover.get((x, y), 0) < stamp)
        current = {}
        for _, run in groupby(enumerate(visible), lambda pair: pair[1] - pair[0]):
            xs = [x for _, x in run]
            span = xs[0], xs[-1] + 1
            current[span] = pending.pop(span, y)
        for (left, right), top in pending.items():
            yield Rect(left*w, top*h, (right-left)*w, (y-top)*h).intersection(box)
        pending = current
    for (left, right), top in pending.items():
        yield Rect(left*w, top*h, (right-left)*w, (cells.bottom-top)*h).intersection(box)


@dataclass(frozen=True)
class Placement:
    asset: tuple[Atlas, int, int]
    source: Rect
    x: int
    y: int
    z: int
    cell: tuple[int, int]


class Renderer(GraphicLayer):
    def __init__(self, term) -> None:
        self.term = term
        self.commands: list[tuple[Sprite, Rect, Rect, Rect, tuple[int, int], int, int]] = []
        self.assets: dict[tuple[Atlas, int, int], int] = {}
        self.shown: dict[tuple[Hashable, int], tuple[int, Placement]] = {}

    def present(self, canvas: Canvas) -> None:
        desired = {}
        for sprite, box, field, clip, cell, stamp, z in self.commands:
            atlas = sprite.atlas
            assert atlas is not None
            col, row = atlas.origin(sprite.index)
            visible = fragments(box.intersection(field), clip, cell, canvas.cover, stamp)
            for part, rect in enumerate(visible):
                source = Rect(col * box.width + rect.x - box.x,
                              row * box.height + rect.y - box.y, rect.width, rect.height)
                asset = atlas, box.width, box.height
                desired[sprite.key, part] = Placement(asset, source, rect.x, rect.y, z, cell)
        gfx = self.term.graphics
        for key in list(self.shown):
            pid, old = self.shown[key]
            if key not in desired or desired[key].asset != old.asset:
                gfx.send(gfx.delete(self.assets[old.asset], free=False, p=pid, q=2))
                del self.shown[key]
        used = {p.asset for p in desired.values()}
        for asset in self.assets.keys() - used:
            gfx.send(gfx.delete(self.assets[asset], q=2))
            del self.assets[asset]
        occupied = {pid for pid, _ in self.shown.values()}
        candidate = 1
        moves = []
        for key, place in desired.items():
            if key in self.shown and self.shown[key][1] == place:
                continue
            asset = place.asset
            if asset not in self.assets:
                atlas, width, height = asset
                size = (atlas.image.width // atlas.width * width,
                        atlas.image.height // atlas.height * height)
                pixels = atlas.image.convert('RGBA').resize(size, Image.Resampling.NEAREST)
                image_id = gfx._allocate_image_id(None)
                self.assets[asset] = image_id
                gfx.send(gfx.image(pixels.tobytes(), image_id=image_id, format=32,
                                   width=size[0], height=size[1], display=False,
                                   compress=True, q=1))
            if key in self.shown:
                pid = self.shown[key][0]
            else:
                while candidate in occupied:
                    candidate += 1
                pid = candidate
                candidate += 1
                if pid > 0xffffffff:  # Kitty placement IDs are unsigned 32-bit integers.
                    raise OverflowError('Kitty placement IDs exhausted')
            x, dx = divmod(place.x, place.cell[0])
            y, dy = divmod(place.y, place.cell[1])
            src = place.source
            moves.append(self.term.move_xy(x, y))
            moves.append(gfx.place(self.assets[asset], p=pid, x=src.x, y=src.y, w=src.width,
                                   h=src.height, X=dx, Y=dy, z=place.z, C=1, q=1))
            self.shown[key] = pid, place
        if moves:
            with self.term.location():
                gfx.send(moves)

    def graphics_error(self, image_id: int, reason: str) -> None:
        if image_id in self.assets.values():
            self.release()
            raise RuntimeError(f'Terminal rejected game image {image_id}: {reason}')

    def release(self) -> None:
        gfx = self.term.graphics
        for image_id in self.assets.values():
            gfx.send(gfx.delete(image_id, q=2))
        self.assets.clear()
        self.shown.clear()
        self.commands.clear()
