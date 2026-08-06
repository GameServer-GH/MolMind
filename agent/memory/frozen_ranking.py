"""Durable frozen-ranking snapshots for cross-process hydrate.

``last_result`` stays the hot in-memory PipelineResult. This module stores a
compact JSON snapshot (ScoreRecord fields + run/config identity) so reload,
multi-worker get(), and PDF/export/explain can recover ``frozen_result``
without pickling the full mutable pipeline object.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
import json
from typing import Any

from packages.models import ScoreRecord
from plugins.molmind_core.scientific.pipeline.export import to_csv_text


_SCORE_FIELD_NAMES = {item.name for item in fields(ScoreRecord)}


def _score_record_to_dict(mol: Any) -> dict[str, Any]:
    if is_dataclass(mol) and not isinstance(mol, type):
        raw = asdict(mol)
    else:
        raw = {name: getattr(mol, name, None) for name in _SCORE_FIELD_NAMES}
    raw.pop("fp_bits", None)
    # Drop heavy / non-JSON nested evidence payloads; identity + scores remain.
    raw["attributions"] = []
    raw["evidence_hits"] = []
    raw["citations"] = []
    for key in ("eligibility_reasons", "audit_missing"):
        value = raw.get(key)
        if isinstance(value, tuple):
            raw[key] = list(value)
    # JSON-safe primitives only.
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _SCORE_FIELD_NAMES:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, (list, tuple, dict)):
            try:
                json.dumps(value)
                clean[key] = value
            except (TypeError, ValueError):
                clean[key] = [] if isinstance(value, (list, tuple)) else {}
        else:
            clean[key] = str(value)
    return clean


def rehydrate_score_record(data: dict[str, Any]) -> ScoreRecord:
    cleaned = {key: value for key, value in dict(data or {}).items() if key in _SCORE_FIELD_NAMES}
    cleaned.pop("fp_bits", None)
    cleaned.setdefault("attributions", [])
    cleaned.setdefault("evidence_hits", [])
    cleaned.setdefault("citations", [])
    for key in ("eligibility_reasons", "audit_missing"):
        if isinstance(cleaned.get(key), list):
            cleaned[key] = tuple(cleaned[key])
    return ScoreRecord(**cleaned)


@dataclass
class _FrozenConfig:
    config_hash: str = ""
    mode: str = "competition"
    degraded_channels: list[str] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def llm(self) -> dict[str, Any]:
        return dict(self.raw.get("llm") or {})

    @property
    def reserve_n(self) -> int:
        return int(self.raw.get("reserve_n", 20))

    def mark_degraded(self, channel: str) -> None:
        if channel not in self.degraded_channels:
            self.degraded_channels.append(channel)


@dataclass
class FrozenRankingResult:
    """Duck-types PipelineResult for export / explain / mechanism."""

    top_molecules: list[ScoreRecord]
    reserve_molecules: list[ScoreRecord] = field(default_factory=list)
    run_id: str = ""
    input_sha256: str = ""
    selection_sha256: str = ""
    reserve_selection_sha256: str = ""
    source_filename: str = ""
    config: _FrozenConfig = field(default_factory=_FrozenConfig)
    mechanism_graphs: list[Any] = field(default_factory=list)
    scored_molecules: list[ScoreRecord] = field(default_factory=list)
    hydrated_from_snapshot: bool = True

    @property
    def output_count(self) -> int:
        return len(self.top_molecules)

    def to_csv_text(self) -> str:
        return to_csv_text(
            self.top_molecules,
            mode=self.config.mode,
            config_hash=self.config.config_hash,
            degraded_channels=self.config.degraded_channels,
            run_id=self.run_id,
            input_sha256=self.input_sha256,
            selection_hash=self.selection_sha256,
        )

    def to_reserve_csv_text(self) -> str:
        return to_csv_text(
            self.reserve_molecules,
            mode=self.config.mode,
            config_hash=self.config.config_hash,
            degraded_channels=self.config.degraded_channels,
            run_id=self.run_id,
            input_sha256=self.input_sha256,
            selection_hash=self.reserve_selection_sha256,
        )


def snapshot_from_result(result: Any) -> dict[str, Any] | None:
    """Build a JSON-safe frozen_ranking dict from a live pipeline result."""
    if result is None:
        return None
    top = list(getattr(result, "top_molecules", None) or [])
    if not top:
        return None
    reserve = list(getattr(result, "reserve_molecules", None) or [])
    config = getattr(result, "config", None)
    config_payload = {
        "config_hash": str(getattr(config, "config_hash", "") or ""),
        "mode": str(getattr(config, "mode", "competition") or "competition"),
        "degraded_channels": list(getattr(config, "degraded_channels", None) or []),
        "assumptions": dict(getattr(config, "assumptions", None) or {}),
        "reserve_n": int(getattr(config, "reserve_n", 20) or 20),
        "llm": dict(getattr(config, "llm", None) or {}),
    }
    return {
        "run_id": str(getattr(result, "run_id", "") or ""),
        "top_n": len(top),
        "input_sha256": str(getattr(result, "input_sha256", "") or ""),
        "selection_sha256": str(getattr(result, "selection_sha256", "") or ""),
        "reserve_selection_sha256": str(
            getattr(result, "reserve_selection_sha256", "") or ""
        ),
        "source_filename": str(getattr(result, "source_filename", "") or ""),
        "config": config_payload,
        "top_molecules": [_score_record_to_dict(mol) for mol in top],
        "reserve_molecules": [_score_record_to_dict(mol) for mol in reserve],
    }


def hydrate_from_snapshot(snapshot: dict[str, Any] | None) -> FrozenRankingResult | None:
    if not isinstance(snapshot, dict):
        return None
    raw_top = snapshot.get("top_molecules") or []
    if not isinstance(raw_top, list) or not raw_top:
        return None
    top = [
        rehydrate_score_record(item)
        for item in raw_top
        if isinstance(item, dict)
    ]
    if not top:
        return None
    reserve = [
        rehydrate_score_record(item)
        for item in (snapshot.get("reserve_molecules") or [])
        if isinstance(item, dict)
    ]
    cfg = snapshot.get("config") if isinstance(snapshot.get("config"), dict) else {}
    frozen_cfg = _FrozenConfig(
        config_hash=str(cfg.get("config_hash") or ""),
        mode=str(cfg.get("mode") or "competition"),
        degraded_channels=list(cfg.get("degraded_channels") or []),
        assumptions=dict(cfg.get("assumptions") or {}),
        raw={
            "llm": dict(cfg.get("llm") or {}),
            "reserve_n": int(cfg.get("reserve_n") or 20),
        },
    )
    return FrozenRankingResult(
        top_molecules=top,
        reserve_molecules=reserve,
        run_id=str(snapshot.get("run_id") or ""),
        input_sha256=str(snapshot.get("input_sha256") or ""),
        selection_sha256=str(snapshot.get("selection_sha256") or ""),
        reserve_selection_sha256=str(snapshot.get("reserve_selection_sha256") or ""),
        source_filename=str(snapshot.get("source_filename") or ""),
        config=frozen_cfg,
    )


def ensure_session_last_result(session: Any) -> Any | None:
    """Return hot last_result, hydrating from frozen_ranking when needed."""
    current = getattr(session, "last_result", None)
    if current is not None:
        return current
    snapshot = getattr(session, "frozen_ranking", None)
    hydrated = hydrate_from_snapshot(snapshot if isinstance(snapshot, dict) else None)
    if hydrated is not None:
        session.last_result = hydrated
    return getattr(session, "last_result", None)


def has_durable_freeze(session: Any) -> bool:
    if getattr(session, "last_result", None) is not None:
        return True
    snapshot = getattr(session, "frozen_ranking", None)
    if isinstance(snapshot, dict) and (snapshot.get("top_molecules") or snapshot.get("run_id")):
        return True
    if str(getattr(session, "last_run_id", "") or "").strip():
        return True
    history = getattr(session, "run_history", None) or []
    return bool(history)
