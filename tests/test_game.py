"""Game sessions, desktop routing and native atlas output."""
import io
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PIL import Image

from hamingjur.game import (Action, Actions, Atlas, Bindings, Camera, Frame, GameSession,
                            GameView, distance_field, find_path, line_clear, run)
from hamingjur.keyboard import Keystroke
from hamingjur.ui import Application, Dialog, Label, Pointer, Rect, Window
from hamingjur.window import Size
from .accessories import TestTerminal


class Probe:
    def __init__(self):
        self.log = []
        self.paint = lambda frame: None

    def step(self, tick, actions):
        self.log.append((tick, actions))

    def draw(self, frame, alpha):
        self.paint(frame)


@pytest.fixture
def term(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    terminal = TestTerminal(stream=io.StringIO(), kind='xterm-256color', force_styling=True)
    terminal._height_and_width = lambda: SimpleNamespace(ws_col=32, ws_row=16)
    return terminal


def test_input_and_replay():
    game, bindings = Probe(), Bindings()
    bindings.press('hop', 'UP', repeat=True)
    bindings.press('flap', 'SPACE')
    bindings.hold('run', 'r')
    bindings.remember('east', 'd')
    bindings.remember('west', 'a')
    session = GameSession(game, bindings, tick_hz=4)
    for _ in range(2049):
        session.key('UP', 'repeat')
    session.key('SPACE', 'repeat')
    session.key('r', releases=False)
    session.key('d')
    session.key('a')
    first = session.step()
    assert first.events == (Action('hop'),) * 2049
    assert first.held == {'run', 'west'}
    assert session.step().held == {'west'}
    session.key('r')
    for _ in range(8):
        assert session.step().held == {'run', 'west'}
    session.key('r', 'release')
    assert session.step().held == {'west'}
    session.clear_input()
    assert session.step() == Actions()
    copy = GameSession(Probe())
    copy.replay(actions for _, actions in game.log)
    assert copy.game.log == game.log


def test_ticks_focus_and_pointer(term, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('hamingjur.ui.desktop.monotonic', lambda: now[0])
    bindings = Bindings()
    bindings.stroke('fence')
    bindings.click('walk', 'RIGHT')
    bindings.hold('walk_right', 'RIGHT')
    session = GameSession(Probe(), bindings, tick_hz=4)
    camera = Camera.fit(Rect(0, 0, 8, 4))
    view = GameView(session, camera=camera)
    app = Application(term)
    app.add(Window('Field', view, Rect(1, 2, 18, 10)))
    app.draw()
    assert app.dispatch(Pointer(2, 3))
    assert app.dispatch(Pointer(3, 5, kind='move'))
    assert app.dispatch(Pointer(5, 6, kind='release'))
    assert app.dispatch(Pointer(2, 3, 'RIGHT'))
    now[0] += .375
    app.draw()
    path = ((.25, .25), (.75, 1.25), (1.75, 1.75))
    assert session.game.log == [(0, Actions((Action('fence', path), Action('walk', path[0]))))]
    assert view.alpha == .5
    app.dispatch(Pointer(2, 3))
    session.key('RIGHT')
    dialog = app.add(Dialog('Pause', Label('Wait')))
    assert not view.strokes
    now[0] += 500
    app.draw()
    assert session.tick == 1
    dialog.close()
    app.draw()
    now[0] += .25
    app.draw()
    assert session.game.log[-1] == (1, Actions())


def test_retained_sprite_and_partial_occlusion(term, monkeypatch):
    monkeypatch.setattr(term, '_is_a_tty', True)
    monkeypatch.setattr(term, 'does_kitty_graphics', lambda *args, **kw: True)
    monkeypatch.setattr(term, 'get_cell_size', lambda: Size(8, 16))
    sheet = Atlas(Image.new('RGBA', (4, 2), 'red'), 2, 2)
    game = Probe()
    index = [0]
    game.paint = lambda f: f.sprite('boat', (0, 0), atlas=sheet, index=index[0], size=(8, 4))
    app = Application(term)
    view = GameView(GameSession(game))
    field = app.add(Window('Field', view, Rect(1, 2, 20, 10)))
    app.draw()
    assert 'a=t' in term.stream.getvalue()
    offset = term.stream.tell()
    app.draw()
    assert term.stream.tell() == offset
    index[0] = 1
    app.draw()
    output = term.stream.getvalue()[offset:]
    assert 'a=p' in output and 'a=t' not in output and 'x=64' in output
    cover = app.add(Window('Cover', Label(''), Rect(6, 1, 14, 9)))
    app.draw()
    [(pid, placement)] = view._renderer.shown.values()
    assert placement.source == Rect(64, 0, 32, 64)
    cover.close()
    app.draw()
    assert view._renderer.shown['boat', 0][0] == pid
    assert view._renderer.shown['boat', 0][1].source.width == 64
    field.close()
    app.draw()
    assert not view._renderer.assets
    assert 'd=I' in term.stream.getvalue()


def test_fullscreen_ticks_without_input(term, monkeypatch):
    now = [0.0]
    monkeypatch.setattr('hamingjur.ui.desktop.monotonic', lambda: now[0])
    monkeypatch.setattr(term, '_is_a_tty', True)
    monkeypatch.setattr(term, 'get_kitty_keyboard_state', lambda: None)
    for mode in ('fullscreen', 'cbreak', 'keypad', 'hidden_cursor', 'mouse_enabled',
                 'bracketed_paste', 'notify_on_resize', 'focus_events'):
        monkeypatch.setattr(term, mode, lambda **kw: nullcontext())
    monkeypatch.setattr(Application, '_watch_display', lambda self, stack: None)
    game = Probe()
    frames = []
    game.paint = lambda f: frames.append(f.camera)

    def read(timeout):
        assert 0 < timeout and now[0] < 1, 'host missed a deadline or ignored Escape'
        now[0] += timeout
        return Keystroke('') if now[0] < 1 else Keystroke('\x1b', name='KEY_ESCAPE')
    monkeypatch.setattr(term, 'inkey', read)
    run(GameSession(game, tick_hz=4), terminal=term)
    assert [tick for tick, _ in game.log] == [0, 1, 2]
    assert set(frames) == {Camera(0, 0, 32, 16)}


def test_camera_tile_source_and_navigation():
    camera = Camera(0, 0, 5, 3).follow((20, 20), bounds=Rect(0, 0, 10, 6))
    view = Rect(1, 2, 80, 24)
    assert camera.unproject(camera.project((7.5, 4), view), view) == (7.5, 4)
    atlas = Atlas(Image.new('RGBA', (2, 1)), 1, 1)
    frame = Frame(camera)

    def load(area):
        assert area == Rect(5, 3, 5, 3)
        yield 5, 3, 1
    frame.tiles('earth', load, atlas=atlas)
    assert frame.sprites['earth', 5, 3].index == 1
    area = Rect(0, 0, 4, 2)

    def cost(src, dst):
        return None if dst == (1, 0) or dst[0] < src[0] else 1

    path = find_path(area, (0, 0), (3, 0), cost)
    assert len(path) == 6
    assert distance_field(area, [(3, 0)], cost)[0, 0] == 5
    assert find_path(area, (3, 0), (0, 0), cost) is None
    assert not line_clear((0, 0), (1, 1), lambda p: p != (1, 0))
    assert line_clear((1, 3), (1, 0), lambda _: True)


def test_independent_tick_and_presentation_rates(term, monkeypatch):
    now = [0.0]
    monkeypatch.setattr('hamingjur.ui.desktop.monotonic', lambda: now[0])
    frames = []
    recorded = []
    game = Probe()
    game.paint = lambda f: frames.append(f.camera)
    view = GameView(GameSession(game, tick_hz=8, on_tick=lambda t, a: recorded.append((t, a))),
                    frame_hz=2)
    app = Application(term)
    app.add(Window('Clock', view))
    for i in range(9):
        now[0] = i / 8
        app.draw()
    assert len(frames) == 3
    assert len(game.log) == 8
    assert recorded == game.log
    app.dispatch(Keystroke('', name='FOCUS_OUT'))
    assert not app.dispatch('x')
    now[0] = 300
    app.draw()
    app.dispatch(Keystroke('', name='FOCUS_IN'))
    app.draw()
    assert len(game.log) == 8


def test_examples_headless():
    from bin.game_crossing import Crossing
    from bin.game_trails import Trails

    garden = Trails()
    session = GameSession(garden)
    session.submit('trail', ((9.5, 4.5), (9.5, 7.5)))
    session.submit('trail', ((13.5, 6.5),))
    for _ in range(80):
        session.step()
        assert garden.at not in garden.hedges
    assert garden.hedges == {(9, y) for y in range(4, 8)}
    assert garden.at == (13, 6)
    scene = Frame(Camera(0, 0, 24, 12))
    garden.draw(scene, 0)
    assert scene.sprites['walker'].at == garden.at
    assert scene.sprites['garden', 9, 6].glyph == '#'

    crossing = Crossing()
    crossing.x = 7
    game = GameSession(crossing)
    game.submit('boost')
    game.step()
    assert crossing.y == 6 and crossing.boost < 60
    game.replay(Actions() for _ in range(60))
    assert crossing.boost == 60


def test_resize_and_dismiss_reclaims_native_resources(term, monkeypatch):
    from hamingjur.protocols import parse_event

    monkeypatch.setattr(term, '_is_a_tty', True)
    monkeypatch.setattr(term, 'does_kitty_graphics', lambda *args: True)
    cell = [Size(8, 16)]
    monkeypatch.setattr(term, 'get_cell_size', lambda: cell[0])
    sheet = Atlas(Image.new('RGBA', (2, 2)), 2, 2)
    game = Probe()
    game.paint = lambda f: f.sprite('bird', (.5, .5), atlas=sheet)
    view = GameView(GameSession(game))
    app = Application(term)
    app.add(Window('Sprites', view))
    for zoom in range(100):
        cell[0] = Size(8 + zoom % 3, 16 + zoom % 5)
        app.draw()
        assert len(view._renderer.assets) == len(view._renderer.shown) == 1
    image_id, = view._renderer.assets.values()
    error = Keystroke('')
    error._protocol_event = parse_event(f'\x1b_Gi={image_id};ENOMEM: quota exceeded\x1b\\')
    with pytest.raises(RuntimeError, match='quota exceeded'):
        app.dispatch(error)
    assert not view._renderer.assets
    app.draw()
    assert len(view._renderer.shown) == 1
    app.release_images()
    assert not view._renderer.shown


def test_tablet_modes_and_simultaneous_world_strokes(term, monkeypatch):
    from contextlib import ExitStack
    from hamingjur import silkie
    from hamingjur.protocols import parse_event

    bindings = Bindings()
    bindings.stroke('paint')
    session = GameSession(Probe(), bindings)
    view = GameView(session)
    app = Application(term)
    app.add(Window('Pad', view, Rect(1, 2, 16, 6)))
    app.draw()
    modes = []
    monkeypatch.setattr(term, 'get_cell_size', lambda: Size(8, 16))
    monkeypatch.setattr(term, 'get_kitty_keyboard_state', lambda: None)
    caps = SimpleNamespace(supports=lambda selector: selector == 'tablet-pointer')
    monkeypatch.setattr(term.silkie, 'capabilities', lambda: caps)
    monkeypatch.setattr(term.tablet, 'enabled', lambda mode: nullcontext(modes.append(mode)))
    with ExitStack() as stack:
        view.input_modes(term, stack)
        assert modes == [silkie.TabletMode.TOOLS | silkie.TabletMode.TOUCH]

        def touch(contact, phase, col):
            key = Keystroke('')
            key._protocol_event = parse_event(silkie.command(
                'tablet-pointer', 'event', kind=2, src=0, device=1, contact=contact,
                phase=phase, x=col * 8 * 256, y=4 * 16 * 256))
            assert app.dispatch(key)

        touch(1, 1, 3)
        touch(2, 1, 4)
        touch(1, 2, 5)
        touch(2, 4, 4)
        touch(1, 3, 6)
        assert session.step().events == (Action('paint', ((1, 1), (3, 1), (4, 1))),)
