"""Fixed-step simulation and terminal-independent action input."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil, isfinite
from typing import Callable, Iterable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .scene import Frame


@dataclass(frozen=True)
class Action:
    """One game-defined command, optionally carrying a world position or path."""

    name: str
    value: object = None


@dataclass(frozen=True)
class Actions:
    """Ordered one-shot commands and the controls held during this tick."""

    events: tuple[Action, ...] = ()
    held: frozenset[str] = frozenset()


class Game(Protocol):
    """A simulation can also be stepped without creating a terminal."""

    def step(self, tick: int, actions: Actions) -> None: ...
    def draw(self, frame: Frame, alpha: float) -> None: ...


class Bindings:
    """Map terminal keys and pointer gestures to game-defined actions."""

    def __init__(self) -> None:
        self._groups: dict[str, str] = {}
        self.keys: dict[str, tuple[str, str, bool]] = {}
        self.clicks: dict[str, str] = {}
        self.strokes: dict[str, str] = {}

    def press(self, action: str, key: str, *, repeat: bool = False) -> None:
        """Queue each press, optionally accepting reported repeats."""
        self.keys[key.upper()] = (action, 'press', repeat)

    def hold(self, action: str, key: str) -> None:
        """Expose the action on every tick while its key is held."""
        self.keys[key.upper()] = (action, 'hold', True)

    def remember(self, action: str, key: str, *, group: str = 'direction') -> None:
        """Latch this action until another key in the same group replaces it."""
        self.keys[key.upper()] = (action, 'remember', True)
        self._groups[key.upper()] = group

    def click(self, action: str, button: str = 'LEFT') -> None:
        """Queue the world position of a pointer press."""
        self.clicks[button] = action

    def stroke(self, action: str, button: str = 'LEFT') -> None:
        """Queue the complete world-space path when the pointer is released."""
        self.strokes[button] = action


class GameSession:
    """Own one simulation, its tick counter, and its pending controls."""

    def __init__(self, game: Game, bindings: Bindings | None = None, *,
                 tick_hz: int = 60, legacy_hold_seconds: float = 0.25,
                 on_tick: Callable[[int, Actions], None] | None = None):
        if not isinstance(tick_hz, int) or tick_hz <= 0:
            raise ValueError('tick_hz must be a positive integer')
        if not isfinite(legacy_hold_seconds) or legacy_hold_seconds <= 0:
            raise ValueError('legacy_hold_seconds must be finite and positive')
        self.game = game
        self.bindings = bindings if bindings is not None else Bindings()
        self.tick_hz = tick_hz
        self.tick = 0
        self.on_tick = on_tick
        self._hold_ticks = ceil(tick_hz * legacy_hold_seconds)
        self._events: list[Action] = []
        self._held: dict[str, tuple[str, int | None]] = {}
        self._remembered: dict[str, str] = {}

    def submit(self, action: str, value: object = None) -> None:
        """Append an action for the next simulation tick."""
        self._events.append(Action(action, value))

    def key(self, key: str, kind: str = 'press', *, releases: bool = True) -> bool:
        """Accept a normalized press, repeat or release. Return whether bound."""
        if kind not in ('press', 'repeat', 'release'):
            raise ValueError('Unknown key event kind')
        key = key.upper()
        # Release reports carry the modifiers still down at release time.
        physical = key
        while physical.partition('_')[0] in ('CTRL', 'ALT', 'SHIFT', 'SUPER', 'HYPER', 'META'):
            _, separator, remainder = physical.partition('_')
            if not separator:
                break
            physical = remainder
        if kind == 'release' and physical in self._held:
            del self._held[physical]
            return True
        binding = self.bindings.keys.get(key)
        if binding is None:
            return False
        action, mode, repeat = binding
        if kind == 'release':
            return True
        if mode == 'hold':
            self._held[physical] = (action, None if releases else self.tick + self._hold_ticks)
        elif mode == 'remember':
            self._remembered[self.bindings._groups[key]] = action
        elif kind == 'press' or repeat:
            self.submit(action)
        return True

    def clear_input(self) -> None:
        """Cancel queued and held controls when input ownership changes."""
        self._events.clear()
        self._held.clear()
        self._remembered.clear()

    def step(self, actions: Actions | None = None) -> Actions:
        """Advance one tick. The returned input can be streamed to a recorder."""
        if actions is None:
            self._held = {k: v for k, v in self._held.items()
                          if v[1] is None or self.tick < v[1]}
            active = set(self._remembered.values())
            active.update(action for action, _ in self._held.values())
            actions = Actions(tuple(self._events), frozenset(active))
            self._events.clear()
        self.game.step(self.tick, actions)
        self.tick += 1
        if self.on_tick is not None:
            self.on_tick(self.tick - 1, actions)
        return actions

    def replay(self, stream: Iterable[Actions]) -> None:
        """Advance once per recorded tick from a lazily consumed input stream."""
        for actions in stream:
            self.step(actions)
