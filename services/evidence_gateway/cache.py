"""SQLite query-state cache with explicit missing/error/staleness semantics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

QueryStatus = Literal[
    "hit",
    "verified_empty",
    "query_failed",
    "auth_missing",
    "not_queried",
]
Decision = Literal[
    "local_hit",
    "skip_fresh_verified_empty",
    "query_remote",
    "retry_remote",
    "offline_missing",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QueryDecision:
    action: Decision
    status: QueryStatus
    payload_path: str | None = None
    reason: str = ""


class EvidenceQueryCache:
    """Persistent source/entity/endpoint state; payloads remain content files."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity (
              entity_key TEXT PRIMARY KEY,
              original_inchikey TEXT,
              standardized_inchikey TEXT,
              cas TEXT,
              standardized_smiles TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_query (
              source_id TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              status TEXT NOT NULL,
              retrieved_at TEXT,
              expires_at TEXT,
              next_retry_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              payload_path TEXT,
              payload_json TEXT,
              payload_sha256 TEXT,
              source_version TEXT,
              error_type TEXT,
              error_message TEXT,
              PRIMARY KEY (source_id, entity_key, endpoint)
            );
            CREATE INDEX IF NOT EXISTS source_query_status_idx
              ON source_query(source_id, status, expires_at);
            """
        )
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(source_query)").fetchall()
        }
        if "payload_json" not in columns:
            self.db.execute("ALTER TABLE source_query ADD COLUMN payload_json TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert_entity(self, entity_key: str, **identity: Any) -> None:
        self.db.execute(
            """
            INSERT INTO entity(entity_key, original_inchikey, standardized_inchikey,
              cas, standardized_smiles, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_key) DO UPDATE SET
              original_inchikey=excluded.original_inchikey,
              standardized_inchikey=excluded.standardized_inchikey,
              cas=excluded.cas,
              standardized_smiles=excluded.standardized_smiles,
              updated_at=excluded.updated_at
            """,
            (
                entity_key,
                identity.get("original_inchikey"),
                identity.get("standardized_inchikey"),
                identity.get("cas"),
                identity.get("standardized_smiles"),
                utc_now().isoformat(),
            ),
        )
        self.db.commit()

    def record(
        self,
        *,
        source_id: str,
        entity_key: str,
        endpoint: str,
        status: QueryStatus,
        ttl: timedelta | None = None,
        retry_after: timedelta | None = None,
        payload_path: str | None = None,
        payload: Any | None = None,
        payload_sha256: str | None = None,
        source_version: str | None = None,
        error: Exception | None = None,
    ) -> None:
        now = utc_now()
        existing = self.db.execute(
            "SELECT attempt_count FROM source_query WHERE source_id=? AND entity_key=? AND endpoint=?",
            (source_id, entity_key, endpoint),
        ).fetchone()
        attempts = int(existing["attempt_count"]) + 1 if existing else 1
        self.db.execute(
            """
            INSERT INTO source_query(source_id, entity_key, endpoint, status,
              retrieved_at, expires_at, next_retry_at, attempt_count, payload_path,
              payload_json, payload_sha256, source_version, error_type, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, entity_key, endpoint) DO UPDATE SET
              status=excluded.status, retrieved_at=excluded.retrieved_at,
              expires_at=excluded.expires_at, next_retry_at=excluded.next_retry_at,
              attempt_count=excluded.attempt_count, payload_path=excluded.payload_path,
              payload_json=excluded.payload_json, payload_sha256=excluded.payload_sha256,
              source_version=excluded.source_version, error_type=excluded.error_type,
              error_message=excluded.error_message
            """,
            (
                source_id,
                entity_key,
                endpoint,
                status,
                now.isoformat(),
                (now + ttl).isoformat() if ttl else None,
                (now + retry_after).isoformat() if retry_after else None,
                attempts,
                payload_path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload is not None else None,
                payload_sha256,
                source_version,
                type(error).__name__ if error else None,
                str(error)[:1000] if error else None,
            ),
        )
        self.db.commit()

    def load_payload(self, *, source_id: str, entity_key: str, endpoint: str) -> Any | None:
        row = self.db.execute(
            "SELECT payload_json FROM source_query WHERE source_id=? AND entity_key=? AND endpoint=?",
            (source_id, entity_key, endpoint),
        ).fetchone()
        if row is None or not row["payload_json"]:
            return None
        return json.loads(row["payload_json"])

    def get_state(self, *, source_id: str, entity_key: str, endpoint: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT source_id, entity_key, endpoint, status, retrieved_at,
              expires_at, next_retry_at, attempt_count, payload_path,
              payload_sha256, source_version, error_type, error_message
            FROM source_query
            WHERE source_id=? AND entity_key=? AND endpoint=?
            """,
            (source_id, entity_key, endpoint),
        ).fetchone()
        return dict(row) if row is not None else None

    def decide(
        self,
        *,
        source_id: str,
        entity_key: str,
        endpoint: str,
        online: bool,
        now: datetime | None = None,
    ) -> QueryDecision:
        clock = now or utc_now()
        row = self.db.execute(
            "SELECT * FROM source_query WHERE source_id=? AND entity_key=? AND endpoint=?",
            (source_id, entity_key, endpoint),
        ).fetchone()
        if row is None:
            return QueryDecision(
                "query_remote" if online else "offline_missing",
                "not_queried",
                reason="no local query state",
            )
        status: QueryStatus = row["status"]
        expires = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        retry = datetime.fromisoformat(row["next_retry_at"]) if row["next_retry_at"] else None
        fresh = expires is None or expires > clock
        if status == "hit" and fresh and (row["payload_path"] or row["payload_json"]):
            return QueryDecision("local_hit", status, row["payload_path"], "fresh cached payload")
        if status == "verified_empty" and fresh:
            return QueryDecision(
                "skip_fresh_verified_empty",
                status,
                reason="fresh negative cache; not a biological negative label",
            )
        if not online:
            return QueryDecision("offline_missing", status, row["payload_path"], "offline mode")
        if status in {"query_failed", "auth_missing"} and retry and retry > clock:
            return QueryDecision("offline_missing", status, reason="retry backoff active")
        return QueryDecision(
            "retry_remote" if status in {"query_failed", "auth_missing"} else "query_remote",
            status,
            row["payload_path"],
            "stale or retryable cache state",
        )

    def summary(self) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT source_id, status, COUNT(*) AS n FROM source_query GROUP BY source_id, status"
        ).fetchall()
        return {
            "schema_version": "molmind-evidence-query-cache-summary-v1",
            "counts": [dict(row) for row in rows],
        }
