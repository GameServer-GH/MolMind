from __future__ import annotations

import csv
import json
from pathlib import Path

from services.pipeline.run_diff import compare_nomination_csv, write_run_diff


def _write(path: Path, rows: list[tuple[str, float]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["排名", "化合物标识符", "molecule_id", "final_score", "run_id", "config_hash"])
        writer.writeheader()
        for rank, (molecule_id, score) in enumerate(rows, 1):
            writer.writerow({"排名": rank, "化合物标识符": molecule_id, "molecule_id": molecule_id, "final_score": score, "run_id": "run", "config_hash": "cfg"})


def test_run_diff_reports_entry_exit_and_rank_delta(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.csv"
    candidate = tmp_path / "candidate.csv"
    _write(baseline, [("A", 0.9), ("B", 0.8), ("C", 0.7)])
    _write(candidate, [("A", 0.91), ("C", 0.71), ("D", 0.69)])
    payload = compare_nomination_csv(baseline, candidate)
    assert payload["entered"] == ["D"]
    assert payload["exited"] == ["B"]
    assert payload["rank_deltas"][1]["molecule_id"] == "C"
    assert payload["rank_deltas"][1]["rank_delta"] == -1
    assert payload["candidate_monotonic_descending"]

    out = tmp_path / "diff.json"
    write_run_diff(baseline, candidate, out)
    assert json.loads(out.read_text())["schema_version"] == "molmind-run-diff-v1"

