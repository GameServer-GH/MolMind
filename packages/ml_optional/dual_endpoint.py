"""HepG2-FFA 脂质/活力双终点模型接口；无数据时显式不可用。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rdkit import Chem


REQUIRED_TRAINING_FIELDS = (
    "molecule_id",
    "standardized_smiles",
    "dose",
    "dose_unit",
    "treatment_time_hours",
    "lipid_response",
    "cell_viability_response",
    "batch_id",
    "vehicle_control_id",
)


@dataclass(frozen=True)
class DualEndpointPrediction:
    lipid_effect_probability: float | None = None
    viability_risk_probability: float | None = None
    lipid_uncertainty: float | None = None
    viability_uncertainty: float | None = None
    applicability: float = 0.0
    skipped: bool = True
    reason: str = "same_condition_training_data_unavailable"
    model_version: str = "unavailable"


class DualEndpointPredictor(Protocol):
    def predict(self, mol: Chem.Mol) -> DualEndpointPrediction: ...


@dataclass(frozen=True)
class UnavailableDualEndpointPredictor:
    reason: str = "same_condition_training_data_unavailable"

    def predict(self, mol: Chem.Mol) -> DualEndpointPrediction:
        del mol
        return DualEndpointPrediction(reason=self.reason)


def load_dual_endpoint_predictor(
    manifest: dict,
    *,
    model_dir: Path,
) -> DualEndpointPredictor:
    """只接受显式任务模型条目；绝不把 DILI 代理伪装成 HepG2 活力模型。"""
    task = manifest.get("task_specific_dual_endpoint") or {}
    status = str(task.get("status") or "unavailable")
    if status != "available":
        return UnavailableDualEndpointPredictor(
            reason=str(task.get("reason") or "same_condition_training_data_unavailable")
        )
    path = model_dir / str(task.get("path") or "")
    if not path.is_file():
        return UnavailableDualEndpointPredictor(reason="declared_task_model_file_missing")
    # 当前仓库没有经过独立验证的序列化格式；存在未知文件也不得静默加载。
    return UnavailableDualEndpointPredictor(reason="task_model_format_not_implemented_or_validated")
