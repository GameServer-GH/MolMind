"""可核验运行清单：输入、政策、环境、模型、证据与交付物摘要。"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from rdkit import rdBase

from packages.models import ScoreRecord
from services.ingest.cache import sha256_file
from services.pipeline.config_loader import (
    ALGORITHM_CONTRACT_VERSION,
    ROOT,
    SNAPSHOT_DIR,
    AppConfig,
)
from services.pipeline.run_identity import canonical_selection, selection_sha256


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
) -> Path:
    artifacts: dict[str, str] = {}
    candidates = [
        output_path,
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
    manifest = {
        "schema_version": "molmind-run-manifest-v2",
        "run_id": run_id,
        "selection_sha256": selection_hash,
        "reserve_selection_sha256": reserve_selection_hash
        or selection_sha256(list(reserve_molecules or [])),
        "ordered_reserve_candidates": canonical_selection(list(reserve_molecules or [])),
        "ordered_candidates": canonical_selection(top_molecules),
        "input": {
            "path": input_path.name,
            "sha256": sha256_file(input_path),
        },
        "config_hash": cfg.config_hash,
        "algorithm_contract_version": ALGORITHM_CONTRACT_VERSION,
        "assumption_policy_version": cfg.assumptions.get("version", "unversioned"),
        "snapshot_sha256": _hash_files(snapshot_files),
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
        ],
        "random_seed": cfg.seed,
        "started_at": started_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "runtime_seconds": round(runtime_seconds, 3),
        "artifacts": artifacts,
    }
    path = output_path.with_suffix(".run_manifest.json")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
