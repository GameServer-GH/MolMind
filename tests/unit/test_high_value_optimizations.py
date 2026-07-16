"""高价值优化回归：语义隔离、透明代理、稳健性与显式降级。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdkit import Chem

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import leave_one_case_out, load_goldset, max_similarity
from packages.ml_optional import load_dual_endpoint_predictor
from packages.models import EvidenceHit, MoleculeAssessment, MoleculeRecord, ScoreRecord
from services.eligibility import evaluate_candidate_eligibility, policy_from_config
from services.evidence_facade import EvidenceBundle, EvidenceFacade
from services.ingest import load_feature_cache, parse_sdf_detailed, save_feature_cache
from services.mechanism import build_mechanism_markdown
from services.pipeline.config_loader import ROOT, load_config
from services.ranker import analyze_rank_robustness, apply_scaffold_diversity, score_molecule
from services.scorer_tox import score_tox


def _record(smiles: str, mid: str) -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    mol = Chem.MolFromSmiles(smiles)
    assert desc is not None and mol is not None
    return MoleculeRecord(
        molecule_id=mid,
        smiles=smiles,
        inchikey=Chem.MolToInchiKey(mol) or "",
        cas=None,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )


def _score_record(smiles: str, mid: str, score: float) -> ScoreRecord:
    record = _record(smiles, mid)
    return ScoreRecord(
        molecule_id=mid,
        smiles=smiles,
        inchikey=record.inchikey,
        cas=None,
        scaffold_smiles="",
        lipid_score=0.6,
        tox_risk=0.2,
        novelty_score=0.7,
        conf_e=0.2,
        final_score=score,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
        toxicity_confidence=0.5,
        toxicity_evidence_coverage=0.5,
        safety_clearance_confidence=0.5,
        eligibility_status="eligible",
        fp_bits=record.fp_bits,
    )


def test_legacy_presence_and_miss_are_audit_only(tmp_path: Path) -> None:
    rows = [
        {
            "inchikey": "LEGACY",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "novelty",
            "score": 0.55,
            "confidence": 0.4,
            "evidence_id": "chembl:1:present",
            "payload": {"lipid_hits": 0},
        },
        {
            "inchikey": "LEGACY",
            "adapter_id": "bake_miss_v1",
            "query_type": "novelty",
            "score": 0.5,
            "confidence": 0.05,
            "evidence_id": "bake_miss:LEGACY",
            "payload": {},
        },
    ]
    path = tmp_path / "legacy.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    bundle = EvidenceFacade(load_config(mode="offline"), snapshot_dir=tmp_path).query(
        inchikey="LEGACY", cas=None, smiles="CCO", allow_live=False
    )
    assert not bundle.novelty
    assert bundle.conf_e == 0.0
    assert [hit.evidence_role for hit in bundle.annotation] == ["annotation_only"]
    assert [hit.evidence_role for hit in bundle.query_audit] == ["query_audit"]


def test_toxicity_risk_signal_does_not_raise_safety_clearance() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    low = _record("O=C(O)c1ccccc1", "low")
    high_case = next(case for case in gold.false_positives if case.name == "Amiodarone")
    high = _record(high_case.smiles, "high")
    _risk_low, heads_low, *_ = score_tox(low, cfg, gold, EvidenceBundle())
    _risk_high, heads_high, *_ = score_tox(high, cfg, gold, EvidenceBundle())
    assert heads_low["evidence_coverage"] == pytest.approx(0.0)
    assert heads_low["safety_clearance_confidence"] == pytest.approx(0.0)
    assert heads_low["proxy_coverage"] == pytest.approx(0.20)
    assert heads_high["risk_signal_confidence"] >= heads_low["risk_signal_confidence"]
    assert heads_high["safety_clearance_confidence"] <= heads_low["safety_clearance_confidence"]


def test_low_confidence_toxicity_uses_conservative_nomination_bound() -> None:
    cfg = load_config(mode="offline")
    policy = policy_from_config(cfg.gates)
    decision = evaluate_candidate_eligibility(
        MoleculeAssessment(
            molecule_id="borderline",
            lipid_score=0.6,
            toxicity_score=0.43,
            toxicity_confidence=0.2,
            toxicity_evidence_coverage=0.2,
            safety_clearance_confidence=0.2,
            toxicity_upper_bound=0.48,
        ),
        policy,
    )
    assert decision.status == "review_required"
    assert any("conservative_R_tox 0.480" in reason for reason in decision.reasons)


def test_novelty_ignores_database_presence_and_uses_frozen_reference() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    case = gold.positives[0]
    record = _record(case.smiles, "known")
    annotation_like = EvidenceBundle(
        novelty=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="novelty",
                score=0.99,
                confidence=0.99,
                evidence_id="legacy:presence",
                evidence_role="annotation_only",
            )
        ]
    )
    empty = score_molecule(record, cfg, gold, EvidenceBundle())
    annotated = score_molecule(record, cfg, gold, annotation_like)
    assert annotated.novelty_score == empty.novelty_score == pytest.approx(0.0)
    assert annotated.conf_e == empty.conf_e == 0.0
    assert annotated.novelty_reference_version == "goldset_relevant_lipid_v1"


def test_similarity_portfolio_avoids_near_duplicate_when_alternative_exists() -> None:
    candidates = [
        _score_record("CCOc1ccccc1", "A", 0.90),
        _score_record("CCCOc1ccccc1", "A_near", 0.89),
        _score_record("NCC(=O)O", "B", 0.70),
    ]
    selected = apply_scaffold_diversity(
        candidates,
        top_n=2,
        max_per_scaffold=2,
        redundancy_lambda=0.05,
        max_pairwise_similarity=0.60,
        similarity_cluster_threshold=0.50,
        max_per_similarity_cluster=1,
        mmr_lambda=0.85,
    )
    assert [item.molecule_id for item in selected] == ["A", "B"]
    assert selected[1].internal_nearest_similarity <= 0.60


def test_rank_robustness_is_deterministic_and_audit_only() -> None:
    cfg = load_config(mode="offline")
    candidates = [
        _score_record("CCOc1ccccc1", "A", 0.80),
        _score_record("NCC(=O)O", "B", 0.70),
    ]
    before = [item.final_score for item in candidates]
    first = analyze_rank_robustness(candidates, cfg, top_n=1)
    second = analyze_rank_robustness(candidates, cfg, top_n=1)
    assert first == second
    assert [item.final_score for item in candidates] == before
    assert all(0.0 <= float(row["inclusion_frequency"]) <= 1.0 for row in first)


def test_goldset_leave_one_out_removes_self_and_near_duplicate() -> None:
    gold = load_goldset()
    case = gold.positives[0]
    loo = leave_one_case_out(gold, case)
    similarity, name = max_similarity(case.fp_bits, loo.positives)
    assert name != case.name
    assert similarity < 0.98


def test_mechanism_labels_proxy_and_unresolved_plan() -> None:
    mol = _score_record("CCO", "UNRESOLVED_CANDIDATE", 0.7)
    text = build_mechanism_markdown(
        [mol], assumptions=load_config(mode="offline").assumptions
    )
    assert "非官方" in text
    assert "有效命中（赛题口径）" not in text
    assert "L5-UNRESOLVED" in text
    assert "无偏解析" in text
    assert "仅表示通过项目配置" in text
    assert "候选级证据覆盖" in text
    assert "候选级引用清单" in text


def test_dual_endpoint_model_is_explicitly_unavailable() -> None:
    cfg = load_config(mode="offline")
    predictor = load_dual_endpoint_predictor(cfg.model_manifest, model_dir=ROOT)
    prediction = predictor.predict(Chem.MolFromSmiles("CCO"))
    assert prediction.skipped is True
    assert prediction.lipid_effect_probability is None
    assert "data" in prediction.reason.lower() or "unavailable" in prediction.reason.lower()


def test_scientific_status_does_not_promote_annotation_to_activity() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    record = _record("CCOc1ccccc1", "ANNOTATION_ONLY")
    annotation = EvidenceBundle(
        annotation=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="annotation",
                score=0.0,
                confidence=0.0,
                evidence_id="chembl:annotation",
                evidence_role="annotation_only",
                query_status="annotation_only",
                evidence_type="identity_annotation",
            )
        ]
    )
    scored = score_molecule(record, cfg, gold, annotation)
    assert scored.scientific_status == "proxy_only"
    assert scored.claim_ceiling == "proxy_nomination"
    assert "lipid_activity" in scored.audit_missing
    assert "safety_clearance" in scored.audit_missing
    assert annotation.lipid_query_status == "not_queried"
    assert annotation.novelty_score == pytest.approx(0.0)


def test_chembl_annotation_does_not_drive_lipid_query_status() -> None:
    bundle = EvidenceBundle(
        annotation=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="annotation",
                score=0.0,
                confidence=0.0,
                evidence_id="chembl:present",
                evidence_role="annotation_only",
                query_status="annotation_only",
                evidence_type="identity_annotation",
            )
        ],
        lipid=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="lipid",
                score=0.5,
                confidence=0.6,
                evidence_id="chembl:lipid",
                evidence_role="task_evidence",
                direction="supports",
                query_status="exact_hit",
                evidence_type="endpoint_evidence",
            )
        ],
    )
    assert bundle.lipid_query_status == "exact_hit"
    alone = EvidenceBundle(
        annotation=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="annotation",
                score=0.0,
                confidence=0.0,
                evidence_id="chembl:present",
                evidence_role="annotation_only",
                query_status="annotation_only",
            )
        ]
    )
    assert alone.lipid_query_status == "not_queried"
    assert alone.conf_e == pytest.approx(0.0)


def test_ml_proxy_clearance_does_not_raise_safety_clearance() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    # Benign-ish fragment: may get model applicability / proxy clearance, never external safety.
    low = _record("CCO", "ethanol_proxy")
    _risk, heads, *_ = score_tox(low, cfg, gold, EvidenceBundle())
    assert heads["safety_clearance_confidence"] == pytest.approx(0.0)
    assert heads.get("proxy_clearance_confidence", 0.0) >= 0.0
    assert "proxy_clearance_confidence" in heads


def test_bundle_lineage_and_citations_populated(tmp_path: Path) -> None:
    rows = [
        {
            "inchikey": "LINEAGEKEY",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.4,
            "confidence": 0.5,
            "evidence_id": "chembl:LINEAGE:lipid",
            "endpoint": "cellular_lipid_reduction",
            "direction": "supports",
            "evidence_role": "task_evidence",
            "query_status": "exact_hit",
            "adapter_version": "chembl_lipid_v3",
            "source_version": "chembl_lipid_v3",
            "payload": {
                "chembl_id": "CHEMBL1",
                "structured_hits": [
                    {
                        "standard_value": "10",
                        "standard_units": "uM",
                        "assay_description": "lipid droplet reduction",
                    }
                ],
            },
            "retrieved_at": "2026-07-15T00:00:00+00:00",
        }
    ]
    path = tmp_path / "lineage.jsonl"
    path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    bundle = EvidenceFacade(load_config(mode="offline"), snapshot_dir=tmp_path).query(
        inchikey="LINEAGEKEY", cas=None, smiles="CCO", allow_live=False
    )
    assert bundle.normalized_inchikey == "LINEAGEKEY"
    assert bundle.input_structure_hash
    assert bundle.queried_at
    assert bundle.source_versions.get("chembl_lipid_v1")
    assert all(hit.evidence_type == "endpoint_evidence" for hit in bundle.lipid)
    cfg = load_config(mode="offline")
    gold = load_goldset()
    scored = score_molecule(_record("CCO", "LINEAGE"), cfg, gold, bundle)
    assert scored.citations
    assert scored.citations[0].accession == "CHEMBL1"
    assert scored.citations[0].value == "10"
    assert scored.citations[0].unit == "uM"
    assert scored.selection_factors.get("eligibility")
    assert scored.input_structure_hash == bundle.input_structure_hash


def test_identity_review_forces_review_required_eligibility() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    record = _record("CCO", "IDENTITY_AMBIG")
    evidence = EvidenceBundle(
        lipid=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="lipid",
                score=0.8,
                confidence=0.8,
                evidence_id="chembl:IDENTITY:lipid",
                evidence_role="task_evidence",
                direction="supports",
                query_status="exact_hit",
                evidence_type="endpoint_evidence",
            )
        ],
        query_audit=[
            EvidenceHit(
                adapter_id="pubchem_tox_v1",
                query_type="query_audit",
                score=0.0,
                confidence=0.0,
                evidence_id="pubchem:identity_review:x",
                evidence_role="query_audit",
                query_status="identity_review_required",
                evidence_type="query_audit",
            )
        ],
    )
    scored = score_molecule(record, cfg, gold, evidence)
    assert scored.scientific_status == "identity_review_required"
    assert scored.eligibility_status == "review_required"
    assert scored.gated_out is True
    assert "identity_review_required" in scored.eligibility_reasons


def test_feature_cache_roundtrip_without_pickle(tmp_path: Path) -> None:
    result = parse_sdf_detailed(ROOT / "data" / "sample.sdf")
    path = tmp_path / "features.json.gz"
    save_feature_cache(path, result, metadata={"schema_version": "test"})
    loaded = load_feature_cache(path)
    assert loaded is not None
    assert [item.molecule_id for item in loaded.records] == [
        item.molecule_id for item in result.records
    ]
    assert [item.smiles for item in loaded.records] == [item.smiles for item in result.records]
    assert path.read_bytes()[:2] == b"\x1f\x8b"
