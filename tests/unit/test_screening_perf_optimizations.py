"""纯性能优化回归：索引缓存、SMARTS 预编译、goldset 相似度复用不改排序语义。"""

from __future__ import annotations

from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from rdkit import Chem

from plugins.molmind_core.scientific.evidence_facade.facade import EvidenceFacade
from plugins.molmind_core.scientific.evidence_facade.index_cache import clear_index_cache
from plugins.molmind_core.scientific.hard_filter.filter import (
    _compiled_structural_alerts,
    apply_hard_filters,
)
from plugins.molmind_core.scientific.pipeline.config_loader import load_config
from plugins.molmind_core.scientific.ranker import score_molecule
from services.evidence_facade import EvidenceBundle


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


def test_evidence_facade_reuses_process_index_cache(tmp_path: Path) -> None:
    clear_index_cache()
    rows = [
        {
            "inchikey": "CACHEKEY",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.4,
            "confidence": 0.5,
            "evidence_id": "chembl:CACHE:lipid",
            "endpoint": "cellular_lipid_reduction",
            "direction": "supports",
            "evidence_role": "task_evidence",
            "query_status": "exact_hit",
        }
    ]
    path = tmp_path / "cache.jsonl"
    path.write_text(__import__("json").dumps(rows[0]) + "\n", encoding="utf-8")
    cfg = load_config(mode="offline")
    first = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    second = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    assert first._index is second._index
    assert first._epa_index is second._epa_index
    clear_index_cache()


def test_hard_filter_smarts_compile_cache_stable() -> None:
    cfg = load_config(mode="offline")
    first = apply_hard_filters(_record("CCO", "EtOH"), cfg)
    second = apply_hard_filters(_record("CCO", "EtOH2"), cfg)
    assert first.status == second.status
    info = _compiled_structural_alerts.cache_info()
    assert info.hits >= 1


def test_score_molecule_goldset_reuse_matches_baseline() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    record = _record("CCOc1ccccc1", "reuse")
    scored = score_molecule(record, cfg, gold, EvidenceBundle())
    assert 0.0 <= scored.novelty_score <= 1.0
    assert 0.0 <= scored.novelty_max_similarity <= 1.0
    assert "positive_similarity" in scored.lipid_parts
