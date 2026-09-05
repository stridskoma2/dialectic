"""Controller-owned logical-turn deadlines and observable activity tracking."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from .process import cancel_and_wait
from .schemas import AgentRequest

IDLE_WATCHDOG_SECONDS = 90.0
MAXIMUM_TURN_SECONDS = 3_600.0
TURN_EXTENSION_SECONDS = 300.0
_STREAMING_RUNTIMES = frozenset({"codex", "grok-build"})
_T = TypeVar("_T")


class TurnDeadlineExpired(TimeoutError):
    """Raised when a logical turn reaches its idle or allotted deadline."""

    def __init__(self, reason: str, seconds: float) -> None:
        self.reason = reason
        self.seconds = seconds
        if reason == "idle":
            detail = (
                "agent turn emitted no observable provider activity for "
                f"{math.ceil(seconds)} seconds"
            )
        else:
            detail = f"agent turn reached its allotted {math.ceil(seconds)}-second deadline"
        super().__init__(detail)


@dataclass(slots=True)
class _ActiveTurn:
    key: str
    role: str
    target_id: str
    phase: str
    runtime: str
    started_at: datetime
    allotted_seconds: float
    deadline_monotonic: float
    maximum_deadline_monotonic: float
    idle_seconds: float | None
    last_activity_monotonic: float
    wakeup: asyncio.Event
    loop: asyncio.AbstractEventLoop
    invocation: asyncio.Future[object] | None = None
    stopping: bool = False
    wakeup_pending: bool = False


class TurnDeadlineController:
    """Own dynamic turn deadlines while provider processes remain untrusted."""

    def __init__(
        self,
        *,
        idle_seconds: float = IDLE_WATCHDOG_SECONDS,
        maximum_turn_seconds: float = MAXIMUM_TURN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_seconds <= 0 or maximum_turn_seconds <= 0:
            raise ValueError("turn deadline bounds must be positive")
        self._idle_seconds = idle_seconds
        self._maximum_turn_seconds = maximum_turn_seconds
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveTurn] = {}

    @staticmethod
    def key_for(request: AgentRequest) -> str:
        return f"{request.role}:{request.target_id}:{request.turn_phase}"

    def activity_callback(self, request: AgentRequest) -> Callable[[], None]:
        key = self.key_for(request)

        def record() -> None:
            with self._lock:
                turn = self._active.get(key)
                now = self._monotonic()
                if turn is not None and self._is_running(turn, now):
                    turn.last_activity_monotonic = now
                    self._notify_locked(turn)

        return record

    async def wait_for(
        self,
        request: AgentRequest,
        runtime: str,
        invocation: Awaitable[_T],
    ) -> _T:
        """Await one turn under an extendable allotment and safe idle watchdog."""

        key = self.key_for(request)
        now = self._monotonic()
        loop = asyncio.get_running_loop()
        allotted = min(float(request.timeout_seconds), self._maximum_turn_seconds)
        idle = self._idle_seconds if runtime in _STREAMING_RUNTIMES else None
        turn = _ActiveTurn(
            key=key,
            role=request.role,
            target_id=request.target_id,
            phase=request.turn_phase,
            runtime=runtime,
            started_at=datetime.now(UTC),
            allotted_seconds=allotted,
            deadline_monotonic=now + allotted,
            maximum_deadline_monotonic=now + self._maximum_turn_seconds,
            idle_seconds=idle,
            last_activity_monotonic=now,
            wakeup=asyncio.Event(),
            loop=loop,
        )
        with self._lock:
            if key in self._active:
                raise RuntimeError("duplicate logical turn deadline registration")
            self._active[key] = turn

        task = asyncio.ensure_future(invocation)
        with self._lock:
            turn.invocation = task
        wakeup_task = asyncio.create_task(turn.wakeup.wait())
        try:
            while True:
                reason, remaining = self._remaining(key)
                if remaining <= 0:
                    await cancel_and_wait((task,))
                    seconds = idle if reason == "idle" else turn.allotted_seconds
                    raise TurnDeadlineExpired(reason, float(seconds or 0))
                done, _ = await asyncio.wait(
                    {task, wakeup_task}, timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    return task.result()
                if wakeup_task in done:
                    turn.wakeup.clear()
                    wakeup_task = asyncio.create_task(turn.wakeup.wait())
        except BaseException:
            with self._lock:
                turn.stopping = True
            await cancel_and_wait((task,))
            raise
        finally:
            with self._lock:
                self._active.pop(key, None)
            if not wakeup_task.done():
                wakeup_task.cancel()
            await asyncio.gather(wakeup_task, return_exceptions=True)

    def extend_active(self, seconds: float = TURN_EXTENSION_SECONDS) -> dict[str, object]:
        """Extend every active turn, capped by the controller hard ceiling."""

        if seconds <= 0:
            raise ValueError("turn extension must be positive")
        extended = 0
        with self._lock:
            now = self._monotonic()
            for turn in self._active.values():
                if not self._can_extend(turn, now):
                    continue
                previous = turn.deadline_monotonic
                turn.deadline_monotonic = min(
                    turn.deadline_monotonic + seconds,
                    turn.maximum_deadline_monotonic,
                )
                if turn.deadline_monotonic > previous:
                    turn.allotted_seconds += turn.deadline_monotonic - previous
                    extended += 1
                    self._notify_locked(turn)
        snapshot = self.snapshot()
        snapshot["extendedTurns"] = extended
        return snapshot

    def snapshot(self) -> dict[str, object]:
        now = self._monotonic()
        wall_now = datetime.now(UTC)
        with self._lock:
            turns = tuple(self._active.values())
            items = [self._snapshot_turn(turn, now, wall_now) for turn in turns]
        items.sort(
            key=lambda item: (
                item["effectiveRemainingSeconds"],
                item["targetId"],
                item["phase"],
            )
        )
        return {
            "active": bool(items),
            "turnCount": len(items),
            "remainingSeconds": min(
                (int(item["remainingSeconds"]) for item in items), default=0
            ),
            "effectiveRemainingSeconds": min(
                (int(item["effectiveRemainingSeconds"]) for item in items),
                default=0,
            ),
            "canExtend": any(bool(item["canExtend"]) for item in items),
            "turns": items,
        }

    def _remaining(self, key: str) -> tuple[str, float]:
        now = self._monotonic()
        with self._lock:
            turn = self._active.get(key)
            if turn is None:
                return "allotted", 0.0
            reason, remaining = self._remaining_turn(turn, now)
            if remaining <= 0:
                turn.stopping = True
            return reason, remaining

    @staticmethod
    def _remaining_turn(turn: _ActiveTurn, now: float) -> tuple[str, float]:
        allotted = turn.deadline_monotonic - now
        if turn.idle_seconds is not None:
            idle = turn.last_activity_monotonic + turn.idle_seconds - now
            if idle <= allotted:
                return "idle", idle
        return "allotted", allotted

    @classmethod
    def _is_running(cls, turn: _ActiveTurn, now: float) -> bool:
        task = turn.invocation
        return (
            not turn.stopping
            and (task is None or not task.done())
            and not (isinstance(task, asyncio.Task) and task.cancelling())
            and cls._remaining_turn(turn, now)[1] > 0
        )

    @classmethod
    def _can_extend(cls, turn: _ActiveTurn, now: float) -> bool:
        return cls._is_running(turn, now) and turn.deadline_monotonic < turn.maximum_deadline_monotonic

    def _notify_locked(self, turn: _ActiveTurn) -> None:
        if turn.wakeup_pending or turn.wakeup.is_set():
            return
        turn.wakeup_pending = True
        turn.loop.call_soon_threadsafe(self._wake_turn, turn)

    def _wake_turn(self, turn: _ActiveTurn) -> None:
        with self._lock:
            turn.wakeup_pending = False
            if self._active.get(turn.key) is turn:
                turn.wakeup.set()

    @classmethod
    def _snapshot_turn(
        cls, turn: _ActiveTurn, now: float, wall_now: datetime
    ) -> dict[str, object]:
        remaining = max(0.0, turn.deadline_monotonic - now)
        maximum_remaining = max(0.0, turn.maximum_deadline_monotonic - now)
        idle_remaining = (
            None
            if turn.idle_seconds is None
            else max(0.0, turn.last_activity_monotonic + turn.idle_seconds - now)
        )
        return {
            "key": turn.key,
            "role": turn.role,
            "targetId": turn.target_id,
            "phase": turn.phase,
            "runtime": turn.runtime,
            "startedAt": turn.started_at.isoformat(),
            "deadlineAt": (wall_now + timedelta(seconds=remaining)).isoformat(),
            "remainingSeconds": math.ceil(remaining),
            "idleRemainingSeconds": (
                None if idle_remaining is None else math.ceil(idle_remaining)
            ),
            "effectiveRemainingSeconds": math.ceil(
                min(remaining, idle_remaining)
                if idle_remaining is not None
                else remaining
            ),
            "maximumRemainingSeconds": math.ceil(maximum_remaining),
            "canExtend": cls._can_extend(turn, now),
        }
