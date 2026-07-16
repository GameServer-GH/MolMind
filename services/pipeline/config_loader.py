"""加载 YAML 配置并计算稳定 config_hash（阶段 1 桩；语义随后续清单项演进）。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "evidence_snapshot"
ALGORITHM_CONTRACT_VERSION = "competition-eligibility-v10"

REQUIRED_YAML = (
    "rank_weights.yaml",
    "filter_steps.yaml",
    "lipid_steps.yaml",
    "tox_steps.yaml",
    "assumptions.yaml",
)


class ConfigLoadError(FileNotFoundError):
    """配置文件缺失或路径无效。"""


@dataclass
class AppConfig:
    raw: dict[str, Any]
    filter_steps: dict[str, Any]
    lipid_steps: dict[str, Any]
    tox_steps: dict[str, Any]
    model_manifest: dict[str, Any]
    assumptions: dict[str, Any]
    mode: str
    seed: int
    config_hash: str
    degraded_channels: list[str] = field(default_factory=list)
    # P0-C：ML 邻居命中 run 级统计（不把 no_neighbor 刷进逐分子 degraded）
    ml_predict_calls: int = 0
    ml_no_neighbor_hits: int = 0

    @property
    def weights(self) -> dict[str, float]:
        return dict(self.raw["weights"])

    @property
    def gates(self) -> dict[str, float]:
        return dict(self.raw["gates"])

    @property
    def lipid_fuse(self) -> dict[str, float]:
        return dict(self.raw["lipid_fuse"])

    @property
    def tox_fuse(self) -> dict[str, Any]:
        return dict(self.raw["tox_fuse"])

    @property
    def diversity(self) -> dict[str, Any]:
        return dict(self.raw["diversity"])

    @property
    def evidence(self) -> dict[str, Any]:
        return dict(self.raw.get("evidence", {}))

    @property
    def critic(self) -> dict[str, Any]:
        return dict(self.raw.get("critic", {}))

    @property
    def quality_gates(self) -> dict[str, Any]:
        return dict(self.raw.get("quality_gates", {}))

    @property
    def novelty(self) -> dict[str, Any]:
        return dict(self.raw.get("novelty", {}))

    @property
    def robustness(self) -> dict[str, Any]:
        return dict(self.raw.get("robustness", {}))

    @property
    def feature_cache(self) -> dict[str, Any]:
        return dict(self.raw.get("feature_cache", {}))

    @property
    def top_n(self) -> int:
        return int(self.raw.get("top_n", 10))

    @property
    def top_k_for_critic(self) -> int:
        return int(self.raw.get("top_k_for_critic", 30))

    @property
    def reserve_n(self) -> int:
        return int(self.raw.get("reserve_n", 20))

    @property
    def competition_scoring(self) -> dict[str, Any]:
        return dict(self.raw.get("competition_scoring", {}))

    @property
    def ml_enabled(self) -> bool:
        return bool(self.raw.get("ml", {}).get("enabled", True)) and bool(
            self.model_manifest.get("models")
        )

    @property
    def allow_live_evidence(self) -> bool:
        # 交付默认必须可复现：auto 只读冻结快照；显式 online 才访问可变外部服务。
        return self.mode == "online"

    @property
    def llm(self) -> dict[str, Any]:
        return dict(self.raw.get("llm", {}))

    @property
    def llm_mechanism_enabled(self) -> bool:
        """机制润色开关（不改排名）。实际调用仍需 API Key；无 Key 则模板降级。"""
        llm = self.raw.get("llm", {})
        return bool(llm.get("enabled")) and bool(llm.get("mechanism_pdf", True))

    @property
    def llm_critic_enabled(self) -> bool:
        """证据约束 Critic 仅读本 Run 证据；交付默认关，且不应改榜。"""
        llm = self.raw.get("llm", {})
        return bool(llm.get("enabled")) and bool(llm.get("critic_enabled"))

    @property
    def llm_critic_affects_ranking(self) -> bool:
        llm = self.raw.get("llm", {})
        return bool(llm.get("critic_affects_ranking", False))

    def mark_degraded(self, channel: str) -> None:
        if channel not in self.degraded_channels:
            self.degraded_channels.append(channel)

    def note_ml_predict(self, *, had_neighbor: bool) -> None:
        """记录一次 ML 预测；无合格邻居时累加 no_neighbor（run 级汇总用）。"""
        self.ml_predict_calls += 1
        if not had_neighbor:
            self.ml_no_neighbor_hits += 1

    def finalize_ml_run_stats(self) -> str | None:
        """返回 diagnostics 备注；不把 no_neighbor 写入 degraded_channels。"""
        if self.ml_predict_calls <= 0:
            return None
        return (
            f"ml_neighbors: no_hit={self.ml_no_neighbor_hits}/{self.ml_predict_calls}"
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigLoadError(f"缺少配置文件: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置必须是 mapping: {path}")
    return data


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigLoadError(f"缺少配置文件: {path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _ljr_cfg_digest(payload: dict[str, Any]) -> str:
    """Stable config fingerprint (LJR digest helper)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _files_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((p for p in paths if p.is_file()), key=lambda p: str(p)):
        digest.update(str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _validate_config(
    rank: dict[str, Any],
    filter_steps: dict[str, Any],
    model_manifest: dict[str, Any],
    assumptions: dict[str, Any],
) -> None:
    weights = rank.get("weights") or {}
    required_weights = {"lipid", "tox_safety", "novelty", "evidence_confidence"}
    if set(weights) != required_weights:
        raise ValueError(f"weights 必须且只能包含 {sorted(required_weights)}")
    numeric_weights = {k: float(v) for k, v in weights.items()}
    if any(v < 0 or v > 1 for v in numeric_weights.values()):
        raise ValueError("weights 必须位于 [0, 1]")
    if abs(sum(numeric_weights.values()) - 1.0) > 1e-9:
        raise ValueError("weights 必须归一化且总和为 1.0")

    gates = rank.get("gates") or {}
    for name in (
        "tox_hard",
        "tox_soft",
        "tox_nomination_max",
        "lipid_min",
        "min_toxicity_confidence",
        "min_safety_evidence_coverage",
        "local_toxicity_proxy_coverage",
        "tox_uncertainty_penalty",
        "low_confidence_tox_margin",
    ):
        value = float(gates.get(name, 0.0))
        if value < 0 or value > 1:
            raise ValueError(f"gates.{name} 必须位于 [0, 1]")
    if float(gates.get("tox_soft", 0.0)) > float(gates.get("tox_hard", 1.0)):
        raise ValueError("gates.tox_soft 不得高于 gates.tox_hard")
    if float(gates.get("tox_nomination_max", 0.45)) > float(gates.get("tox_hard", 1.0)):
        raise ValueError("gates.tox_nomination_max 不得高于 gates.tox_hard")

    top_n = int(rank.get("top_n", 10))
    if top_n < 1 or top_n > 50:
        raise ValueError("top_n 须在 1–50 之间")
    if int(rank.get("top_k_for_critic", top_n)) < top_n:
        raise ValueError("top_k_for_critic 不得小于 top_n")
    reserve_n = int(rank.get("reserve_n", 20))
    if reserve_n < 0 or reserve_n > 200:
        raise ValueError("reserve_n 须在 0–200 之间")
    scoring = rank.get("competition_scoring") or {}
    if scoring.get("enabled", True):
        if scoring.get("normalization") != "percentile_rank":
            raise ValueError("competition_scoring.normalization 当前仅支持 percentile_rank")
        if scoring.get("primary") not in {"product", "equal_mean"}:
            raise ValueError("competition_scoring.primary 须为 product|equal_mean")

    steps = filter_steps.get("steps") or []
    alert_step = next((s for s in steps if s.get("id") == "structural_alerts"), {})
    allowed = {"hard_exclusion", "soft_penalty", "review_required", "information_only"}
    from rdkit import Chem

    seen_rules: set[str] = set()
    for rule in alert_step.get("rules") or []:
        rule_id = str(rule.get("id") or "")
        classification = str(rule.get("classification") or "")
        smarts = str(rule.get("smarts") or "")
        if not rule_id or rule_id in seen_rules:
            raise ValueError(f"结构警示 rule id 缺失或重复: {rule_id!r}")
        seen_rules.add(rule_id)
        if classification not in allowed:
            raise ValueError(f"结构警示 {rule_id} classification 无效: {classification}")
        if not smarts or Chem.MolFromSmarts(smarts) is None:
            raise ValueError(f"结构警示 {rule_id} SMARTS 无效: {smarts}")

    if model_manifest.get("models") and not model_manifest.get("version"):
        raise ValueError("model_manifest 含模型时必须提供 version")

    assumption_rows = assumptions.get("assumptions") or []
    by_id = {str(row.get("id")): row for row in assumption_rows if isinstance(row, dict)}
    for required in (
        "screening_concentration",
        "viability_endpoint",
        "viability_proxy",
        "viability_secondary_endpoint",
        "parallel_endpoint_required",
        "lipid_hit_threshold",
        "toxicity_nomination_proxy",
        "novelty_proxy",
        "unresolved_mechanism",
        "delivery_platform",
    ):
        if required not in by_id:
            raise ValueError(f"assumptions.yaml 缺少安全默认: {required}")
    if abs(float(by_id["viability_proxy"]["value"]) - float(gates["viability_proxy"])) > 1e-9:
        raise ValueError("viability_proxy 在 assumptions.yaml 与 rank_weights.yaml 不一致")
    if abs(float(by_id["screening_concentration"]["value"]) - 10.0) > 1e-9:
        raise ValueError("screening_concentration 必须为已确认的 10 μM")
    if str(by_id["viability_endpoint"]["value"]) != "CCK-8":
        raise ValueError("viability_endpoint 必须为已确认的 CCK-8")
    if by_id["parallel_endpoint_required"].get("value") is not True:
        raise ValueError("parallel_endpoint_required 必须为 true")
    if abs(
        float(by_id["toxicity_nomination_proxy"]["value"])
        - float(gates["tox_nomination_max"])
    ) > 1e-9:
        raise ValueError("tox_nomination_max 在 assumptions.yaml 与 rank_weights.yaml 不一致")

    diversity = rank.get("diversity") or {}
    for name in ("max_pairwise_tanimoto", "similarity_cluster_threshold", "mmr_lambda"):
        value = float(diversity.get(name, 0.0))
        if value < 0 or value > 1:
            raise ValueError(f"diversity.{name} 必须位于 [0, 1]")
    if int(diversity.get("max_per_similarity_cluster", 1)) < 1:
        raise ValueError("diversity.max_per_similarity_cluster 必须 >= 1")


def load_config(
    *,
    mode: str | None = None,
    config_dir: Path | None = None,
    seed: int | None = None,
    use_snapshot: bool | None = None,
) -> AppConfig:
    cfg_dir = Path(config_dir) if config_dir is not None else CONFIG_DIR
    if not cfg_dir.is_dir():
        raise ConfigLoadError(f"配置目录不存在: {cfg_dir}")

    for name in REQUIRED_YAML:
        if not (cfg_dir / name).is_file():
            raise ConfigLoadError(f"缺少配置文件: {cfg_dir / name}")

    rank = _read_yaml(cfg_dir / "rank_weights.yaml")
    filter_steps = _read_yaml(cfg_dir / "filter_steps.yaml")
    lipid_steps = _read_yaml(cfg_dir / "lipid_steps.yaml")
    tox_steps = _read_yaml(cfg_dir / "tox_steps.yaml")
    assumptions = _read_yaml(cfg_dir / "assumptions.yaml")
    manifest_path = cfg_dir / "model_manifest.json"
    model_manifest = _read_json(manifest_path) if manifest_path.is_file() else {"models": []}
    _validate_config(rank, filter_steps, model_manifest, assumptions)

    resolved_mode = (mode or os.environ.get("MOLMIND_MODE") or rank.get("mode_default", "auto")).lower()
    if resolved_mode not in {"auto", "online", "offline"}:
        raise ValueError(f"未知 mode: {resolved_mode}（允许 auto|online|offline）")

    resolved_seed = int(seed if seed is not None else rank.get("seed", 42))
    # Quality-Max 是冻结主路径，必须读取同一份证据快照。此前 API 在
    # config_hash 计算后再修改 use_snapshot，导致同一 hash 可对应两套排名。
    # 现在运行开关在 hash 前解析；auto 强制开启，online/offline 仍允许显式关闭。
    effective_use_snapshot = True if resolved_mode == "auto" else (
        True if use_snapshot is None else bool(use_snapshot)
    )
    evidence = dict(rank.get("evidence") or {})
    evidence["use_snapshot"] = effective_use_snapshot
    if effective_use_snapshot:
        evidence["prefer_snapshot"] = True
    else:
        evidence["prefer_snapshot"] = False
    rank = {
        **rank,
        "seed": resolved_seed,
        "mode_default": resolved_mode,
        "evidence": evidence,
    }

    snapshot_digest = ""
    snap = Path(os.environ.get("EVIDENCE_SNAPSHOT_DIR", SNAPSHOT_DIR))
    if effective_use_snapshot and snap.is_dir():
        snapshot_digest = _files_digest(list(snap.glob("*.jsonl")))

    model_paths = [ROOT / str(m.get("path") or "") for m in model_manifest.get("models") or []]
    reference_paths = [
        *sorted((DATA_DIR / "goldset").glob("*.yaml")),
        DATA_DIR / "reference" / "nafld_pathways.yaml",
    ]
    algorithm_paths = [
        ROOT / "services" / "hard_filter" / "filter.py",
        ROOT / "services" / "scorer_lipid" / "scorer.py",
        ROOT / "services" / "scorer_tox" / "scorer.py",
        ROOT / "services" / "ranker" / "ranker.py",
        ROOT / "services" / "critic" / "critic.py",
        ROOT / "services" / "eligibility" / "policy.py",
        ROOT / "services" / "novelty" / "scorer.py",
        ROOT / "packages" / "chem_core" / "core.py",
    ]

    hash_payload = {
        "rank": rank,
        "filter_steps": filter_steps,
        "lipid_steps": lipid_steps,
        "tox_steps": tox_steps,
        "model_manifest": model_manifest,
        "assumptions": assumptions,
        "mode": resolved_mode,
        "snapshot_digest": snapshot_digest,
        "model_artifact_digest": _files_digest(model_paths),
        "reference_digest": _files_digest(reference_paths),
        "algorithm_digest": _files_digest(algorithm_paths),
        "algorithm_contract_version": ALGORITHM_CONTRACT_VERSION,
    }
    return AppConfig(
        raw=rank,
        filter_steps=filter_steps,
        lipid_steps=lipid_steps,
        tox_steps=tox_steps,
        model_manifest=model_manifest,
        assumptions=assumptions,
        mode=resolved_mode,
        seed=resolved_seed,
        config_hash=_ljr_cfg_digest(hash_payload),
    )
