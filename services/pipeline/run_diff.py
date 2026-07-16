"""Deterministic candidate-set diff for ranking and evidence review."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _ids(rows: list[dict[str, str]]) -> list[str]:
    return [str(row.get("化合物标识符") or row.get("molecule_id") or "") for row in rows]


def compare_nomination_csv(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    baseline = _read_rows(baseline_path)
    candidate = _read_rows(candidate_path)
    baseline_ids = _ids(baseline)
    candidate_ids = _ids(candidate)
    baseline_set = set(baseline_ids)
    candidate_set = set(candidate_ids)
    shared = baseline_set & candidate_set
    baseline_rank = {mid: index + 1 for index, mid in enumerate(baseline_ids)}
    candidate_rank = {mid: index + 1 for index, mid in enumerate(candidate_ids)}
    baseline_scores = {
        mid: float(row.get("final_score") or 0.0)
        for mid, row in zip(baseline_ids, baseline)
    }
    candidate_scores = {
        mid: float(row.get("final_score") or 0.0)
        for mid, row in zip(candidate_ids, candidate)
    }
    top_k = min(len(baseline_ids), len(candidate_ids), 10)
    baseline_top = set(baseline_ids[:top_k])
    candidate_top = set(candidate_ids[:top_k])
    union = baseline_top | candidate_top
    score_deltas = [
        {
            "molecule_id": mid,
            "baseline_rank": baseline_rank[mid],
            "candidate_rank": candidate_rank[mid],
            "rank_delta": candidate_rank[mid] - baseline_rank[mid],
            "baseline_final_score": baseline_scores.get(mid, 0.0),
            "candidate_final_score": candidate_scores.get(mid, 0.0),
            "score_delta": round(candidate_scores.get(mid, 0.0) - baseline_scores.get(mid, 0.0), 8),
        }
        for mid in sorted(shared, key=lambda item: (candidate_rank[item], item))
    ]
    return {
        "schema_version": "molmind-run-diff-v1",
        "baseline": {
            "path": str(baseline_path),
            "run_id": baseline[0].get("run_id", "") if baseline else "",
            "config_hash": baseline[0].get("config_hash", "") if baseline else "",
            "candidate_count": len(baseline_ids),
            "ordered_ids": baseline_ids,
        },
        "candidate": {
            "path": str(candidate_path),
            "run_id": candidate[0].get("run_id", "") if candidate else "",
            "config_hash": candidate[0].get("config_hash", "") if candidate else "",
            "candidate_count": len(candidate_ids),
            "ordered_ids": candidate_ids,
        },
        "top_k": top_k,
        "top_k_jaccard": round(len(baseline_top & candidate_top) / len(union), 8) if union else 1.0,
        "shared_count": len(shared),
        "entered": sorted(candidate_set - baseline_set),
        "exited": sorted(baseline_set - candidate_set),
        "rank_deltas": score_deltas,
        "baseline_monotonic_descending": all(
            baseline_scores[a] >= baseline_scores[b]
            for a, b in zip(baseline_ids, baseline_ids[1:])
        ),
        "candidate_monotonic_descending": all(
            candidate_scores[a] >= candidate_scores[b]
            for a, b in zip(candidate_ids, candidate_ids[1:])
        ),
    }


def write_run_diff(baseline_path: Path, candidate_path: Path, output_path: Path) -> Path:
    payload = compare_nomination_csv(baseline_path, candidate_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path

