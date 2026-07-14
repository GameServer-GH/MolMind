"""GoldSet 回归断言（ADR-M14）。"""

from __future__ import annotations

from dataclasses import dataclass

from packages.chem_core import clamp, morgan_fp
from packages.goldset import GoldCase, GoldSet
from packages.models import MoleculeRecord
from rdkit import Chem
from rdkit.Chem import Descriptors, Lipinski

from services.evidence_facade.bundle import EvidenceBundle
from services.evidence_facade.facade import EvidenceFacade
from services.hard_filter import apply_hard_filters
from services.pipeline.config_loader import AppConfig
from services.ranker import score_molecule


@dataclass
class HarnessResult:
    passed: bool
    messages: list[str]


def _case_to_record(case: GoldCase) -> MoleculeRecord:
    mol = Chem.MolFromSmiles(case.smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id=f"GOLD_{case.name}",
        smiles=case.smiles,
        inchikey=case.inchikey,
        cas=case.cas,
        mw=float(Descriptors.MolWt(mol)),
        logp=float(Descriptors.MolLogP(mol)),
        hbd=int(Lipinski.NumHDonors(mol)),
        hba=int(Lipinski.NumHAcceptors(mol)),
        tpsa=float(Descriptors.TPSA(mol)),
        rotatable_bonds=int(Lipinski.NumRotatableBonds(mol)),
        aromatic_rings=int(Descriptors.NumAromaticRings(mol)),
        fp_bits=morgan_fp(mol),
    )


def run_goldset_harness(cfg: AppConfig, gold: GoldSet) -> HarnessResult:
    facade = EvidenceFacade(cfg)
    messages: list[str] = []
    ok = True
    tox_soft = float(cfg.gates["tox_soft"])
    tox_hard = float(cfg.gates["tox_hard"])

    for case in gold.false_positives:
        record = _case_to_record(case)
        ev = facade.query(inchikey=record.inchikey, cas=record.cas, smiles=record.smiles)
        scored = score_molecule(record, cfg, gold, ev)
        if scored.tox_risk < tox_soft:
            ok = False
            messages.append(
                f"FAIL FP {case.name}: R_tox={scored.tox_risk:.3f} < tox_soft={tox_soft}"
            )
        else:
            messages.append(f"OK FP {case.name}: R_tox={scored.tox_risk:.3f}")

    lipid_min = float(cfg.gates["lipid_min"])
    for case in gold.positives:
        record = _case_to_record(case)
        ev = EvidenceBundle()
        scored = score_molecule(record, cfg, gold, ev)
        if scored.lipid_score < lipid_min:
            ok = False
            messages.append(
                f"FAIL POS {case.name}: S_lipid={scored.lipid_score:.3f} < lipid_min={lipid_min}"
            )
        else:
            messages.append(f"OK POS {case.name}: S_lipid={scored.lipid_score:.3f}")

        filt = apply_hard_filters(record, cfg)
        if case.expected.get("pass_filter") and not filt.passed:
            messages.append(f"WARN POS {case.name} 未过硬过滤: {filt.reason}")

    risks = []
    for case in gold.all_cases():
        record = _case_to_record(case)
        scored = score_molecule(record, cfg, gold, EvidenceBundle())
        risks.append(scored.tox_risk)
    if len(risks) > 1:
        mean = sum(risks) / len(risks)
        var = sum((x - mean) ** 2 for x in risks) / len(risks)
        std = var**0.5
        if std < 0.05:
            ok = False
            messages.append(f"FAIL tox diversity on goldset std={std:.4f}")
        else:
            messages.append(f"OK goldset tox std={std:.4f}")

    _ = clamp, tox_hard
    return HarnessResult(passed=ok, messages=messages)
