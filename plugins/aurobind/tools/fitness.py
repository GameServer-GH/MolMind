"""AuroBind fitness 旁证 Tools — 只进证据卡，永不写主榜。"""

from __future__ import annotations

import os
from typing import Any

from plugins.catalog_common import assert_no_selection_write, enrichment_envelope


def predict_pl_fitness(
    *,
    smiles_list: list[str] | None = None,
    target_sequence: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """蛋白–配体 fitness 旁证。

    默认 stub：未设置 ``MOLMIND_AUROBIND_ENABLED=1`` 或缺少靶点序列时降级。
    真实对接推理留给后续接入上游权重；本仓只保证契约与主榜隔离。
    """
    smiles = list(smiles_list or [])
    enabled = os.environ.get("MOLMIND_AUROBIND_ENABLED", "").strip() in {"1", "true", "yes"}
    degraded: list[str] = []
    if not enabled:
        degraded.append("aurobind_not_enabled")
    if not (target_sequence or "").strip():
        degraded.append("aurobind_missing_target_sequence")
    if not smiles:
        degraded.append("aurobind_empty_smiles")

    if degraded:
        result = enrichment_envelope(
            tool="predict_pl_fitness",
            plugin="aurobind",
            ok=True,
            data={"scores": []},
            message="AuroBind 未就绪（需启用开关 + 靶点序列）；已降级，不影响主榜。",
            degraded=degraded,
            digest={"n_ligands": len(smiles), "gpu_required": True},
        )
        assert_no_selection_write(result)
        return result

    # Placeholder: real model call would live here. Still never writes selection.
    result = enrichment_envelope(
        tool="predict_pl_fitness",
        plugin="aurobind",
        ok=True,
        data={
            "scores": [
                {"smiles": s, "fitness": None, "note": "adapter_ready_awaiting_weights"}
                for s in smiles
            ]
        },
        message="AuroBind 适配器已接通（占位分数）；结果仅作旁证。",
        digest={"n_ligands": len(smiles), "mode": "placeholder"},
    )
    assert_no_selection_write(result)
    return result


def run_enrichment_pass(
    *,
    smiles_list: list[str] | None = None,
    target_sequence: str | None = None,
) -> dict[str, Any]:
    inner = predict_pl_fitness(
        smiles_list=smiles_list,
        target_sequence=target_sequence,
    )
    result = enrichment_envelope(
        tool="enrich_topn_with_aurobind",
        plugin="aurobind",
        ok=True,
        data=inner.get("data"),
        message=inner.get("message") or "",
        degraded=list(inner.get("degraded") or []),
        digest=dict(inner.get("digest") or {}),
    )
    assert_no_selection_write(result)
    return result
