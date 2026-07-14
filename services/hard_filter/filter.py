"""Hard Filter：Ro5 + 专家红线 + 警示结构。

HF contract lineage: YLuo / LJR
"""

from __future__ import annotations

from rdkit import Chem

from packages.chem_core import HARD_PATTERNS
from packages.models import FilterDecision, MoleculeRecord
from services.pipeline.config_loader import AppConfig


def apply_hard_filters(record: MoleculeRecord, cfg: AppConfig) -> FilterDecision:
    steps = cfg.filter_steps.get("steps", [])
    step_codes: list[str] = []

    ro5 = next((s for s in steps if s.get("id") == "lipinski_ro5"), {})
    params = ro5.get("params", {})
    max_mw = float(params.get("max_mw", 500.0))
    max_logp = float(params.get("max_logp", 5.0))
    max_hbd = int(params.get("max_hbd", 5))
    max_hba = int(params.get("max_hba", 10))

    if record.mw > max_mw:
        return FilterDecision(False, ["lipinski_ro5"], f"MW {record.mw:.1f} > {max_mw}")
    step_codes.append("lipinski_ro5")

    if record.logp > max_logp:
        return FilterDecision(False, step_codes, f"LogP {record.logp:.2f} > {max_logp}")
    if record.hbd > max_hbd:
        return FilterDecision(False, step_codes, f"HBD {record.hbd} > {max_hbd}")
    if record.hba > max_hba:
        return FilterDecision(False, step_codes, f"HBA {record.hba} > {max_hba}")

    red = next((s for s in steps if s.get("id") == "expert_redlines"), {})
    red_params = red.get("params", {})
    max_mw_hard = float(red_params.get("max_mw_hard", 600.0))
    if record.mw > max_mw_hard:
        return FilterDecision(False, step_codes + ["expert_redlines"], f"红线 MW>{max_mw_hard}")
    step_codes.append("expert_redlines")

    mol = Chem.MolFromSmiles(record.smiles)
    if mol is None:
        return FilterDecision(False, step_codes + ["structural_alerts"], "SMILES 无法解析")

    for name, pattern in HARD_PATTERNS:
        if mol.HasSubstructMatch(pattern):
            return FilterDecision(
                False,
                step_codes + ["structural_alerts"],
                f"硬过滤警示: {name}",
            )
    step_codes.append("structural_alerts")
    step_codes.append("basic_props")
    return FilterDecision(True, step_codes, "类药与红线通过")
