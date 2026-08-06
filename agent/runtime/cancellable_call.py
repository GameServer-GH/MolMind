"""Hard-interrupt fencing for sync third-party calls.

Python cannot kill a blocked httpx/Chromium thread. This helper runs the work
in a daemon worker and returns as soon as ``cancel_event`` is set; any late
result is discarded so the cancelled run cannot write observations back.
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")

_ACTIVE_CANCEL: ContextVar[threading.Event | None] = ContextVar(
    "molmind_active_cancel_event",
    default=None,
)


class CallCancelled(RuntimeError):
    """Raised when the caller abandons an in-flight cancellable call."""


@dataclass(frozen=True)
class CancellableOutcome:
    status: str  # completed | cancelled | failed
    value: Any = None
    error: BaseException | None = None
    discarded: bool = False


def current_cancel_event() -> threading.Event | None:
    return _ACTIVE_CANCEL.get()


def bind_cancel_event(event: threading.Event | None) -> Token:
    return _ACTIVE_CANCEL.set(event)


def reset_cancel_event(token: Token) -> None:
    _ACTIVE_CANCEL.reset(token)


@contextmanager
def cancel_scope(event: threading.Event | None) -> Iterator[threading.Event | None]:
    """Bind a cancel event for nested LLM/tool helpers in this task."""
    token = bind_cancel_event(event)
    try:
        yield event
    finally:
        reset_cancel_event(token)


def resolve_cancel_event(
    cancel_event: threading.Event | None = None,
) -> threading.Event | None:
    return cancel_event if cancel_event is not None else current_cancel_event()


def run_cancellable(
    fn: Callable[[], T],
    *,
    cancel_event: threading.Event,
    expected_run_id: str = "",
    current_run_id: Callable[[], str] | None = None,
    poll_sec: float = 0.25,
    join_timeout: float = 0.05,
) -> T:
    """Execute ``fn`` until it finishes or cancel/run-id fence trips.

    On cancel, raises :class:`CallCancelled` immediately. The worker may keep
    running in the background; its return value is never surfaced to the caller.
    """
    outcome_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

    def worker() -> None:
        try:
            outcome_queue.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 — surfaced to caller when not cancelled
            outcome_queue.put(("err", exc))

    thread = threading.Thread(target=worker, name="cancellable-call", daemon=True)
    thread.start()
    while True:
        if cancel_event.is_set():
            thread.join(timeout=join_timeout)
            raise CallCancelled("call cancelled")
        if expected_run_id and current_run_id is not None:
            if str(current_run_id() or "") != expected_run_id:
                thread.join(timeout=join_timeout)
                raise CallCancelled("run_id fence discarded late result")
        try:
            kind, payload = outcome_queue.get(timeout=max(0.05, float(poll_sec)))
        except queue.Empty:
            continue
        if kind == "ok":
            if cancel_event.is_set():
                raise CallCancelled("call cancelled")
            if expected_run_id and current_run_id is not None:
                if str(current_run_id() or "") != expected_run_id:
                    raise CallCancelled("run_id fence discarded late result")
            return payload  # type: ignore[return-value]
        if isinstance(payload, BaseException):
            if cancel_event.is_set():
                raise CallCancelled("call cancelled") from payload
            raise payload
        raise RuntimeError("cancellable worker returned an unexpected payload")


def wait_interruptible(
    cancel_event: threading.Event,
    *,
    timeout_sec: float,
    slice_sec: float = 0.5,
) -> bool:
    """Sleep up to ``timeout_sec`` in slices; return True if cancelled early."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while time.monotonic() < deadline:
        if cancel_event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if cancel_event.wait(timeout=min(max(0.05, float(slice_sec)), remaining)):
            return True
    return cancel_event.is_set()
