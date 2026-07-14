"""可选本地 ML 头：ECFP k-NN（DILI / ADMET 代理）；无模型文件则跳过。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdkit import Chem

from packages.ml_optional.knn_model import KnnModel, load_knn_model

_CACHE: dict[str, "OptionalHeadsBundle"] = {}


@dataclass
class MLHeadResult:
    dili: float = 0.0
    admet: float = 0.0
    skipped: bool = True
    reason: str = "no_model"
    dili_neighbor: str | None = None
    admet_neighbor: str | None = None
    dili_sim: float = 0.0
    admet_sim: float = 0.0


@dataclass
class OptionalHeadsBundle:
    """已加载的模型包；可对任意分子预测。"""

    dili_model: KnnModel | None = None
    admet_model: KnnModel | None = None
    reason: str = "empty"
    placeholder_dili: float | None = None
    placeholder_admet: float | None = None

    @property
    def skipped(self) -> bool:
        return (
            self.dili_model is None
            and self.admet_model is None
            and self.placeholder_dili is None
        )

    def predict(self, mol: Chem.Mol | None) -> MLHeadResult:
        if self.placeholder_dili is not None:
            return MLHeadResult(
                dili=float(self.placeholder_dili),
                admet=float(self.placeholder_admet or 0.0),
                skipped=False,
                reason=self.reason,
            )
        if self.skipped or mol is None:
            return MLHeadResult(skipped=True, reason=self.reason)
        dili, dili_name, dili_sim = (0.0, None, 0.0)
        admet, admet_name, admet_sim = (0.0, None, 0.0)
        parts: list[str] = []
        if self.dili_model is not None:
            dili, dili_name, dili_sim = self.dili_model.predict(mol)
            parts.append(f"dili:{self.dili_model.version}")
        if self.admet_model is not None:
            admet, admet_name, admet_sim = self.admet_model.predict(mol)
            parts.append(f"admet:{self.admet_model.version}")
        return MLHeadResult(
            dili=dili,
            admet=admet,
            skipped=False,
            reason="+".join(parts) or self.reason,
            dili_neighbor=dili_name,
            admet_neighbor=admet_name,
            dili_sim=dili_sim,
            admet_sim=admet_sim,
        )


def _stable_key(manifest: dict[str, Any], root: Path) -> str:
    return f"{root.resolve()}|{json.dumps(manifest, sort_keys=True, ensure_ascii=False)}"


def load_optional_heads(manifest: dict[str, Any], *, model_dir: Path | None = None) -> MLHeadResult:
    """兼容旧测试：返回占位/跳过摘要（不针对具体分子）。

    真实打分请用 ``load_optional_heads_bundle(...).predict(mol)``。
    """
    bundle = load_optional_heads_bundle(manifest, model_dir=model_dir)
    if bundle.skipped:
        return MLHeadResult(skipped=True, reason=bundle.reason)
    if bundle.placeholder_dili is not None:
        return MLHeadResult(
            dili=float(bundle.placeholder_dili),
            admet=float(bundle.placeholder_admet or 0.0),
            skipped=False,
            reason=bundle.reason,
        )
    return MLHeadResult(skipped=False, reason=bundle.reason, dili=0.0, admet=0.0)


def load_optional_heads_bundle(
    manifest: dict[str, Any],
    *,
    model_dir: Path | None = None,
) -> OptionalHeadsBundle:
    models = manifest.get("models") or []
    if not models:
        return OptionalHeadsBundle(reason="empty_manifest")

    root = Path(model_dir) if model_dir is not None else Path(".")
    cache_key = _stable_key(manifest, root)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    dili_model: KnnModel | None = None
    admet_model: KnnModel | None = None
    placeholder_dili: float | None = None
    placeholder_admet: float | None = None
    reasons: list[str] = []

    for entry in models:
        rel = str(entry.get("path", ""))
        path = root / rel
        kind = str(entry.get("kind") or entry.get("role") or "").lower()
        if not path.is_file():
            continue

        # 测试用假模型：显式 placeholder 字段
        if entry.get("dili_placeholder") is not None or entry.get("admet_placeholder") is not None:
            placeholder_dili = float(entry.get("dili_placeholder", 0.2))
            placeholder_admet = float(entry.get("admet_placeholder", 0.15))
            reasons.append(f"placeholder:{path.name}")
            continue

        try:
            model = load_knn_model(path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError):
            continue

        if kind in {"dili", "dili_knn", "dili_ml"} or "dili" in path.name.lower():
            dili_model = model
            reasons.append(f"dili:{path.name}")
        elif kind in {"admet", "admet_proxy", "admet_ml"} or "admet" in path.name.lower():
            admet_model = model
            reasons.append(f"admet:{path.name}")
        else:
            dili_model = model
            reasons.append(f"dili:{path.name}")

    if dili_model is None and admet_model is None and placeholder_dili is None:
        bundle = OptionalHeadsBundle(reason="model_files_missing")
    else:
        bundle = OptionalHeadsBundle(
            dili_model=dili_model,
            admet_model=admet_model,
            placeholder_dili=placeholder_dili,
            placeholder_admet=placeholder_admet,
            reason="+".join(reasons) or "loaded",
        )
    _CACHE[cache_key] = bundle
    return bundle


def clear_heads_cache() -> None:
    _CACHE.clear()
