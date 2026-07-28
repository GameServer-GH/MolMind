"""可核验运行清单：输入、政策、环境、模型、证据与导出摘要。"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from rdkit import rdBase

from packages.models import ScoreRecord
from plugins.molmind_core.scientific.ingest.cache import sha256_file
from plugins.molmind_core.scientific.pipeline.config_loader import (
    ALGORITHM_CONTRACT_VERSION,
    ROOT,
    SNAPSHOT_DIR,
    AppConfig,
)
from plugins.molmind_core.scientific.pipeline.run_identity import canonical_selection, selection_sha256


def _hash_files(paths: list[Path]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted((p for p in paths if p.is_file()), key=lambda item: str(item)):
        digest.update(str(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_run_manifest(
    *,
    input_path: Path,
    output_path: Path,
    cfg: AppConfig,
    started_at: datetime,
    runtime_seconds: float,
    run_id: str,
    selection_hash: str,
    top_molecules: list[ScoreRecord],
    reserve_molecules: list[ScoreRecord] | None = None,
    reserve_selection_hash: str = "",
    algorithmic_selection_hash: str = "",
    algorithmic_top_molecules: list[ScoreRecord] | None = None,
    nomination_review_actions: list[dict[str, object]] | None = None,
) -> Path:
    artifacts: dict[str, str] = {}
    candidates = [
        output_path,
        output_path.with_suffix(".algorithmic.csv"),
        output_path.with_suffix(".nomination_review.jsonl"),
        output_path.with_suffix(".mechanism.md"),
        output_path.with_suffix(".mechanism.html"),
        output_path.with_suffix(".mechanism.pdf"),
        output_path.with_suffix(".screening_audit.csv"),
        output_path.with_suffix(".critic_audit.csv"),
        output_path.with_suffix(".rank_robustness.json"),
        output_path.with_suffix(".reserve.csv"),
        output_path.with_suffix(".mechanism_graph.json"),
        output_path.with_suffix(".hepg2_ffa_resources.json"),
    ]
    for artifact in candidates:
        if artifact.is_file():
            artifacts[artifact.name] = sha256_file(artifact)

    model_artifacts: dict[str, str] = {}
    for entry in cfg.model_manifest.get("models") or []:
        path = ROOT / str(entry.get("path") or "")
        if path.is_file():
            model_artifacts[str(entry.get("version") or path.name)] = sha256_file(path)

    snapshot_dir = Path(os.environ.get("EVIDENCE_SNAPSHOT_DIR", SNAPSHOT_DIR))
    snapshot_files = list(snapshot_dir.glob("*.jsonl")) if snapshot_dir.is_dir() else []
    epa_cfg = cfg.evidence.get("epa_ctx") or {}
    epa_stage = int(epa_cfg.get("integration_stage", 0))
    clinical_cfg = cfg.clinical_exclusions
    review_cfg = cfg.nomination_review
    algorithmic_top = list(algorithmic_top_molecules or top_molecules)
    algorithmic_hash = algorithmic_selection_hash or selection_hash
    manifest = {
        "schema_version": "molmind-run-manifest-v3",
        "run_id": run_id,
        "selection_sha256": selection_hash,
        "algorithmic_selection_sha256": algorithmic_hash,
        "reserve_selection_sha256": reserve_selection_hash
        or selection_sha256(list(reserve_molecules or [])),
        "ordered_candidates": canonical_selection(top_molecules),
        "ordered_algorithmic_candidates": canonical_selection(algorithmic_top),
        "ordered_reserve_candidates": canonical_selection(list(reserve_molecules or [])),
        "nomination_review": {
            "enabled": bool(review_cfg.get("enabled", True)),
            "require_input_match": bool(
                review_cfg.get(
                    "require_input_match",
                    bool(review_cfg.get("applies_to_input_sha256")),
                )
            ),
            "applies_to_input_sha256": list(
                review_cfg.get("applies_to_input_sha256") or []
                if isinstance(review_cfg.get("applies_to_input_sha256"), list)
                else (
                    [review_cfg.get("applies_to_input_sha256")]
                    if review_cfg.get("applies_to_input_sha256")
                    else []
                )
            ),
            "input_matched": all(
                row.get("input_matched", True) for row in (nomination_review_actions or [])
            )
            if nomination_review_actions
            else True,
            "review_applied": any(
                row.get("review_applied", row.get("applied"))
                for row in (nomination_review_actions or [])
            )
            if nomination_review_actions
            else False,
            "actions": list(nomination_review_actions or []),
            "seat_changes": sum(
                1
                for row in (nomination_review_actions or [])
                if row.get("action") == "drop_from_primary" and row.get("applied")
            ),
        },
        "clinical_exclusions": {
            "enabled": bool(clinical_cfg.get("enabled", True)),
            "exclusion_ids": [
                str(row.get("id") or "")
                for row in (clinical_cfg.get("exclusions") or [])
                if isinstance(row, dict)
            ],
        },
        "input": {
            "path": input_path.name,
            "sha256": sha256_file(input_path),
        },
        "config_hash": cfg.config_hash,
        "algorithm_contract_version": ALGORITHM_CONTRACT_VERSION,
        "assumption_policy_version": cfg.assumptions.get("version", "unversioned"),
        "snapshot_sha256": _hash_files(snapshot_files),
        "epa_ctx": {
            "integration_stage": epa_stage,
            "ranking_effect": "none" if epa_stage <= 1 else "cytotox_risk_only",
            "mapping_paths": list(epa_cfg.get("mapping_paths") or []),
            "risk_summary_paths": list(epa_cfg.get("risk_summary_paths") or []),
            "assay_qc_paths": list(epa_cfg.get("assay_qc_paths") or []),
        },
        "model_artifacts": model_artifacts,
        "python_version": platform.python_version(),
        "rdkit_version": rdBase.rdkitVersion,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python_implementation": platform.python_implementation(),
        },
        "container_image_digest": os.environ.get("MOLMIND_IMAGE_DIGEST", "not_available"),
        "command": [
            sys.executable,
            "-m",
            "apps.cli.main",
            "--input",
            input_path.name,
            "--output",
            str(output_path),
            "--mode",
            cfg.mode,
            "--epa-stage",
            str(epa_stage),
        ],
        "random_seed": cfg.seed,
        "started_at": started_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "runtime_seconds": round(runtime_seconds, 3),
        "artifacts": artifacts,
    }
    path = output_path.with_suffix(".run_manifest.json")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
