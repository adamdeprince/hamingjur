"""Cross the shipping lanes. Run with python -m bin.game_crossing [--fullscreen]."""
from __future__ import annotations

import argparse

from PIL import Image, ImageDraw

from hamingjur.game import Atlas, Bindings, Camera, GameSession, GameView, run
from hamingjur.ui import Application, Label, ProgressBar, Rect, Stack, Window, message_box

BOARD = Rect(0, 0, 24, 10)


def make_sheet():
    image = Image.new('RGBA', (32, 16))
    pen = ImageDraw.Draw(image)
    pen.polygon([(8, 1), (14, 13), (8, 10), (2, 13)], fill='#ffe395')
    pen.rounded_rectangle((17, 4, 30, 12), radius=3, fill='#67d8dd')
    pen.rectangle((21, 5, 24, 8), fill='#16425b')
    return Atlas(image, 16, 16)


class Crossing:
    """Pilot a skiff across moving barges. Space spends a rechargeable boost."""

    def __init__(self):
        self.sheet = make_sheet()
        self.x, self.y = 12, 8
        self.time = 0
        self.boost = 60
        self.crossings = 0
        self.docks = 0

    @staticmethod
    def barges(tick):
        for lane in (2, 4, 6):
            for slot in range(3):
                speed = (1 if lane == 4 else -1) * (lane + 4) / 90
                x = (tick * speed + slot * 9 + lane) % 27 - 3
                yield (lane, slot), x, lane

    def step(self, tick, actions):
        for action in actions.events:
            if action.name == 'boost' and self.boost == 60:
                self.y = max(1, self.y - 2)
                self.boost = 0
            elif action.name == 'up':
                self.y = max(1, self.y - 1)
            elif action.name == 'down':
                self.y = min(8, self.y + 1)
            elif action.name == 'left':
                self.x = max(0, self.x - 1)
            elif action.name == 'right':
                self.x = min(23, self.x + 1)
        self.time = tick + 1
        if self.y == 1:
            self.crossings += 1
            self.y = 8
        elif any(y == self.y and x < self.x + 1 and self.x < x + 3
                 for _, x, y in self.barges(self.time)):
            self.docks += 1
            self.y = 8
        self.boost = min(60, self.boost + 1)

    def draw(self, frame, alpha):
        for key, x, y in self.barges(max(0, self.time - 1 + alpha)):
            frame.sprite(key, (x, y), atlas=self.sheet, index=1, size=(3, 1), glyph='===')
        frame.sprite('skiff', (self.x, self.y), atlas=self.sheet, glyph='^', layer=1)
        frame.label((0, 0), f'Crossings {self.crossings}  Hits {self.docks}')
        frame.label((0, 9), f'Boost {self.boost / 60:.0%}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fullscreen', action='store_true')
    args = parser.parse_args()
    game = Crossing()
    bindings = Bindings()
    for key in ('up', 'down', 'left', 'right'):
        bindings.press(key, key, repeat=True)
    bindings.press('boost', 'SPACE')
    session = GameSession(game, bindings)
    camera = Camera.fit(BOARD)
    if args.fullscreen:
        return run(session, camera=camera)

    class BoostBar(ProgressBar):
        def draw(self, surface, focused=None):
            self.value = game.boost / 60
            super().draw(surface, focused)

    app = Application()
    panel = Stack(GameView(session, camera=camera), BoostBar(),
                  Label('Arrows move   Space boosts   P pauses   Q quits'))
    app.add(Window('Shipping lanes', panel, Rect(2, 1, 72, 23),
                   on_close=lambda _: app.stop()))
    app.bind('P', lambda: message_box(app, 'Paused', 'Return to the shipping lanes.',
                                      choices=('Resume',)))
    app.bind('Q', app.stop)
    app.run()


if __name__ == '__main__':
    main()
