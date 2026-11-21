"""Tap to walk, drag to plant hedges. Run with python -m bin.game_trails."""
from collections import deque
from math import floor

from PIL import Image, ImageDraw

from hamingjur.game import Atlas, Bindings, Camera, GameSession, find_path, line_tiles, run
from hamingjur.ui import Rect

GARDEN = Rect(0, 0, 80, 40)


class Trails:
    def __init__(self):
        art = Image.new('RGB', (12, 4), '#354432')
        paint = ImageDraw.Draw(art)
        paint.rectangle((4, 0, 7, 3), fill='#748853')
        paint.ellipse((8, 0, 11, 3), fill='#f2ba73')
        self.sheet = Atlas(art, 4, 4)
        self.at = (6, 6)
        self.hedges = set()
        self.route = deque()
        self.camera = Camera(0, 0, 24, 12)

    def visible(self, area):
        for row in range(area.y, area.bottom):
            for col in range(area.x, area.right):
                yield col, row, int((col, row) in self.hedges)

    def step(self, tick, actions):
        for event in actions.events:
            path = [(floor(x), floor(y)) for x, y in event.value]
            if len(set(path)) == 1 and GARDEN.contains(*path[0]):
                route = find_path(GARDEN, self.at, path[0],
                                  lambda _, end: None if end in self.hedges else 1)
                self.route = deque(route[1:] if route else ())
            else:
                self.route.clear()
                for start, end in zip(path, path[1:]):
                    for tile in line_tiles(start, end):
                        if GARDEN.contains(*tile) and tile != self.at:
                            self.hedges.add(tile)
        if tick % 5 == 0 and self.route:
            self.at = self.route.popleft()

    def draw(self, frame, alpha):
        frame.camera = self.camera.follow(self.at, bounds=GARDEN)
        # Keep the same two terrain frames and world tile keys as the camera scrolls.
        frame.tiles('garden', self.visible, atlas=self.sheet,
                    glyph=lambda index: '#' if index else '.')
        frame.sprite('walker', self.at, atlas=self.sheet, index=2, glyph='@')


def main():
    bindings = Bindings()
    bindings.stroke('trail')
    run(GameSession(Trails(), bindings))


if __name__ == '__main__':
    main()
