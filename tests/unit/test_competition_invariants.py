"""核心不变量：双门控、交付一致性、审计与复现指纹。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from packages.goldset.hypothesis import infer_hypothesis_pathway
from packages.models import EligibilityPolicy, MoleculeAssessment
from rdkit import Chem
from services.eligibility import evaluate_candidate_eligibility
from services.ingest import parse_sdf_detailed
from services.mechanism import build_mechanism_markdown
from services.pipeline import load_config, run_pipeline, screen_sdf

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_eligibility_is_conjunctive_and_confidence_aware() -> None:
    policy = EligibilityPolicy(
        lipid_min=0.35,
        tox_hard=0.65,
        tox_nomination_max=0.45,
        min_toxicity_confidence=0.2,
    )
    eligible = evaluate_candidate_eligibility(
        MoleculeAssessment("ok", 0.8, 0.1, 0.8), policy
    )
    high_tox = evaluate_candidate_eligibility(
        MoleculeAssessment("toxic", 0.99, 0.65, 0.9), policy
    )
    weak_lipid = evaluate_candidate_eligibility(
        MoleculeAssessment("weak", 0.34, 0.1, 0.9), policy
    )
    uncertain = evaluate_candidate_eligibility(
        MoleculeAssessment("uncertain", 0.8, 0.1, 0.1), policy
    )
    soft_tox = evaluate_candidate_eligibility(
        MoleculeAssessment("soft_tox", 0.8, 0.5, 0.8), policy
    )
    assert eligible.status == "eligible"
    assert high_tox.status == "ineligible"
    assert weak_lipid.status == "ineligible"
    assert uncertain.status == "review_required"
    assert soft_tox.status == "review_required"


def test_final_top_csv_pdf_and_result_share_same_eligible_ids(tmp_path: Path) -> None:
    out = tmp_path / "nomination.csv"
    result = run_pipeline(SAMPLE_SDF, out, mode="offline", top_n=10)
    policy = result.config.gates
    ids = [m.molecule_id for m in result.top_molecules]
    assert ids
    assert len(ids) == len(set(ids))
    assert all(m.eligibility_status == "eligible" and not m.gated_out for m in result.top_molecules)
    assert all(m.lipid_score >= float(policy["lipid_min"]) for m in result.top_molecules)
    assert all(m.tox_risk < float(policy["tox_nomination_max"]) for m in result.top_molecules)
    assert all(
        m.toxicity_confidence >= float(policy["min_toxicity_confidence"])
        for m in result.top_molecules
    )

    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["化合物标识符"] for row in rows] == ids
    assert [int(row["排名"]) for row in rows] == list(range(1, len(ids) + 1))
    assert all(row["eligibility_status"] == "eligible" for row in rows)
    assert all(row["降脂依据"] and row["毒性判断"] and row["排序理由"] for row in rows)
    assert {row["run_id"] for row in rows} == {result.run_id}
    assert {row["input_sha256"] for row in rows} == {result.input_sha256}
    assert {row["selection_sha256"] for row in rows} == {result.selection_sha256}

    mechanism = build_mechanism_markdown(result.top_molecules)
    assert all(f"### 候选 {rank}. {mid}" in mechanism for rank, mid in enumerate(ids, 1))
    mechanism_artifact = out.with_suffix(".mechanism.md").read_text(encoding="utf-8")
    assert f"run_id={result.run_id}" in mechanism_artifact
    assert out.with_suffix(".mechanism.pdf").read_bytes().startswith(b"%PDF")
    graph_path = out.with_suffix(".mechanism_graph.json")
    graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph_payload["ranking_effect"] == "none"
    assert len(graph_payload["graphs"]) == len(ids)
    resource_path = out.with_suffix(".hepg2_ffa_resources.json")
    resource_payload = json.loads(resource_path.read_text(encoding="utf-8"))
    assert resource_payload["ranking_effect"] == "none"
    assert resource_payload["dual_endpoint_model_available"] is False
    manifest = json.loads(out.with_suffix(".run_manifest.json").read_text(encoding="utf-8"))
    assert graph_path.name in manifest["artifacts"]
    assert resource_path.name in manifest["artifacts"]
    assert manifest["run_id"] == result.run_id
    assert manifest["selection_sha256"] == result.selection_sha256
    assert [row["molecule_id"] for row in manifest["ordered_candidates"]] == ids


def test_every_input_record_has_machine_readable_screening_audit(tmp_path: Path) -> None:
    out = tmp_path / "nomination.csv"
    result = run_pipeline(SAMPLE_SDF, out, mode="offline", top_n=5)
    assert len(result.screening_audit) == result.raw_count
    assert all(item.status in {"passed", "rejected", "review_required", "invalid"} for item in result.screening_audit)
    assert all(item.reason for item in result.screening_audit)
    audit_path = out.with_suffix(".screening_audit.csv")
    assert audit_path.is_file()
    with audit_path.open(encoding="utf-8-sig", newline="") as fh:
        audit_rows = list(csv.DictReader(fh))
    assert len(audit_rows) == result.raw_count
    critic_path = out.with_suffix(".critic_audit.csv")
    assert critic_path.is_file()
    with critic_path.open(encoding="utf-8-sig", newline="") as fh:
        critic_rows = list(csv.DictReader(fh))
    assert critic_rows
    assert all(row["checks_performed"] and row["final_decision"] for row in critic_rows)


def test_standardization_and_duplicate_ids_are_stable(tmp_path: Path) -> None:
    sdf = tmp_path / "duplicates.sdf"
    writer = Chem.SDWriter(str(sdf))
    for smiles in ("CC(=O)[O-].[Na+]", "CCO"):
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        mol.SetProp("ID", "DUP")
        writer.write(mol)
    writer.close()

    parsed = parse_sdf_detailed(sdf)
    assert parsed.raw_count == 2
    assert [r.molecule_id for r in parsed.records] == ["DUP", "DUP__00002"]
    assert parsed.records[0].source_molecule_id == "DUP"
    assert "." not in parsed.records[0].smiles
    assert "fragment_parent" in parsed.records[0].standardization_steps


def test_snapshot_content_changes_config_hash(tmp_path: Path, monkeypatch) -> None:
    snap = tmp_path / "snapshot"
    snap.mkdir()
    path = snap / "frozen.jsonl"
    monkeypatch.setenv("EVIDENCE_SNAPSHOT_DIR", str(snap))
    path.write_text('{"inchikey":"A","score":0.1}\n', encoding="utf-8")
    first = load_config(mode="offline").config_hash
    path.write_text('{"inchikey":"A","score":0.2}\n', encoding="utf-8")
    second = load_config(mode="offline").config_hash
    assert first != second


def test_no_evidence_does_not_fabricate_specific_pathway() -> None:
    pathway, support = infer_hypothesis_pathway("CCO")
    assert pathway["id"] == "UNRESOLVED"
    assert "证据不足" in support


def test_same_input_config_and_seed_preserve_order() -> None:
    a = screen_sdf(SAMPLE_SDF, cfg=load_config(mode="offline", seed=42), top_n=10)
    b = screen_sdf(SAMPLE_SDF, cfg=load_config(mode="offline", seed=42), top_n=10)
    assert [m.molecule_id for m in a.top_molecules] == [m.molecule_id for m in b.top_molecules]
    assert a.to_csv_text() == b.to_csv_text()
    assert a.run_id == b.run_id
    assert a.selection_sha256 == b.selection_sha256
