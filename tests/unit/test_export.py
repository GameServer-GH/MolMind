"""CSV 导出列契约。"""

from __future__ import annotations

from pathlib import Path
import json
import csv
import io

from packages.models import ScoreRecord
from services.pipeline import CSV_COLUMNS, run_pipeline
from plugins.molmind_core.scientific.pipeline.export import (
    export_nomination_csv,
    reserve_output_path,
    rows_from_top,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def _eligible_record(molecule_id: str, *, tier: str, rank: int) -> ScoreRecord:
    return ScoreRecord(
        molecule_id=molecule_id,
        smiles="CCO",
        inchikey=f"{molecule_id:0<14}-ABCDEFGHIJ-A"[:27],
        cas=None,
        scaffold_smiles="CCO",
        lipid_score=0.4,
        tox_risk=0.2,
        novelty_score=0.8,
        conf_e=0.5,
        final_score=0.6,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="proxy",
        tox_rationale="proxy",
        overall_reason="eligible",
        eligibility_status="eligible",
        nomination_tier=tier,
        primary_rank=rank if tier == "primary" else None,
        reserve_rank=rank if tier == "reserve" else None,
        replacement_for=(f"primary_slot_{((rank - 1) % 10) + 1}" if tier == "reserve" else ""),
    )


def test_csv_columns_include_run_metadata(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    run_pipeline(SAMPLE_SDF, out, mode="auto", top_n=5)
    header = out.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == ",".join(CSV_COLUMNS)
    assert "run_mode" in CSV_COLUMNS
    assert "config_hash" in CSV_COLUMNS
    assert "degraded_channels" in CSV_COLUMNS
    assert CSV_COLUMNS[:5] == ["排名", "化合物标识符", "降脂依据", "毒性判断", "排序理由"]
    assert "SI" not in CSV_COLUMNS
    assert "EC50" not in CSV_COLUMNS
    assert "effect_x_novelty" in CSV_COLUMNS
    assert "screening_concentration_um" in CSV_COLUMNS
    assert "viability_endpoint" in CSV_COLUMNS
    assert "submission_schema_version" in CSV_COLUMNS
    assert "identity_review_required" in CSV_COLUMNS
    assert "pubchem_raw_status" in CSV_COLUMNS


def test_submission_csv_canonicalizes_provider_status_and_preserves_raw(
    tmp_path: Path,
) -> None:
    result = run_pipeline(
        SAMPLE_SDF,
        tmp_path / "canonical-status.csv",
        mode="offline",
        top_n=1,
        write_mechanism=False,
    )
    molecule = result.top_molecules[0]
    molecule.evidence_source_audit = {
        **(molecule.evidence_source_audit or {}),
        "pubchem": {
            "status": "exact_hit",
            "hit_count": 1,
            "scored_hit_count": 0,
            "ranking_effect": "annotation_or_audit_only",
        },
    }
    row = rows_from_top(
        [molecule],
        mode="auto",
        config_hash=result.config.config_hash,
        degraded_channels=[],
    )[0]
    assert row["pubchem_query_status"] == "hit"
    assert row["pubchem_raw_status"] == "exact_hit"
    assert row["submission_scope"] == "pre_wet_lab_computational_nomination"


def test_pipeline_writes_candidate_scores_and_evidence_ledger(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    run_pipeline(SAMPLE_SDF, out, mode="offline", top_n=2, write_mechanism=False)
    scores = out.with_suffix(".candidate_scores.jsonl")
    ledger = out.with_suffix(".evidence_ledger.jsonl")
    citations = out.with_suffix(".citations.jsonl")
    selection_audit = out.with_suffix(".selection_audit.jsonl")
    reserve = out.with_suffix(".reserve.csv")
    assert scores.is_file()
    assert ledger.is_file()
    assert citations.is_file()
    assert selection_audit.is_file()
    assert reserve.is_file()
    score_rows = [json.loads(line) for line in scores.read_text(encoding="utf-8").splitlines()]
    ledger_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    audit_rows = [
        json.loads(line) for line in selection_audit.read_text(encoding="utf-8").splitlines()
    ]
    assert score_rows
    assert ledger_rows
    assert all("scientific_status" in row for row in score_rows)
    assert all("query_status" in row for row in ledger_rows)
    assert any(row.get("evidence_type") for row in ledger_rows) or any(
        row.get("evidence_type") == "query_audit" for row in ledger_rows
    )
    assert any(row.get("outcome") == "selected" for row in audit_rows)
    assert all("selection_factors" in row for row in audit_rows)
    assert all("effect_x_novelty" in row for row in score_rows)


def test_primary_and_reserve_exports_keep_frozen_lineage_and_bom(tmp_path: Path) -> None:
    primary = [_eligible_record(f"P{i:02}", tier="primary", rank=i) for i in range(1, 11)]
    reserve = [_eligible_record(f"R{i:02}", tier="reserve", rank=i) for i in range(1, 21)]
    run_id = "mm-frozen"
    input_sha256 = "input-hash"
    config_hash = "config-hash"
    primary_path = tmp_path / "library_nomination_top10.csv"
    reserve_path = reserve_output_path(primary_path)

    export_nomination_csv(
        primary,
        primary_path,
        mode="auto",
        config_hash=config_hash,
        degraded_channels=[],
        requested_top_n=10,
        run_id=run_id,
        input_sha256=input_sha256,
        selection_hash="primary-selection",
        nomination_tier="primary",
    )
    export_nomination_csv(
        reserve,
        reserve_path,
        mode="auto",
        config_hash=config_hash,
        degraded_channels=[],
        requested_top_n=20,
        run_id=run_id,
        input_sha256=input_sha256,
        selection_hash="reserve-selection",
        nomination_tier="reserve",
    )

    assert primary_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert reserve_path.read_bytes().startswith(b"\xef\xbb\xbf")
    primary_rows = list(csv.DictReader(io.StringIO(primary_path.read_text(encoding="utf-8-sig"))))
    reserve_rows = list(csv.DictReader(io.StringIO(reserve_path.read_text(encoding="utf-8-sig"))))
    assert len(primary_rows) == 10
    assert {row["nomination_tier"] for row in primary_rows} == {"primary"}
    assert {row["nomination_tier"] for row in reserve_rows} == {"reserve"}
    assert [int(row["reserve_rank"]) for row in reserve_rows] == list(range(1, 21))
    assert not ({row["molecule_id"] for row in primary_rows} & {row["molecule_id"] for row in reserve_rows})
    for rows in (primary_rows, reserve_rows):
        assert {row["run_id"] for row in rows} == {run_id}
        assert {row["input_sha256"] for row in rows} == {input_sha256}
        assert {row["config_hash"] for row in rows} == {config_hash}


def test_reserve_shortage_writes_auditable_note(tmp_path: Path) -> None:
    reserve = [_eligible_record(f"R{i:02}", tier="reserve", rank=i) for i in range(1, 4)]
    out = tmp_path / "library_nomination_reserve.csv"
    export_nomination_csv(
        reserve,
        out,
        mode="auto",
        config_hash="config-hash",
        degraded_channels=[],
        requested_top_n=20,
        run_id="mm-frozen",
        input_sha256="input-hash",
        selection_hash="reserve-selection",
        nomination_tier="reserve",
    )
    note = out.with_suffix(".note.txt").read_text(encoding="utf-8")
    assert "仅 3 个" in note
    assert "未临时重跑" in note
