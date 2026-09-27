"""Per-game synchronization states and the result objects the orchestrator returns."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GameState(Enum):
    SYNCED = "synced"
    LOCAL_NEWER = "local_newer"
    USB_NEWER = "usb_newer"
    CONFLICT = "conflict"
    MISSING_LOCAL_PATH = "missing_local_path"
    RUNNING = "running"
    ERROR = "error"
    UNKNOWN = "unknown"
    FIRST_SYNC = "first_sync"

    @classmethod
    def parse(cls, value) -> "GameState":
        """Registry values come from a JSON file; anything unreadable is UNKNOWN,
        never SYNCED."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


# States that need the user's attention. UNKNOWN is here on purpose: an unknown
# result is not a success (plan §2.3).
ATTENTION_STATES = frozenset({
    GameState.CONFLICT,
    GameState.MISSING_LOCAL_PATH,
    GameState.RUNNING,
    GameState.ERROR,
    GameState.UNKNOWN,
    GameState.FIRST_SYNC,
})

# States in which a synchronization would move data (the "pending changes" tray colour).
PENDING_STATES = frozenset({GameState.LOCAL_NEWER, GameState.USB_NEWER})


class Outcome(Enum):
    """What the orchestrator actually did with a game during one cycle."""
    NOTHING = "nothing"          # already in sync, or only analyzed
    BACKED_UP = "backed_up"      # PC → USB
    RESTORED = "restored"        # USB → PC
    BASELINE = "baseline"        # identical on both sides; baseline recorded
    SKIPPED = "skipped"          # left untouched on purpose (conflict, running, ...)
    FAILED = "failed"            # an operation was attempted and did not validate
    ROLLED_BACK = "rolled_back"  # restore failed, previous PC state recovered


@dataclass
class GameResult:
    key: str
    title: str
    state: GameState
    outcome: Outcome = Outcome.NOTHING
    message: dict | None = None      # messages.msg() dict explaining the result
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.NOTHING, Outcome.BACKED_UP,
                                Outcome.RESTORED, Outcome.BASELINE) \
            and self.state not in ATTENTION_STATES


@dataclass
class SyncReport:
    """Everything one synchronization cycle produced, for the UI and notifications."""
    games: list[GameResult] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)   # cycle-level problems
    started: float = 0.0
    finished: float = 0.0
    completed: bool = False     # False = stopped early (USB removed, deadline, error)
    analyzed_only: bool = False
    timings: dict = field(default_factory=dict)

    def by_state(self, state: GameState) -> list[GameResult]:
        return [g for g in self.games if g.state == state]

    def count(self, *outcomes: Outcome) -> int:
        return sum(1 for g in self.games if g.outcome in outcomes)

    @property
    def attention(self) -> list[GameResult]:
        return [g for g in self.games
                if g.state in ATTENTION_STATES or g.outcome in (Outcome.FAILED,
                                                                Outcome.ROLLED_BACK)]

    @property
    def synchronized(self) -> list[GameResult]:
        return [g for g in self.games if g.ok]

    @property
    def success(self) -> bool:
        """True only when the cycle completed and nothing needs attention."""
        return self.completed and not self.errors and not self.attention
