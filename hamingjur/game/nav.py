"""Deterministic four-neighbour navigation on caller-defined tile costs."""
from __future__ import annotations

from heapq import heappop, heappush
from math import isfinite
from typing import Callable, Iterable, Iterator, Optional

from ..ui import Rect

Tile = tuple[int, int]
Cost = Callable[[Tile, Tile], Optional[float]]


def _search(bounds, starts, cost, target=None, *, reverse=False):
    costs = {}
    parent = {}
    queue: list[tuple[float, Tile]] = []
    for tile in starts:
        if not bounds.contains(*tile):
            raise ValueError('Endpoint outside search bounds')
        if tile not in costs:
            costs[tile] = 0.0
            heappush(queue, (0.0, tile))
    while queue:
        spent, tile = heappop(queue)
        if spent != costs[tile]:
            continue
        if tile == target:
            break
        x, y = tile
        for other in ((x-1, y), (x, y-1), (x+1, y), (x, y+1)):
            if not bounds.contains(*other):
                continue
            weight = cost(other, tile) if reverse else cost(tile, other)
            if weight is None:
                continue
            if not isfinite(weight) or weight < 0:
                raise ValueError('Expected a finite nonnegative movement cost or None')
            total = spent + weight
            if not isfinite(total):
                raise OverflowError('Path cost exceeds float range')
            if total < costs.get(other, float('inf')):
                costs[other] = total
                if target is not None:
                    parent[other] = tile
                heappush(queue, (total, other))
    return costs, parent


def distance_field(bounds: Rect, goals: Iterable[Tile], cost: Cost) -> dict[Tile, float]:
    """Minimum cost from each reachable tile to any goal, including directed costs."""
    return _search(bounds, goals, cost, reverse=True)[0]


def find_path(bounds: Rect, start: Tile, goal: Tile, cost: Cost) -> list[Tile] | None:
    """Return a lowest-cost path including both endpoints, or None if unreachable."""
    if not bounds.contains(*goal):
        raise ValueError('Endpoint outside search bounds')
    costs, previous = _search(bounds, [start], cost, goal)
    if goal not in costs:
        return None
    result = [goal]
    while result[-1] != start:
        result.append(previous[result[-1]])
    result.reverse()
    return result


def line_tiles(start: Tile, end: Tile) -> Iterator[Tile]:
    """Yield cells touched by a center-to-center segment, including both corner neighbours."""
    x, y = start
    dx, dy = end[0] - x, end[1] - y
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    dx, dy = abs(dx), abs(dy)
    ix = iy = 0
    while True:
        yield x, y
        if (x, y) == end:
            return
        crossing = (2*ix + 1)*dy - (2*iy + 1)*dx
        if crossing == 0:
            yield x + sx, y
            yield x, y + sy
        if crossing <= 0:
            x += sx
            ix += 1
        if crossing >= 0:
            y += sy
            iy += 1


def line_clear(start: Tile, end: Tile, passable: Callable[[Tile], bool]) -> bool:
    """Test line of sight through cells using the caller's visibility rules."""
    return all(passable(tile) for tile in line_tiles(start, end))
