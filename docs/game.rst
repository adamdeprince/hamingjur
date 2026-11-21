Terminal games
==============

A game owns its world and implements two methods. A :class:`~hamingjur.game.GameSession`
delivers ordered actions to ``step(tick, actions)`` at a fixed rate. A
:class:`~hamingjur.game.GameView` calls ``draw(frame, alpha)`` to describe what
the player sees. Embed the view in a desktop window or pass the session to
``run()`` for fullscreen play. Escape exits the fullscreen host.

.. code-block:: python

    from hamingjur.game import Bindings, Camera, GameSession, GameView
    from hamingjur.ui import Application, Rect, Window

    class Walker:
        def __init__(self):
            self.x = 0

        def step(self, tick, actions):
            for action in actions.events:
                if action.name == 'right':
                    self.x = (self.x + 1) % 16

        def draw(self, frame, alpha):
            frame.sprite('player', (self.x, 2), glyph='@')

    bindings = Bindings()
    bindings.press('right', 'RIGHT', repeat=True)
    session = GameSession(Walker(), bindings)
    camera = Camera.fit(Rect(0, 0, 16, 5))

    app = Application()
    view = GameView(session, camera=camera)
    app.add(Window('Walk', view, on_close=lambda _: app.stop()))
    app.run()

From a checkout, run the shipping-lanes game or the scrolling garden.

.. code-block:: console

    $ python -m bin.game_crossing
    $ python -m bin.game_crossing --fullscreen
    $ python -m bin.game_trails

In Shipping lanes, cross the lanes with the arrows and press Space to boost.
In the desktop version P opens a pause dialog and Q quits. In the garden,
click or tap to walk and drag to plant a hedge. The camera follows the walker.
The garden accepts pen and touchscreen contacts when Silkie grants tablet input.

Ticks and input
---------------

Ticks start at zero. The default is 60 ticks per second. ``actions.events``
preserves press order and duplicates. ``bindings.hold('run', 'SHIFT_R')``
adds an action to ``actions.held`` until release. On keyboards that send only
presses, each press renews the hold for ``legacy_hold_seconds``. These
keyboards also report ordinary repeats as presses. Use ``bindings.remember()``
for a direction that stays active until replaced by another in its group.
Losing focus, opening a menu or switching into a dialog pauses the view and
clears its pending controls. Returning starts a new clock interval.

``bindings.click('walk')`` delivers an ``Action`` with a world-space point as
its value. ``bindings.stroke('fence')`` delivers a tuple of points on release.
During a drag, ``view.strokes`` exposes the active paths for previews.
A view converts positions using the camera from its last draw.
The game decides how to handle a captured stroke ending outside the board.

Keep randomness and gameplay updates in ``step``. Use the fractional tick
``alpha`` in ``draw`` to interpolate between previous and current positions.
``GameView(session, frame_hz=30)`` samples the scene at 30 Hz while the
session continues at its configured tick rate. Resizing also refreshes the scene.

For headless play, call ``session.submit(name, value)`` and ``session.step()``,
or supply an ``Actions`` value directly to ``step()``. Each call returns the
actions used. Pass ``on_tick=writer`` to the session to receive
``writer(tick, actions)`` after each completed tick, including ticks during
interactive play. Replay an iterable of those action batches using
``new_session.replay(batches)`` with the same initial world and seed.
The caller owns the recording format and can stream batches as they arrive.

Rendering a world
-----------------

A camera uses logical cell units. It scales uniformly in those units and centers
the board inside the widget. Assign ``frame.camera = camera.follow(point, bounds=world)``
before drawing a scrolling world. ``project`` and ``unproject`` use the same
viewport rectangle for drawing and picking.

Construct an ``Atlas(image, width, height)`` from a Pillow sheet with equal-sized
frames in row-major order. Keep the atlas across draws and use a new atlas
when its pixels change. Submit each visible object with a stable key.

.. code-block:: python

    frame.sprite('boat', (x, y), atlas=boats, index=2, size=(3, 1),
                 glyph='===', layer=1, sort_y=y + 1)
    frame.label((1, 0), f'Crossings {score}')

Sprites are ordered by layer, sort point and submission order. Positions
and sizes are world units. Labels are terminal text drawn above sprites.
Kitty-compatible terminals with cell pixel geometry receive one upload per
visible atlas and display size. Movement and animation update placements.
The compositor crops placements against the viewport and overlying widgets.
Other terminals display each sprite's glyph at its projected position.

``frame.tiles(key, visible, atlas=terrain)`` calls ``visible(world_rect)`` for
the current camera. Yield ``(column, row, atlas_index)`` for each desired tile.
A tile source can load its own chunks on demand. ``glyph`` accepts a string
or a function from atlas index to fallback text.

Navigation
----------

``find_path(bounds, start, goal, cost)`` finds a minimum-cost four-neighbour
route, including both endpoints. ``distance_field(bounds, goals, cost)``
computes the travel cost to the nearest reachable goal. Both accept directed
costs. Return a finite nonnegative number from ``cost(source, destination)``
or return ``None`` for a blocked edge. ``line_tiles(start, end)`` yields every
tile touched by a segment between tile centers. ``line_clear`` tests those
tiles against a visibility predicate.

API
---

.. automodule:: hamingjur.game
   :members:
   :imported-members:
