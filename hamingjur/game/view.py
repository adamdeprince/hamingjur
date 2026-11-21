"""Widget and full-screen hosts for terminal games."""
from __future__ import annotations

from math import ceil, floor, isfinite
from typing import Hashable

from ..keyboard import Keystroke
from ..silkie import TabletMode
from ..ui import Application, Rect, Widget, Window, key_name
from ..window import Size
from .render import Renderer
from .scene import Camera, Frame, Point
from .session import GameSession


class GameView(Widget):
    """Run a session while focused. Menus, dialogs and focus loss pause its clock."""

    focusable = True
    track_terminal_focus = True

    def __init__(self, session: GameSession, *, camera: Camera | None = None,
                 frame_hz: float | None = None, **options):
        if frame_hz is not None and (not isfinite(frame_hz) or frame_hz <= 0):
            raise ValueError('frame_hz must be finite and positive')
        options.setdefault('grow', 1)
        super().__init__(**options)
        self.session = session
        self.camera = camera
        self.frame_hz = frame_hz
        self._frame_number = 0
        self._cached_frame: Frame | None = None
        self._frame_key: tuple[int, Camera | None, Rect] | None = None
        self.alpha = 0.0
        self._start: float | None = None
        self._base_tick = 0
        self._camera = camera
        self._paths: dict[Hashable, tuple[str, list[Point]]] = {}
        self._renderer: Renderer | None = None
        self._native: bool | None = None
        self._cell: Size | None = None
        self._clip = self.bounds
        self._release_events = False

    @property
    def strokes(self):
        """World-space paths currently being drawn, for live previews."""
        return tuple(tuple(points) for _, points in self._paths.values())

    def input_modes(self, terminal, stack):
        if terminal.get_kitty_keyboard_state() is not None:
            stack.enter_context(terminal.enable_kitty_keyboard(report_events=True,
                                                               report_all_keys=True))
            self._release_events = True
            stack.callback(setattr, self, '_release_events', False)
        if self.session.bindings.strokes or self.session.bindings.clicks:
            caps = terminal.silkie.capabilities()
            if caps is not None and caps.supports('tablet-pointer'):
                try:
                    mode = TabletMode.TOOLS | TabletMode.TOUCH
                    stack.enter_context(terminal.tablet.enabled(mode))
                except RuntimeError:
                    pass
        self._start = None

    def on_blur(self):
        self._start = None
        self._frame_key = None
        self.session.clear_input()
        self._paths.clear()

    def on_frame(self, now):
        if self._start is None:
            self._start, self._base_tick = now, self.session.tick
        elapsed = (now - self._start) * self.session.tick_hz
        count = floor(elapsed)
        while self.session.tick < self._base_tick + count:
            self.session.step()
        self.alpha = elapsed - count
        delay = (count + 1) / self.session.tick_hz
        if self.frame_hz is not None:
            self._frame_number = floor((now - self._start) * self.frame_hz)
            delay = min(delay, (self._frame_number + 1) / self.frame_hz)
        return self._start + delay

    def on_key(self, key, app):
        if isinstance(key, Keystroke) and key.tablet is not None:
            event = key.tablet
            if event.operation != 'event' or event.integer('kind') not in (1, 2):
                return False
            contact = tuple(event.integer(k) for k in ('kind', 'device', 'contact'))
            phase = {1: 'press', 2: 'move', 3: 'release', 4: 'cancel', 5: 'cancel'}.get(
                event.integer('phase') or 0)
            cell = self._cell or app.terminal.get_cell_size()
            point = event.position
            at = (point[0] / cell[0], point[1] / cell[1]) if point and cell else None
            return self._pointer(contact, 'LEFT', phase, at)
        name = key_name(key).removesuffix('_REPEATED').removesuffix('_RELEASED')
        if not name:
            name = getattr(key, 'key_value', key)
        kind = ('release' if getattr(key, 'released', False) else
                'repeat' if getattr(key, 'repeated', False) else 'press')
        return self.session.key(name, kind, releases=self._release_events)

    def on_pointer(self, pointer, app):
        at = (pointer.x + .5, pointer.y + .5)
        return self._pointer('mouse', pointer.button, pointer.kind, at)

    def _pointer(self, contact, button, kind, at):
        camera = self._camera
        if not camera or not self.bounds.width or not self.bounds.height:
            return False
        point = camera.unproject(at, self.bounds) if at is not None else None
        if kind in ('press', 'double'):
            if (point is None or not self._clip.contains(*at)
                    or not camera.x <= point[0] < camera.x + camera.width
                    or not camera.y <= point[1] < camera.y + camera.height):
                return False
            bindings = self.session.bindings
            if button in bindings.clicks:
                self.session.submit(bindings.clicks[button], point)
            if button in bindings.strokes:
                self._paths[contact] = bindings.strokes[button], [point]
            return button in bindings.clicks or button in bindings.strokes
        if contact not in self._paths:
            return False
        action, path = self._paths[contact]
        if point is not None and point != path[-1]:
            path.append(point)
        if kind == 'release':
            self.session.submit(action, tuple(path))
        if kind in ('release', 'cancel'):
            del self._paths[contact]
        return True

    def draw(self, surface, focused=None):
        surface.fill()
        self._clip = surface.clip
        if not surface.width or not surface.height:
            return
        frame_key = self._frame_number, self.camera, surface.bounds
        frame = self._cached_frame
        if self.frame_hz is None or frame is None or frame_key != self._frame_key:
            frame = Frame(self.camera or Camera(0, 0, surface.width, surface.height))
            self.session.game.draw(frame, self.alpha)
            self._cached_frame, self._frame_key = frame, frame_key
        self._camera = cam = frame.camera
        bounds = surface.bounds
        left, top = cam.project((cam.x, cam.y), bounds)
        right, bottom = cam.project((cam.x + cam.width, cam.y + cam.height), bounds)
        # Only complete terminal cells inside the letterboxed field can carry text.
        field = Rect(ceil(left), ceil(top), max(0, floor(right) - ceil(left)),
                     max(0, floor(bottom) - ceil(top)))
        area = surface.sub(field)
        term = surface.terminal
        if self._renderer is None or self._renderer.term is not term:
            self._renderer = Renderer(term)
            self._native = None
        renderer = self._renderer
        renderer.commands.clear()
        if any(sprite.atlas for sprite in frame.sprites.values()):
            if self._native is None:
                self._native = (term.is_a_tty and not surface.appearance.ascii_only
                                and term.does_kitty_graphics())
            self._cell = term.get_cell_size() if self._native else None
        ordered = sorted(frame.sprites.values(), key=lambda s: (s.layer, s.sort_y))
        origin_x, origin_y, scale = cam.transform(bounds)
        if self._cell:
            cw, ch = self._cell
            pixel_field = Rect(round(left * cw), round(top * ch),
                               round(right * cw) - round(left * cw),
                               round(bottom * ch) - round(top * ch))
        for z, sprite in enumerate(ordered, 1):
            x, y = origin_x + sprite.at[0] * scale, origin_y + sprite.at[1] * scale
            if self._cell and sprite.atlas:
                box = Rect(round(x * cw), round(y * ch), max(1, round(sprite.size[0] * scale * cw)),
                           max(1, round(sprite.size[1] * scale * ch)))
                stamp = surface.graphic(renderer)
                renderer.commands.append((sprite, box, pixel_field, surface.clip,
                                          (cw, ch), stamp, z))
            else:
                area.write(floor(x) - field.x, floor(y) - field.y, sprite.glyph)
        for at, text, role in frame.labels:
            x, y = cam.project(at, bounds)
            area.write(floor(x) - field.x, floor(y) - field.y, text, role)


def run(session: GameSession, *, camera: Camera | None = None, frame_hz: float | None = None,
        terminal=None):
    """Run a borderless game with the same widget host. Escape exits by default."""
    app = Application(terminal, bars=False)
    view = GameView(session, camera=camera, frame_hz=frame_hz)
    window = Window('', view, framed=False, on_close=lambda _: app.stop())
    window.maximized = True
    app.add(window)
    app.bind('ESCAPE', app.stop)
    return app.run()
