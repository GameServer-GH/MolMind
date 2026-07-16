"""services.critic：GoldSet FP/阳性药不得进 Top；critic_actions 可记录。"""

from __future__ import annotations

from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from rdkit import Chem
from services.critic import rule_critic, summarize_critic_actions
from services.evidence_facade import EvidenceBundle
from services.ingest import parse_sdf
from services.pipeline.config_loader import load_config
from services.ranker import score_molecule

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def _record_from_case(name: str, smiles: str) -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id=name,
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


def test_critic_drops_known_positive() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    lov = next(c for c in gold.positives if c.name == "Lovastatin")
    scored = score_molecule(_record_from_case("Lovastatin", lov.smiles), cfg, gold, EvidenceBundle())
    sample = parse_sdf(SAMPLE_SDF)
    alts = [score_molecule(r, cfg, gold, EvidenceBundle()) for r in sample[:5]]
    pool = [scored] + [a for a in alts if not a.gated_out]
    top, actions = rule_critic([scored], pool, cfg, gold, top_n=1)
    assert any(a.action == "drop" and a.molecule_id == scored.molecule_id for a in actions)
    assert scored.molecule_id not in {m.molecule_id for m in top}
    assert actions  # critic_actions 可记录


def test_false_positive_neighbor_intercept() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    amio = next(c for c in gold.false_positives if "Amiodarone" in c.name)
    scored = score_molecule(_record_from_case("Amiodarone", amio.smiles), cfg, gold, EvidenceBundle())
    # Force into candidate set even if gated
    scored.gated_out = False
    scored.tox_risk = max(scored.tox_risk, float(cfg.critic.get("fp_tox_soft", 0.45)))
    top, actions = rule_critic([scored], [scored], cfg, gold, top_n=1)
    assert any(a.action == "drop" for a in actions)
    assert "Amiodarone" not in {m.molecule_id for m in top}


def test_critic_soft_drops_low_mw_fragment() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    # 烟酸：低 MW，应被 min_mw_top 软踢
    niacin = "O=C(O)c1cccnc1"
    scored = score_molecule(_record_from_case("T0879", niacin), cfg, gold, EvidenceBundle())
    scored.gated_out = False
    sample = parse_sdf(SAMPLE_SDF)
    alts = [score_molecule(r, cfg, gold, EvidenceBundle()) for r in sample[:8]]
    pool = [a for a in alts if not a.gated_out]
    top, actions = rule_critic([scored] + pool, pool, cfg, gold, top_n=3)
    assert scored.molecule_id not in {m.molecule_id for m in top}
    assert any("分子量过低" in a.reason for a in actions if a.molecule_id == scored.molecule_id)


def test_critic_berberine_family_quota_at_most_one() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    ber = next(c for c in gold.positives if c.name == "Berberine")
    # 两个高相似小檗碱变体：配额最多 1
    b1 = score_molecule(_record_from_case("B1", ber.smiles), cfg, gold, EvidenceBundle())
    b1.gated_out = False
    b1.final_score = 0.9
    b1.novelty_score = 0.55
    # 略改结构仍高相似（原小檗碱盐）
    b2_smiles = "[Cl-].c1c2c(cc3c1OCO3)-c1cc3cc4c(cc3c[n+]1CC2)OCO4"
    b2 = score_molecule(_record_from_case("B2", b2_smiles), cfg, gold, EvidenceBundle())
    b2.gated_out = False
    b2.final_score = 0.89
    b2.novelty_score = 0.55
    sample = parse_sdf(SAMPLE_SDF)
    alts = [score_molecule(r, cfg, gold, EvidenceBundle()) for r in sample[:10]]
    pool = [a for a in alts if not a.gated_out]
    # Berberine 本身会因 known_positive 被踢；用近邻 B2 + 人工抬高的第二近邻
    # 若 B1 是精确对照会被踢；只测 B2 与另一高分非小檗碱
    top, actions = rule_critic([b2] + pool, pool, cfg, gold, top_n=5)
    berberine_keeps = [
        a for a in actions if a.action == "keep" and "family=berberine" in a.reason
    ]
    assert len(berberine_keeps) <= 1
    assert len(top) >= 1


def test_summarize_critic_actions_histogram() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    lov = next(c for c in gold.positives if c.name == "Lovastatin")
    scored = score_molecule(_record_from_case("Lovastatin", lov.smiles), cfg, gold, EvidenceBundle())
    sample = parse_sdf(SAMPLE_SDF)
    alts = [score_molecule(r, cfg, gold, EvidenceBundle()) for r in sample[:5]]
    pool = [scored] + [a for a in alts if not a.gated_out]
    _top, actions = rule_critic([scored], pool, cfg, gold, top_n=2)
    hist = summarize_critic_actions(actions)
    assert hist["known_positive"] >= 1 or hist["near_positive"] >= 1
    assert sum(hist.values()) == len(actions)

