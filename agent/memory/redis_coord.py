"""Optional Redis coordination for session mutation locks and pubsub."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator


class RedisCoordinator:
    def __init__(self, url: str | None) -> None:
        self._url = (url or "").strip() or None
        self._client: Any = None
        self._available = False
        if self._url:
            try:
                import redis

                self._client = redis.from_url(self._url, socket_connect_timeout=1.0)
                self._client.ping()
                self._available = True
            except Exception:
                self._client = None
                self._available = False

    @property
    def enabled(self) -> bool:
        return self._available and self._client is not None

    @contextmanager
    def mutation_lock(
        self,
        session_id: str,
        ttl_seconds: int = 10,
        *,
        wait_seconds: float = 15.0,
        require: bool = False,
    ) -> Iterator[None]:
        """Acquire a short Redis lock, spinning until acquired or wait budget ends."""
        token = uuid.uuid4().hex
        key = f"mm:lock:{session_id}"
        acquired = False
        deadline = time.monotonic() + max(0.1, float(wait_seconds))
        if self.enabled:
            while time.monotonic() < deadline:
                try:
                    acquired = bool(
                        self._client.set(key, token, nx=True, ex=max(1, int(ttl_seconds)))
                    )
                except Exception:
                    acquired = False
                    break
                if acquired:
                    break
                time.sleep(0.005)
            if require and not acquired:
                raise TimeoutError(f"redis session lock timeout: {session_id}")
        try:
            yield
        finally:
            if acquired and self.enabled:
                try:
                    current = self._client.get(key)
                    if current and (
                        current.decode() if isinstance(current, bytes) else str(current)
                    ) == token:
                        self._client.delete(key)
                except Exception:
                    pass

    def publish_event(self, session_id: str, payload: dict) -> None:
        if not self.enabled:
            return
        try:
            self._client.publish(
                f"mm:events:{session_id}",
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            return
