"""Bounded background execution for long-running MCP tools. State in PostgreSQL."""
from __future__ import annotations

import copy
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SCPJobManager:
    def __init__(self, max_workers: int = 2):
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scp-job")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = Lock()
        self.cancel_events: dict[str, Event] = {}
        self.futures: dict[str, Future[Any]] = {}
        self.lease_stops: dict[str, Event] = {}
        self._store = None

    def _job_store(self):
        if self._store is None:
            from agent.memory import build_job_store

            self._store = build_job_store()
        return self._store

    def _persist(self, job: dict[str, Any]) -> None:
        terminal = job.get("status") in {"completed", "failed", "cancelled"}
        self._job_store().upsert(
            {
                "job_id": job["job_id"],
                "kind": "scp_tool",
                "session_id": str(job.get("session_id") or ""),
                "run_id": str(job.get("run_id") or ""),
                "status": job.get("status") or "queued",
                "progress": {
                    "skill_id": job.get("skill_id") or "",
                    "tool_id": job.get("tool_id") or "",
                },
                "result_ref": {"has_result": job.get("result") is not None},
                "error": job.get("error") or "",
                "cancel_reason": job.get("cancel_reason") or "",
                "payload": {
                    "skill_id": job.get("skill_id") or "",
                    "tool_id": job.get("tool_id") or "",
                    "result": job.get("result"),
                    "error_code": job.get("error_code") or "",
                    "arguments": job.get("arguments") or {},
                    "allow_live": bool(job.get("allow_live", True)),
                    "force_refresh": bool(job.get("force_refresh", False)),
                },
                "created_at": job.get("created_at") or _now(),
                "updated_at": job.get("updated_at") or job.get("finished_at") or _now(),
                "finished_at": job.get("finished_at"),
                "lease_owner": str(job.get("lease_owner") or ""),
                "lease_until": job.get("lease_until"),
                "attempt": int(job.get("attempt") or 0),
                "clear_lease": bool(job.get("clear_lease") or terminal),
            }
        )

    def _hydrate(self, job_id: str, *, session_id: str = "") -> dict[str, Any] | None:
        row = self._job_store().get(job_id)
        if not row or str(row.get("kind") or "") != "scp_tool":
            return None
        if session_id and row.get("session_id") != session_id:
            return None
        payload = row.get("payload") or {}
        progress = row.get("progress") or {}
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        if isinstance(progress, str):
            import json

            progress = json.loads(progress)
        return {
            "job_id": job_id,
            "session_id": row.get("session_id") or "",
            "skill_id": payload.get("skill_id") or progress.get("skill_id") or "",
            "tool_id": payload.get("tool_id") or progress.get("tool_id") or "",
            "run_id": row.get("run_id") or "",
            "status": row.get("status") or "queued",
            "created_at": str(row.get("created_at") or ""),
            "result": payload.get("result"),
            "error": row.get("error") or "",
            "error_code": payload.get("error_code") or "",
            "cancel_reason": row.get("cancel_reason") or "",
            "finished_at": str(row.get("finished_at") or "") or None,
            "updated_at": str(row.get("updated_at") or ""),
            "arguments": dict(payload.get("arguments") or {}),
            "allow_live": bool(payload.get("allow_live", True)),
            "force_refresh": bool(payload.get("force_refresh", False)),
            "lease_owner": str(row.get("lease_owner") or ""),
            "attempt": int(row.get("attempt") or 0),
        }

    def _start_lease_heartbeat(self, job_id: str, owner: str) -> None:
        from agent.memory.jobs_store import default_lease_seconds

        stop = Event()
        with self.lock:
            old = self.lease_stops.pop(job_id, None)
            if old is not None:
                old.set()
            self.lease_stops[job_id] = stop
        lease_seconds = default_lease_seconds()
        interval = max(5.0, lease_seconds / 3.0)

        def _loop() -> None:
            while not stop.wait(interval):
                try:
                    if not self._job_store().renew_lease(
                        job_id, owner=owner, lease_seconds=lease_seconds
                    ):
                        break
                except Exception:  # noqa: BLE001
                    break

        threading.Thread(
            target=_loop,
            name=f"scp-lease-{job_id[:8]}",
            daemon=True,
        ).start()

    def _stop_lease_heartbeat(self, job_id: str) -> None:
        with self.lock:
            stop = self.lease_stops.pop(job_id, None)
        if stop is not None:
            stop.set()

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        session_id: str,
        skill_id: str,
        tool_id: str,
        run_id: str = "",
        arguments: dict[str, Any] | None = None,
        allow_live: bool = True,
        force_refresh: bool = False,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        from agent.memory.jobs_store import default_lease_owner

        resolved_job_id = str(job_id or uuid.uuid4().hex)
        job = {
            "job_id": resolved_job_id,
            "session_id": session_id,
            "skill_id": skill_id,
            "tool_id": tool_id,
            "run_id": run_id,
            "status": "queued",
            "created_at": _now(),
            "result": None,
            "error": "",
            "cancel_reason": "",
            "arguments": copy.deepcopy(arguments or {}),
            "allow_live": bool(allow_live),
            "force_refresh": bool(force_refresh),
            "lease_owner": "",
            "attempt": 0,
        }
        cancel_event = Event()
        with self.lock:
            self.jobs[resolved_job_id] = job
            self.cancel_events[resolved_job_id] = cancel_event
        self._persist(job)

        def run() -> None:
            owner = ""
            with self.lock:
                if cancel_event.is_set():
                    job["status"] = "cancelled"
                    job["finished_at"] = _now()
                    job["clear_lease"] = True
                    snapshot = dict(job)
                    self._persist(snapshot)
                    return
                job["status"] = "running"
                job["started_at"] = _now()
                job["updated_at"] = _now()
                snapshot = dict(job)
            self._persist(snapshot)
            try:
                owner = default_lease_owner()
                self._job_store().acquire_lease(
                    resolved_job_id, owner=owner, status="running"
                )
                with self.lock:
                    job["lease_owner"] = owner
                self._start_lease_heartbeat(resolved_job_id, owner)
                result = fn()
                serialized = {
                    **result.__dict__,
                    "content": [block.__dict__ for block in result.content],
                }
                with self.lock:
                    if cancel_event.is_set():
                        job["status"] = "cancelled"
                        job["result"] = None
                    else:
                        job["status"] = "completed"
                        job["result"] = serialized
                    job["finished_at"] = _now()
                    job["updated_at"] = job["finished_at"]
                    job["clear_lease"] = True
                    snapshot = dict(job)
                self._persist(snapshot)
            except Exception as exc:
                with self.lock:
                    if cancel_event.is_set():
                        job["status"] = "cancelled"
                        job["result"] = None
                    else:
                        job["status"] = "failed"
                        job["error_code"] = getattr(exc, "code", "tool_failed")
                        job["error"] = str(exc)
                    job["finished_at"] = _now()
                    job["updated_at"] = job["finished_at"]
                    job["clear_lease"] = True
                    snapshot = dict(job)
                self._persist(snapshot)
            finally:
                self._stop_lease_heartbeat(resolved_job_id)
                try:
                    self._job_store().release_lease(resolved_job_id, owner=owner)
                except Exception:  # noqa: BLE001
                    pass

        future = self.pool.submit(run)
        with self.lock:
            self.futures[resolved_job_id] = future
        future.add_done_callback(lambda _future: self._forget_future(resolved_job_id))
        return dict(job)

    def _forget_future(self, job_id: str) -> None:
        with self.lock:
            self.futures.pop(job_id, None)

    def resume(
        self,
        job_id: str,
        *,
        call_factory: Callable[[dict[str, Any]], Callable[[], Any]],
    ) -> bool:
        """Re-dispatch an orphaned SCP job using persisted arguments."""
        job = self._hydrate(job_id)
        if job is None:
            return False
        if str(job.get("status") or "") in {"completed", "failed", "cancelled"}:
            return False
        if str(job.get("cancel_reason") or "") or str(job.get("status") or "") in {
            "cancel_requested",
            "cancelled",
        }:
            return False
        tool_id = str(job.get("tool_id") or "")
        if not tool_id:
            job["status"] = "failed"
            job["error"] = "orphaned_missing_tool_id"
            job["finished_at"] = _now()
            job["clear_lease"] = True
            self._persist(job)
            return False
        try:
            fn = call_factory(job)
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"orphaned_resume_factory_failed:{exc}"
            job["finished_at"] = _now()
            job["clear_lease"] = True
            self._persist(job)
            return False
        self.submit(
            fn,
            session_id=str(job.get("session_id") or ""),
            skill_id=str(job.get("skill_id") or ""),
            tool_id=tool_id,
            run_id=str(job.get("run_id") or ""),
            arguments=dict(job.get("arguments") or {}),
            allow_live=bool(job.get("allow_live", True)),
            force_refresh=bool(job.get("force_refresh", False)),
            job_id=job_id,
        )
        return True

    def recover_orphan_jobs(
        self,
        *,
        call_factory: Callable[[dict[str, Any]], Callable[[], Any]],
        stale_seconds: int = 120,
        limit: int = 50,
    ) -> list[str]:
        recovered: list[str] = []
        try:
            rows = self._job_store().claim_stale(
                kinds=["scp_tool"],
                stale_seconds=stale_seconds,
                limit=limit,
            )
        except Exception:  # noqa: BLE001 — recovery must not block API startup
            return recovered
        for row in rows:
            job_id = str(row.get("job_id") or "")
            if not job_id:
                continue
            if self.resume(job_id, call_factory=call_factory):
                recovered.append(job_id)
        return recovered

    def cancel(self, job_id: str, *, session_id: str, reason: str = "cancelled") -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            event = self.cancel_events.get(job_id)
            future = self.futures.get(job_id)
            if job is None:
                job = self._hydrate(job_id, session_id=session_id)
                if job is None:
                    return False
                self.jobs[job_id] = job
                self.cancel_events[job_id] = Event()
                event = self.cancel_events[job_id]
            if job.get("session_id") != session_id or job.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                return False
            if event:
                event.set()
            cancelled_before_start = bool(future and future.cancel())
            job["status"] = "cancelled" if cancelled_before_start else "cancel_requested"
            job["cancel_reason"] = reason
            job["updated_at"] = _now()
            if cancelled_before_start:
                job["clear_lease"] = True
            snapshot = dict(job)
        self._stop_lease_heartbeat(job_id)
        self._persist(snapshot)
        self._job_store().request_cancel(job_id, reason=reason)
        return True

    def cancel_for_run(self, *, session_id: str, run_id: str, reason: str = "cancelled") -> list[str]:
        with self.lock:
            ids = [
                job_id
                for job_id, job in self.jobs.items()
                if job.get("session_id") == session_id
                and job.get("run_id") == run_id
                and job.get("status") in {"queued", "running", "cancel_requested", "recovering"}
            ]
        for row in self._job_store().list_by_session(session_id, limit=200):
            if (
                row.get("run_id") == run_id
                and row.get("status") in {"queued", "running", "cancel_requested", "recovering"}
                and row.get("job_id") not in ids
            ):
                ids.append(str(row["job_id"]))
        return [job_id for job_id in ids if self.cancel(job_id, session_id=session_id, reason=reason)]

    def get(self, job_id: str, *, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.get("session_id") == session_id:
                return dict(job)
        return self._hydrate(job_id, session_id=session_id)
