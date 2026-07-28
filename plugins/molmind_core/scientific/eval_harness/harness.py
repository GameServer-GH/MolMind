"""GoldSet 回归断言（ADR-M14）。"""

from __future__ import annotations

from dataclasses import dataclass

from packages.chem_core import clamp, morgan_fp
from packages.goldset import GoldCase, GoldSet, leave_one_case_out
from packages.models import MoleculeRecord
from rdkit import Chem
from rdkit.Chem import Descriptors, Lipinski

from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle
from plugins.molmind_core.scientific.evidence_facade.facade import EvidenceFacade
from plugins.molmind_core.scientific.hard_filter import apply_hard_filters
from plugins.molmind_core.scientific.pipeline.config_loader import AppConfig
from plugins.molmind_core.scientific.ranker import score_molecule


@dataclass
class HarnessResult:
    passed: bool
    messages: list[str]
    protocol: str = "leave-one-reference-out"


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
    messages.append("INFO protocol=leave-one-reference-out; regression reference, not independent test")
    ok = True
    tox_soft = float(cfg.gates["tox_soft"])
    tox_hard = float(cfg.gates["tox_hard"])
    min_std_tox = float(cfg.quality_gates.get("min_std_tox", 0.05))
    if tox_hard >= 1.0:
        ok = False
        messages.append("FAIL FP CONFIG: tox_hard>=1.0 使毒性门槛失去区分力")

    for case in gold.false_positives:
        record = _case_to_record(case)
        # Evaluation is a frozen/offline comparison surface.  Explicit live
        # enrichment must be queried and frozen before it can enter a run.
        ev = facade.query(
            inchikey=record.inchikey,
            cas=record.cas,
            smiles=record.smiles,
            allow_live=False,
        )
        loo_gold = leave_one_case_out(gold, case)
        scored = score_molecule(
            record,
            cfg,
            loo_gold,
            ev,
            excluded_reference_names={case.name},
        )
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
        loo_gold = leave_one_case_out(gold, case)
        scored = score_molecule(record, cfg, loo_gold, ev)
        if scored.lipid_score < lipid_min:
            messages.append(
                f"WARN LOO POS {case.name}: S_lipid={scored.lipid_score:.3f} < "
                f"lipid_min={lipid_min}; proxy recall limitation, not an independent-test failure"
            )
        else:
            messages.append(f"OK POS {case.name}: S_lipid={scored.lipid_score:.3f}")

        filt = apply_hard_filters(record, cfg)
        if case.expected.get("pass_filter") and not filt.passed:
            messages.append(f"WARN POS {case.name} 未过硬过滤: {filt.reason}")

    risks = []
    for case in gold.all_cases():
        record = _case_to_record(case)
        scored = score_molecule(
            record,
            cfg,
            leave_one_case_out(gold, case),
            EvidenceBundle(),
        )
        risks.append(scored.tox_risk)
    if len(risks) > 1:
        mean = sum(risks) / len(risks)
        var = sum((x - mean) ** 2 for x in risks) / len(risks)
        std = var**0.5
        if std < min_std_tox:
            messages.append(
                f"WARN TOX_STD goldset std={std:.4f} < legacy min_std_tox={min_std_tox}; "
                "risk dispersion is not treated as scientific accuracy"
            )
        else:
            messages.append(f"OK goldset tox std={std:.4f} (min_std_tox={min_std_tox})")

    _ = clamp, tox_hard
    return HarnessResult(passed=ok, messages=messages)
