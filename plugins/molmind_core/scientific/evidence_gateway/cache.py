"""SQLite query-state cache with explicit missing/error/staleness semantics."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from plugins.molmind_core.scientific.evidence_gateway.contract import (
    json_safe,
    redact_text,
)

QueryStatus = Literal[
    "hit",
    "verified_empty",
    "query_failed",
    "auth_missing",
    "not_queried",
    "identity_review_required",
    "annotation_only",
]
Decision = Literal[
    "local_hit",
    "skip_fresh_verified_empty",
    "query_remote",
    "retry_remote",
    "offline_missing",
]

QUERY_STATUSES = frozenset(
    {
        "hit",
        "verified_empty",
        "query_failed",
        "auth_missing",
        "not_queried",
        "identity_review_required",
        "annotation_only",
    }
)
_DEFAULT_TTL_DAYS = {
    "hit": 90.0,
    "annotation_only": 30.0,
    "verified_empty": 14.0,
}
_DEFAULT_RETRY_MINUTES = {"query_failed": 60.0, "auth_missing": 10.0}
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_text(value: Any) -> str:
    return redact_text(value)


def _redact_payload(value: Any) -> Any:
    return json_safe(value)


def _parse_clock(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class QueryDecision:
    action: Decision
    status: QueryStatus
    payload_path: str | None = None
    reason: str = ""
    retrieved_at: str | None = None
    expires_at: str | None = None
    next_retry_at: str | None = None
    attempt_count: int = 0
    lookup_field: str | None = None
    lookup_value: str | None = None
    match_type: str | None = None
    # The current query status may be projected to ``not_queried`` when a
    # cached state is stale in offline mode.  Preserve the stored state
    # separately so the audit can explain why no current lookup was claimed.
    prior_status: QueryStatus | None = None


class EvidenceQueryCache:
    """Persistent source/entity/endpoint state.

    The connection is intentionally owned by the constructing thread.  Network
    workers return values to the coordinator, which performs all SQLite writes
    on this thread.
    """

    def __init__(self, path: Path, config: Mapping[str, Any] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_days: dict[str, float] = dict(_DEFAULT_TTL_DAYS)
        self._retry_minutes: dict[str, float] = dict(_DEFAULT_RETRY_MINUTES)
        self.configure(config)
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
              lookup_field TEXT,
              lookup_value TEXT,
              match_type TEXT,
              endpoint_url TEXT,
              adapter_version TEXT,
              query_type TEXT,
              query_contract_hash TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (source_id, entity_key, endpoint, query_contract_hash)
            );
            CREATE INDEX IF NOT EXISTS source_query_status_idx
              ON source_query(source_id, status, expires_at);
            """
        )
        self._migrate_source_query_columns()
        self._migrate_source_query_primary_key()
        self.db.commit()

    def configure(self, config: Mapping[str, Any] | None) -> None:
        """Apply cache TTL/backoff policy from a full or cache-only config."""

        if not config:
            return
        block: Mapping[str, Any] = config.get("cache", config)  # type: ignore[arg-type]
        ttl = block.get("ttl_days") or {}
        retry = block.get("retry_minutes") or {}
        for status, value in ttl.items():
            try:
                self._ttl_days[str(status)] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        for status, value in retry.items():
            try:
                self._retry_minutes[str(status)] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue

    def _migrate_source_query_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(source_query)").fetchall()
        }
        additions = {
            "payload_json": "TEXT",
            "lookup_field": "TEXT",
            "lookup_value": "TEXT",
            "match_type": "TEXT",
            "endpoint_url": "TEXT",
            "adapter_version": "TEXT",
            "query_type": "TEXT",
            "query_contract_hash": "TEXT NOT NULL DEFAULT ''",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                self.db.execute(
                    f"ALTER TABLE source_query ADD COLUMN {name} {sql_type}"
                )

    def _migrate_source_query_primary_key(self) -> None:
        info = self.db.execute("PRAGMA table_info(source_query)").fetchall()
        primary_key = [
            str(row["name"])
            for row in sorted(info, key=lambda item: int(item["pk"] or 0))
            if int(row["pk"] or 0)
        ]
        expected = ["source_id", "entity_key", "endpoint", "query_contract_hash"]
        if primary_key == expected:
            return
        columns = [str(row["name"]) for row in info]
        ordered = [
            "source_id", "entity_key", "endpoint", "status", "retrieved_at",
            "expires_at", "next_retry_at", "attempt_count", "payload_path",
            "payload_json", "payload_sha256", "source_version", "error_type",
            "error_message", "lookup_field", "lookup_value", "match_type",
            "endpoint_url", "adapter_version", "query_type", "query_contract_hash",
        ]
        self.db.execute(
            """
            CREATE TABLE source_query_v3 (
              source_id TEXT NOT NULL, entity_key TEXT NOT NULL,
              endpoint TEXT NOT NULL, status TEXT NOT NULL, retrieved_at TEXT,
              expires_at TEXT, next_retry_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0, payload_path TEXT,
              payload_json TEXT, payload_sha256 TEXT, source_version TEXT,
              error_type TEXT, error_message TEXT, lookup_field TEXT,
              lookup_value TEXT, match_type TEXT, endpoint_url TEXT,
              adapter_version TEXT, query_type TEXT,
              query_contract_hash TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (source_id, entity_key, endpoint, query_contract_hash)
            )
            """
        )
        select_columns = [
            name if name in columns else ("''" if name == "query_contract_hash" else "NULL")
            for name in ordered
        ]
        self.db.execute(
            f"INSERT INTO source_query_v3 ({','.join(ordered)}) "
            f"SELECT {','.join(select_columns)} FROM source_query"
        )
        self.db.execute("DROP TABLE source_query")
        self.db.execute("ALTER TABLE source_query_v3 RENAME TO source_query")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS source_query_status_idx "
            "ON source_query(source_id, status, expires_at)"
        )

    def close(self) -> None:
        self.db.close()

    def upsert_entity(self, entity_key: str, **identity: Any) -> None:
        """Merge identity fields; partial callers never erase known values."""

        self.db.execute(
            """
            INSERT INTO entity(entity_key, original_inchikey, standardized_inchikey,
              cas, standardized_smiles, updated_at)
            VALUES (?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), ?)
            ON CONFLICT(entity_key) DO UPDATE SET
              original_inchikey=COALESCE(excluded.original_inchikey, entity.original_inchikey),
              standardized_inchikey=COALESCE(excluded.standardized_inchikey, entity.standardized_inchikey),
              cas=COALESCE(excluded.cas, entity.cas),
              standardized_smiles=COALESCE(excluded.standardized_smiles, entity.standardized_smiles),
              updated_at=excluded.updated_at
            """,
            (
                str(entity_key),
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
        lookup_field: str | None = None,
        lookup_value: str | None = None,
        match_type: str | None = None,
        endpoint_url: str | None = None,
        adapter_version: str | None = None,
        query_type: str | None = None,
        query_contract_hash: str = "",
    ) -> None:
        if status not in QUERY_STATUSES:
            raise ValueError(f"unsupported evidence query status: {status}")
        now = utc_now()
        if ttl is None and status in self._ttl_days:
            ttl = timedelta(days=self._ttl_days[status])
        if retry_after is None and status in self._retry_minutes:
            retry_after = timedelta(minutes=self._retry_minutes[status])

        safe_payload = _redact_payload(payload) if payload is not None else None
        payload_json = (
            json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if safe_payload is not None
            else None
        )
        if payload_json is not None and not payload_sha256:
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        existing = self.db.execute(
            "SELECT attempt_count FROM source_query WHERE source_id=? AND entity_key=? "
            "AND endpoint=? AND query_contract_hash=?",
            (source_id, entity_key, endpoint, query_contract_hash),
        ).fetchone()
        attempts = int(existing["attempt_count"]) + 1 if existing else 1
        self.db.execute(
            """
            INSERT INTO source_query(source_id, entity_key, endpoint, status,
              retrieved_at, expires_at, next_retry_at, attempt_count, payload_path,
              payload_json, payload_sha256, source_version, error_type, error_message,
              lookup_field, lookup_value, match_type, endpoint_url, adapter_version,
              query_type, query_contract_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, entity_key, endpoint, query_contract_hash) DO UPDATE SET
              status=excluded.status, retrieved_at=excluded.retrieved_at,
              expires_at=excluded.expires_at, next_retry_at=excluded.next_retry_at,
              attempt_count=excluded.attempt_count,
              payload_path=CASE
                WHEN excluded.status IN ('query_failed', 'auth_missing')
                  THEN COALESCE(excluded.payload_path, source_query.payload_path)
                ELSE excluded.payload_path END,
              payload_json=CASE
                WHEN excluded.status IN ('query_failed', 'auth_missing')
                  THEN COALESCE(excluded.payload_json, source_query.payload_json)
                ELSE excluded.payload_json END,
              payload_sha256=CASE
                WHEN excluded.status IN ('query_failed', 'auth_missing')
                  THEN COALESCE(excluded.payload_sha256, source_query.payload_sha256)
                ELSE excluded.payload_sha256 END,
              source_version=CASE
                WHEN excluded.status IN ('query_failed', 'auth_missing')
                  THEN COALESCE(excluded.source_version, source_query.source_version)
                ELSE excluded.source_version END,
              error_type=excluded.error_type,
              error_message=excluded.error_message,
              lookup_field=COALESCE(NULLIF(excluded.lookup_field, ''), source_query.lookup_field),
              lookup_value=COALESCE(NULLIF(excluded.lookup_value, ''), source_query.lookup_value),
              match_type=COALESCE(NULLIF(excluded.match_type, ''), source_query.match_type),
              endpoint_url=COALESCE(NULLIF(excluded.endpoint_url, ''), source_query.endpoint_url),
              adapter_version=CASE
                WHEN excluded.status IN ('query_failed', 'auth_missing')
                  THEN COALESCE(NULLIF(excluded.adapter_version, ''), source_query.adapter_version)
                ELSE excluded.adapter_version END,
              query_type=COALESCE(NULLIF(excluded.query_type, ''), source_query.query_type)
            """,
            (
                source_id,
                entity_key,
                endpoint,
                status,
                now.isoformat(),
                (now + ttl).isoformat() if ttl is not None else None,
                (now + retry_after).isoformat() if retry_after is not None else None,
                attempts,
                payload_path,
                payload_json,
                payload_sha256,
                source_version,
                type(error).__name__ if error else None,
                _redact_text(error)[:1000] if error else None,
                lookup_field,
                lookup_value,
                match_type,
                _redact_text(endpoint_url) if endpoint_url else None,
                adapter_version,
                query_type,
                query_contract_hash,
            ),
        )
        self.db.commit()

    def load_payload(
        self, *, source_id: str, entity_key: str, endpoint: str,
        query_contract_hash: str = "",
    ) -> Any | None:
        if query_contract_hash:
            row = self.db.execute(
                "SELECT payload_json, payload_path, payload_sha256 FROM source_query "
                "WHERE source_id=? AND entity_key=? AND endpoint=? AND query_contract_hash=?",
                (source_id, entity_key, endpoint, query_contract_hash),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT payload_json, payload_path, payload_sha256 FROM source_query "
                "WHERE source_id=? AND entity_key=? AND endpoint=? "
                "ORDER BY retrieved_at DESC LIMIT 1",
                (source_id, entity_key, endpoint),
            ).fetchone()
        if row is None:
            return None
        if row["payload_json"]:
            try:
                raw_inline = str(row["payload_json"]).encode("utf-8")
                expected_sha = str(row["payload_sha256"] or "").strip().lower()
                if expected_sha and hashlib.sha256(raw_inline).hexdigest() != expected_sha:
                    return None
                return json.loads(raw_inline.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
        raw_path = str(row["payload_path"] or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            cache_root = self.path.parent.resolve()
            path = (cache_root / path).resolve()
            # Query state is allowed to reference immutable objects inside its
            # cache directory, never arbitrary relative files via ``..``.
            if path != cache_root and cache_root not in path.parents:
                return None
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
            expected_sha = str(row["payload_sha256"] or "").strip().lower()
            if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
                return None
            return json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def get_state(
        self, *, source_id: str, entity_key: str, endpoint: str,
        query_contract_hash: str = "",
    ) -> dict[str, Any] | None:
        if query_contract_hash:
            row = self.db.execute(
            """
            SELECT source_id, entity_key, endpoint, status, retrieved_at,
              expires_at, next_retry_at, attempt_count, payload_path,
              payload_sha256, source_version, error_type, error_message,
              lookup_field, lookup_value, match_type, endpoint_url,
              adapter_version, query_type, query_contract_hash
            FROM source_query
            WHERE source_id=? AND entity_key=? AND endpoint=? AND query_contract_hash=?
            """,
                (source_id, entity_key, endpoint, query_contract_hash),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM source_query WHERE source_id=? AND entity_key=? "
                "AND endpoint=? ORDER BY retrieved_at DESC LIMIT 1",
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
        force_refresh: bool = False,
        expected_adapter_version: str | None = None,
        expected_endpoint_url: str | None = None,
        expected_query_contract_hash: str = "",
    ) -> QueryDecision:
        clock = now or utc_now()
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        row = self.db.execute(
            "SELECT * FROM source_query WHERE source_id=? AND entity_key=? AND endpoint=? "
            "AND query_contract_hash=?",
            (source_id, entity_key, endpoint, expected_query_contract_hash),
        ).fetchone()
        if row is None:
            legacy = self.db.execute(
                "SELECT * FROM source_query WHERE source_id=? AND entity_key=? "
                "AND endpoint=? ORDER BY retrieved_at DESC LIMIT 1",
                (source_id, entity_key, endpoint),
            ).fetchone()
            if legacy is not None:
                return QueryDecision(
                    "query_remote" if online else "offline_missing",
                    "not_queried",
                    legacy["payload_path"],
                    "cached query contract changed: query_contract_hash",
                    prior_status=legacy["status"],
                )
            return QueryDecision(
                "query_remote" if online else "offline_missing",
                "not_queried",
                reason="no local query state",
            )

        status: QueryStatus = row["status"]
        expires = _parse_clock(row["expires_at"])
        retry = _parse_clock(row["next_retry_at"])
        # Legacy rows without an expiry cannot silently become immortal under
        # a newer TTL policy. Identity-review rows are handled separately.
        fresh = expires is not None and expires > clock
        common = {
            "retrieved_at": row["retrieved_at"],
            "expires_at": row["expires_at"],
            "next_retry_at": row["next_retry_at"],
            "attempt_count": int(row["attempt_count"] or 0),
            "lookup_field": row["lookup_field"],
            "lookup_value": row["lookup_value"],
            "match_type": row["match_type"],
            "prior_status": status,
        }

        if status == "identity_review_required":
            # Provider identity-review payloads (for example a multi-CID list)
            # are part of the scientific audit and must survive cache replay.
            # They never authorize another remote lookup or enter scoring.
            if self.load_payload(
                source_id=source_id,
                entity_key=entity_key,
                endpoint=endpoint,
                query_contract_hash=expected_query_contract_hash,
            ) is not None:
                return QueryDecision(
                    "local_hit",
                    status,
                    row["payload_path"],
                    "cached identity review payload",
                    **common,
                )
            return QueryDecision(
                "offline_missing",
                status,
                row["payload_path"],
                "identity review required before provider lookup",
                **common,
            )

        # Backoff is never bypassed by force_refresh.
        if status in {"query_failed", "auth_missing"} and retry and retry > clock:
            return QueryDecision(
                "offline_missing",
                status,
                row["payload_path"],
                "retry backoff active",
                **common,
            )

        contract_mismatches: list[str] = []
        expected_version = str(expected_adapter_version or "").strip()
        cached_version = str(row["adapter_version"] or "").strip()
        if expected_version and cached_version != expected_version:
            contract_mismatches.append("adapter_version")
        expected_url = str(expected_endpoint_url or "").strip().rstrip("/")
        cached_url = str(row["endpoint_url"] or "").strip().rstrip("/")
        if expected_url and cached_url != expected_url:
            contract_mismatches.append("endpoint_url")
        if contract_mismatches:
            return QueryDecision(
                "query_remote" if online else "offline_missing",
                "not_queried",
                row["payload_path"],
                "cached query contract changed: " + ",".join(contract_mismatches),
                **common,
            )

        has_payload_reference = bool(row["payload_path"] or row["payload_json"])
        if status in {"hit", "annotation_only"} and fresh:
            payload = (
                self.load_payload(
                    source_id=source_id,
                    entity_key=entity_key,
                    endpoint=endpoint,
                    query_contract_hash=expected_query_contract_hash,
                )
                if has_payload_reference
                else None
            )
            if payload is not None and not (force_refresh and online):
                return QueryDecision(
                    "local_hit",
                    status,
                    row["payload_path"],
                    "fresh cached payload",
                    **common,
                )
            if payload is None:
                return QueryDecision(
                    "retry_remote" if online else "offline_missing",
                    "query_failed",
                    row["payload_path"],
                    "cached hit payload unavailable or failed integrity validation",
                    **common,
                )
        if status == "verified_empty" and fresh:
            if not (force_refresh and online):
                return QueryDecision(
                    "skip_fresh_verified_empty",
                    status,
                    row["payload_path"],
                    "fresh negative cache; not a biological negative label",
                    **common,
                )
        if not online:
            return QueryDecision(
                "offline_missing",
                "not_queried",
                row["payload_path"],
                f"cached {status} state is stale; offline mode did not re-query",
                **common,
            )
        return QueryDecision(
            "retry_remote" if status in {"query_failed", "auth_missing"} else "query_remote",
            status,
            row["payload_path"],
            "forced refresh" if force_refresh else "stale or retryable cache state",
            **common,
        )

    def summary(self) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT source_id, status, COUNT(*) AS n FROM source_query GROUP BY source_id, status"
        ).fetchall()
        return {
            "schema_version": "molmind-evidence-query-cache-summary-v2",
            "counts": [dict(row) for row in rows],
        }
