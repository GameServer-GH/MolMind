"""阶段 7：DILIrank / NAFLDkb / LLM Critic（默认关，可选开启）。"""

from __future__ import annotations

from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import CriticAction, MoleculeRecord, ScoreRecord
from rdkit import Chem
from services.critic.critic import apply_llm_critic_suggestions, llm_critic_stub
from services.evidence_facade.facade import EvidenceFacade
from services.evidence_facade.local_tables import load_dilirank, query_dilirank
from services.pipeline import screen_sdf
from services.pipeline.config_loader import load_config
from services.ranker import score_molecule
from services.evidence_facade.bundle import EvidenceBundle

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def _record(smiles: str, mid: str) -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
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


def test_dilirank_raises_amiodarone_tox() -> None:
    index = load_dilirank(ROOT / "data" / "reference" / "dilirank.csv")
    assert index.size >= 1
    amio = next(c for c in load_goldset().false_positives if "Amiodarone" in c.name)
    hit = query_dilirank(index, inchikey=amio.inchikey, smiles=amio.smiles, sim_threshold=0.70)
    assert hit is not None
    assert hit.score >= 0.5

    cfg = load_config(mode="offline")
    # 临时开启本地表 + dili adapter（不写回磁盘）
    cfg.raw["evidence"]["local_tables"]["enabled"] = True
    cfg.raw["evidence"]["adapter_flags"]["dili_table_v1"] = {
        "enabled": True,
        "ranking_weight": 1.0,
    }
    cfg.raw["evidence"]["adapters"] = list(
        set(cfg.raw["evidence"].get("adapters") or []) | {"dili_table_v1"}
    )
    facade = EvidenceFacade(cfg)
    gold = load_goldset()
    with_table = facade.query(inchikey=amio.inchikey, cas=amio.cas, smiles=amio.smiles, allow_live=False)
    scored = score_molecule(_record(amio.smiles, "Amio"), cfg, gold, with_table)
    baseline = score_molecule(_record(amio.smiles, "Amio"), cfg, gold, EvidenceBundle())
    assert scored.tox_risk >= baseline.tox_risk


def test_nafld_default_off_and_gate_when_on() -> None:
    cfg = load_config(mode="offline")
    assert cfg.evidence.get("local_tables", {}).get("enabled") is False
    flags = cfg.evidence.get("adapter_flags") or {}
    assert flags.get("nafldkb_v1", {}).get("enabled") is False

    # 打开后：禁止大量「低 S_lipid + 仅靠证据」进 Top（用断言门槛）
    cfg.raw["evidence"]["local_tables"]["enabled"] = True
    cfg.raw["evidence"]["adapter_flags"]["nafldkb_v1"] = {
        "enabled": True,
        "ranking_weight": 1.0,
    }
    result = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=10)
    low_lipid_evidence_only = 0
    for mol in result.top_molecules:
        if mol.lipid_score < 0.40 and mol.conf_e > 0.2 and mol.lipid_parts.get("rule", 1) < 0.2:
            low_lipid_evidence_only += 1
    assert low_lipid_evidence_only <= 3


def test_llm_critic_discards_without_evidence_ids() -> None:
    cfg = load_config(mode="offline")
    cfg.raw["llm"] = {"enabled": True, "critic_enabled": True, "critic_affects_ranking": True}
    mol = ScoreRecord(
        molecule_id="X",
        smiles="CCO",
        inchikey="",
        cas=None,
        scaffold_smiles="",
        lipid_score=0.5,
        tox_risk=0.2,
        novelty_score=0.5,
        conf_e=0.0,
        final_score=0.5,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
    )
    actions = llm_critic_stub([mol], cfg)
    # 无 evidence_ids → 无 keep/drop 动作
    assert actions == []
    illegal = [CriticAction(action="drop", molecule_id="X", reason="no ids", evidence_ids=[])]
    kept = apply_llm_critic_suggestions(
        [mol], illegal, affect_ranking=True, allowed_evidence_ids=set()
    )
    assert kept[0].molecule_id == "X"
