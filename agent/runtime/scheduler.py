"""Run budgets and bounded scheduling state for one Agent turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_args_hash(args: dict[str, Any]) -> str:
    payload = json.dumps(
        args or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RunBudget:
    max_iterations: int = 4
    max_steps: int = 12
    max_tool_calls: int = 8
    max_retries_per_tool: int = 1
    max_wall_time_sec: float = 180.0
    max_observation_chars: int = 12_000
    max_no_progress_rounds: int = 2

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "RunBudget":
        values = dict(raw or {})

        def _integer(name: str, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(values.get(name, default)))
            except (TypeError, ValueError):
                return default

        def _number(name: str, default: float, minimum: float = 0.1) -> float:
            try:
                return max(minimum, float(values.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            max_iterations=_integer("max_iterations", cls.max_iterations),
            max_steps=_integer("max_steps", cls.max_steps),
            max_tool_calls=_integer("max_tool_calls", cls.max_tool_calls),
            max_retries_per_tool=_integer(
                "max_retries_per_tool",
                cls.max_retries_per_tool,
                minimum=0,
            ),
            max_wall_time_sec=_number("max_wall_time_sec", cls.max_wall_time_sec),
            max_observation_chars=_integer(
                "max_observation_chars",
                cls.max_observation_chars,
                minimum=256,
            ),
            max_no_progress_rounds=_integer(
                "max_no_progress_rounds",
                cls.max_no_progress_rounds,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.max_iterations,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_retries_per_tool": self.max_retries_per_tool,
            "max_wall_time_sec": self.max_wall_time_sec,
            "max_observation_chars": self.max_observation_chars,
            "max_no_progress_rounds": self.max_no_progress_rounds,
        }


@dataclass
class ScheduledCall:
    call_id: str
    tool_id: str
    args_hash: str
    task_id: str
    timeout_sec: float | None
    writes_selection: bool
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    status: str = "running"
    observation_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "task_id": self.task_id,
            "tool_id": self.tool_id,
            "args_hash": self.args_hash,
            "timeout_sec": self.timeout_sec,
            "writes_selection": self.writes_selection,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
        }


class RunController:
    """Tracks hard budgets and repeated-call/no-progress signatures."""

    def __init__(self, budget: RunBudget, *, run_id: str | None = None) -> None:
        self.run_id = run_id or f"agent-{uuid.uuid4().hex[:12]}"
        self.budget = budget
        self.started_at = utc_now()
        self._started_monotonic = time.monotonic()
        self.calls: list[ScheduledCall] = []
        self.planned_steps = 0
        self.stop_reason = ""
        self.status = "running"
        self._observation_counts: dict[str, int] = {}

    @property
    def elapsed_sec(self) -> float:
        return max(0.0, time.monotonic() - self._started_monotonic)

    def register_plan(self, step_count: int) -> tuple[bool, str]:
        self.planned_steps = max(self.planned_steps, int(step_count))
        if step_count > self.budget.max_steps:
            return False, "max_steps_exceeded"
        return True, ""

    def can_start(
        self,
        *,
        tool_id: str,
        args_hash: str,
        allow_retry: bool = True,
    ) -> tuple[bool, str]:
        if self.status != "running":
            return False, self.stop_reason or "run_not_active"
        if self.elapsed_sec >= self.budget.max_wall_time_sec:
            self.stop("max_wall_time_exceeded")
            return False, self.stop_reason
        if len(self.calls) >= self.budget.max_tool_calls:
            self.stop("max_tool_calls_exceeded")
            return False, self.stop_reason
        if len(self.calls) >= self.budget.max_steps:
            self.stop("max_steps_exceeded")
            return False, self.stop_reason
        repeats = sum(
            1
            for call in self.calls
            if call.tool_id == tool_id and call.args_hash == args_hash
        )
        retry_limit = self.budget.max_retries_per_tool if allow_retry else 0
        if repeats > retry_limit:
            self.stop("repeated_tool_call")
            return False, self.stop_reason
        return True, ""

    def start_call(
        self,
        *,
        tool_id: str,
        args_hash: str,
        task_id: str = "",
        timeout_sec: float | None = None,
        writes_selection: bool = False,
        allow_retry: bool = True,
    ) -> ScheduledCall:
        allowed, reason = self.can_start(
            tool_id=tool_id,
            args_hash=args_hash,
            allow_retry=allow_retry,
        )
        if not allowed:
            raise RuntimeError(reason)
        call = ScheduledCall(
            call_id=uuid.uuid4().hex[:12],
            tool_id=tool_id,
            args_hash=args_hash,
            task_id=task_id,
            timeout_sec=timeout_sec,
            writes_selection=writes_selection,
        )
        self.calls.append(call)
        return call

    def active_call(self, tool_id: str) -> ScheduledCall | None:
        return next(
            (
                call
                for call in reversed(self.calls)
                if call.tool_id == tool_id and call.status == "running"
            ),
            None,
        )

    def finish_call(
        self,
        tool_id: str,
        *,
        status: str,
        observation_signature: str = "",
    ) -> ScheduledCall | None:
        call = self.active_call(tool_id)
        if call is None:
            return None
        call.status = status
        call.ended_at = utc_now()
        call.observation_signature = observation_signature
        if observation_signature:
            count = self._observation_counts.get(observation_signature, 0) + 1
            self._observation_counts[observation_signature] = count
            if count > self.budget.max_no_progress_rounds:
                self.stop("loop_stalled")
        return call

    def stop(self, reason: str, *, status: str = "partial") -> None:
        self.stop_reason = str(reason or "stopped")
        self.status = status

    def complete(self) -> None:
        if self.status == "running":
            self.status = "completed"

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "planned_steps": self.planned_steps,
            "tool_calls": len(self.calls),
            "budget": self.budget.to_dict(),
            "calls": [call.to_dict() for call in self.calls[-16:]],
        }
