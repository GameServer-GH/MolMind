"""科学核 Tool 包装：不改变 screen_sdf 语义。"""

from __future__ import annotations

import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from plugins.molmind_core.scientific.pipeline import load_config, screen_sdf
from plugins.molmind_core.tools.evidence_query import (
    EvidenceQueryResponse,
    ProviderAdapter,
    run_query_evidence_impl,
)

LogSink = Callable[[Any], None]


def run_score_and_rank(
    sdf_path: Path,
    *,
    top_n: int,
    source_filename: str,
    log_sink: LogSink | None = None,
) -> Any:
    cfg = load_config(mode="auto", use_snapshot=True, allow_live=False)
    return screen_sdf(
        sdf_path,
        cfg=cfg,
        top_n=top_n,
        source_filename=source_filename,
        log_sink=log_sink,
    )


def run_query_evidence(
    *,
    result: Any = None,
    molecule_index: Mapping[str, Any] | None = None,
    molecule_id: str | None = None,
    inchikey: str | None = None,
    cas: str | None = None,
    smiles: str | None = None,
    providers: Sequence[str] | None = None,
    query_types: Sequence[str] | None = None,
    allow_live: bool = False,
    force_refresh: bool = False,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    snapshot_dir: Path | None = None,
    provider_config_path: Path | None = None,
    cache_path: Path | None = None,
    provider_adapters: Mapping[str, ProviderAdapter] | None = None,
    total_timeout_sec: float | None = None,
    deadline: float | datetime | None = None,
    cancel_event: threading.Event | None = None,
) -> EvidenceQueryResponse:
    """Canonical, read-only Tool handler for candidate evidence lookup.

    Live access is impossible unless ``allow_live=True`` is present in this
    explicit call.  The returned bundle/card is not written to selection or to
    the frozen ranking snapshot.
    """

    return run_query_evidence_impl(
        result=result,
        molecule_index=molecule_index,
        molecule_id=molecule_id,
        inchikey=inchikey,
        cas=cas,
        smiles=smiles,
        providers=providers,
        query_types=query_types,
        allow_live=allow_live,
        force_refresh=force_refresh,
        event_sink=event_sink,
        snapshot_dir=snapshot_dir,
        provider_config_path=provider_config_path,
        cache_path=cache_path,
        provider_adapters=provider_adapters,
        total_timeout_sec=total_timeout_sec,
        deadline=deadline,
        cancel_event=cancel_event,
    )


# Registry metadata remains declarative; this map is the executable boundary
# used by tests/integrations to prove that a declared core Tool has a handler.
CORE_TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "score_and_rank": run_score_and_rank,
    "query_evidence": run_query_evidence,
}


def timed_call(fn: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


__all__ = [
    "CORE_TOOL_HANDLERS",
    "EvidenceQueryResponse",
    "run_query_evidence",
    "run_score_and_rank",
    "timed_call",
]
