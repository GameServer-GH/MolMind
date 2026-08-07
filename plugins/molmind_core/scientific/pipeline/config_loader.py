"""加载 YAML 配置并计算稳定 config_hash（阶段 1 桩；语义随后续清单项演进）。"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = REPO_ROOT
CONFIG_DIR = ROOT / "configs"
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "evidence_snapshot"
ALGORITHM_CONTRACT_VERSION = "competition-eligibility-v13"

REQUIRED_YAML = (
    "rank_weights.yaml",
    "filter_steps.yaml",
    "lipid_steps.yaml",
    "tox_steps.yaml",
    "assumptions.yaml",
)
OPTIONAL_NOMINATION_YAML = (
    "clinical_exclusions.yaml",
    "nomination_review.yaml",
)

# Canonical scientific implementation files whose content must influence every
# config_hash.  ``services/`` modules are compatibility shims and therefore
# cannot serve as the algorithm digest boundary.
ALGORITHM_PATHS = (
    "plugins/molmind_core/scientific/hard_filter/filter.py",
    "plugins/molmind_core/scientific/scorer_lipid/scorer.py",
    "plugins/molmind_core/scientific/scorer_tox/scorer.py",
    "plugins/molmind_core/scientific/ranker/ranker.py",
    "plugins/molmind_core/scientific/critic/critic.py",
    "plugins/molmind_core/scientific/eligibility/policy.py",
    "plugins/molmind_core/scientific/novelty/scorer.py",
    "plugins/molmind_core/scientific/nomination/review.py",
    "plugins/molmind_core/scientific/evidence_facade/bundle.py",
    "plugins/molmind_core/scientific/evidence_facade/facade.py",
    "plugins/molmind_core/scientific/pipeline/runner.py",
    "packages/chem_core/core.py",
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
    def clinical_exclusions(self) -> dict[str, Any]:
        return dict(self.raw.get("clinical_exclusions", {}))

    @property
    def nomination_review(self) -> dict[str, Any]:
        return dict(self.raw.get("nomination_review", {}))

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
        """联网补证据开关；默认关。默认可复现路径只读冻结快照。"""
        return bool(self.raw.get("evidence", {}).get("allow_live", False))

    @property
    def use_snapshot(self) -> bool:
        return bool(self.raw.get("evidence", {}).get("use_snapshot", True))

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
        """证据约束 Critic 仅读本 Run 证据；默认关闭，且不应改榜。"""
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


def _path_mtime_fingerprint(paths: list[Path]) -> tuple[tuple[str, int | None, int | None], ...]:
    parts: list[tuple[str, int | None, int | None]] = []
    for path in sorted({Path(p) for p in paths}, key=lambda item: str(item)):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        try:
            stat = resolved.stat()
            parts.append((str(resolved), int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            parts.append((str(resolved), None, None))
    return tuple(parts)


_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict[tuple[Any, ...], tuple[tuple[Any, ...], AppConfig]] = {}


def _clone_app_config(cfg: AppConfig) -> AppConfig:
    return AppConfig(
        raw=copy.deepcopy(cfg.raw),
        filter_steps=copy.deepcopy(cfg.filter_steps),
        lipid_steps=copy.deepcopy(cfg.lipid_steps),
        tox_steps=copy.deepcopy(cfg.tox_steps),
        model_manifest=copy.deepcopy(cfg.model_manifest),
        assumptions=copy.deepcopy(cfg.assumptions),
        mode=cfg.mode,
        seed=cfg.seed,
        config_hash=cfg.config_hash,
        degraded_channels=[],
        ml_predict_calls=0,
        ml_no_neighbor_hits=0,
    )


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

    epa = (rank.get("evidence") or {}).get("epa_ctx") or {}
    epa_stage = int(epa.get("integration_stage", 0))
    if epa_stage not in {0, 1, 2}:
        raise ValueError("evidence.epa_ctx.integration_stage 须为 0|1|2")
    for name in ("active_hit_threshold", "max_risk_score", "risk_confidence"):
        value = float(epa.get(name, 0.0))
        if value < 0 or value > 1:
            raise ValueError(f"evidence.epa_ctx.{name} 必须位于 [0, 1]")
    if epa_stage == 2 and not bool(epa.get("require_exact_identity_for_stage2", True)):
        raise ValueError(
            "EPA 阶段二必须要求精确身份；如需放宽请先完成结构审计并修改实现契约"
        )

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
        "runtime_platform",
    ):
        if required not in by_id and required == "delivery_platform":
            # Backward-compatible alias; prefer runtime_platform.
            continue
        if required == "runtime_platform" and required not in by_id and "delivery_platform" in by_id:
            continue
        if required not in by_id and required != "delivery_platform":
            raise ValueError(f"assumptions.yaml 缺少安全默认: {required}")
    if "runtime_platform" not in by_id and "delivery_platform" not in by_id:
        raise ValueError("assumptions.yaml 缺少安全默认: runtime_platform")
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


def _parse_bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 须为 true|false（收到 {raw!r}）")


def resolve_runtime_switches(
    *,
    mode: str | None = None,
    allow_live: bool | None = None,
    use_snapshot: bool | None = None,
    mode_default: str = "auto",
) -> tuple[str, bool, bool]:
    """Normalize Quality-Max runtime switches.

    Canonical mode is always ``auto`` (Quality-Max). Legacy ``online`` /
    ``offline`` remain accepted as aliases that only seed ``allow_live`` when
    the explicit live switch is omitted.
    """
    requested = (mode or os.environ.get("MOLMIND_MODE") or mode_default or "auto").lower().strip()
    if requested in {"quality-max", "quality_max", "qmax"}:
        requested = "auto"
    if requested not in {"auto", "online", "offline"}:
        raise ValueError(
            f"未知 mode: {requested}（允许 auto|online|offline；"
            "online/offline 仅为兼容别名，正式路径为 Quality-Max + 快照/联网开关）"
        )

    env_live = _parse_bool_env("MOLMIND_ALLOW_LIVE")
    if allow_live is None:
        allow_live = env_live
    if allow_live is None:
        allow_live = requested == "online"

    env_snap = _parse_bool_env("MOLMIND_USE_SNAPSHOT")
    if use_snapshot is None:
        use_snapshot = env_snap
    if use_snapshot is None:
        use_snapshot = True

    return "auto", bool(allow_live), bool(use_snapshot)


def load_config(
    *,
    mode: str | None = None,
    config_dir: Path | None = None,
    seed: int | None = None,
    use_snapshot: bool | None = None,
    allow_live: bool | None = None,
    epa_stage: int | None = None,
) -> AppConfig:
    cfg_dir = Path(config_dir) if config_dir is not None else CONFIG_DIR
    if not cfg_dir.is_dir():
        raise ConfigLoadError(f"配置目录不存在: {cfg_dir}")

    for name in REQUIRED_YAML:
        if not (cfg_dir / name).is_file():
            raise ConfigLoadError(f"缺少配置文件: {cfg_dir / name}")

    snap = Path(os.environ.get("EVIDENCE_SNAPSHOT_DIR", SNAPSHOT_DIR))
    cache_key = (
        str(cfg_dir.resolve()),
        mode,
        seed,
        use_snapshot,
        allow_live,
        epa_stage,
        os.environ.get("MOLMIND_EPA_STAGE"),
        os.environ.get("MOLMIND_USE_SNAPSHOT"),
        os.environ.get("MOLMIND_ALLOW_LIVE"),
        os.environ.get("EVIDENCE_SNAPSHOT_DIR"),
    )
    fingerprint_paths = [
        *(cfg_dir / name for name in REQUIRED_YAML),
        cfg_dir / "clinical_exclusions.yaml",
        cfg_dir / "nomination_review.yaml",
        cfg_dir / "model_manifest.json",
        *sorted((DATA_DIR / "goldset").glob("*.yaml")),
        DATA_DIR / "reference" / "nafld_pathways.yaml",
        *(ROOT / relative_path for relative_path in ALGORITHM_PATHS),
    ]
    if snap.is_dir():
        fingerprint_paths.extend(sorted(snap.glob("*.jsonl")))
    fingerprint = _path_mtime_fingerprint(fingerprint_paths)
    with _CONFIG_CACHE_LOCK:
        hit = _CONFIG_CACHE.get(cache_key)
        if hit is not None and hit[0] == fingerprint:
            return _clone_app_config(hit[1])

    rank = _read_yaml(cfg_dir / "rank_weights.yaml")
    filter_steps = _read_yaml(cfg_dir / "filter_steps.yaml")
    lipid_steps = _read_yaml(cfg_dir / "lipid_steps.yaml")
    tox_steps = _read_yaml(cfg_dir / "tox_steps.yaml")
    assumptions = _read_yaml(cfg_dir / "assumptions.yaml")
    clinical_exclusions_path = cfg_dir / "clinical_exclusions.yaml"
    nomination_review_path = cfg_dir / "nomination_review.yaml"
    clinical_exclusions = (
        _read_yaml(clinical_exclusions_path) if clinical_exclusions_path.is_file() else {}
    )
    nomination_review = (
        _read_yaml(nomination_review_path) if nomination_review_path.is_file() else {}
    )
    manifest_path = cfg_dir / "model_manifest.json"
    model_manifest = _read_json(manifest_path) if manifest_path.is_file() else {"models": []}
    _validate_config(rank, filter_steps, model_manifest, assumptions)

    resolved_mode, effective_allow_live, effective_use_snapshot = resolve_runtime_switches(
        mode=mode,
        allow_live=allow_live,
        use_snapshot=use_snapshot,
        mode_default=str(rank.get("mode_default", "auto")),
    )
    resolved_seed = int(seed if seed is not None else rank.get("seed", 42))
    evidence = dict(rank.get("evidence") or {})
    epa = dict(evidence.get("epa_ctx") or {})
    if epa_stage is None:
        env_stage = os.environ.get("MOLMIND_EPA_STAGE")
        epa_stage = int(env_stage) if env_stage not in (None, "") else None
    if epa_stage is not None:
        if int(epa_stage) not in {0, 1, 2}:
            raise ValueError("epa_stage 须为 0|1|2")
        epa["integration_stage"] = int(epa_stage)
        evidence["epa_ctx"] = epa
    evidence["use_snapshot"] = effective_use_snapshot
    evidence["allow_live"] = effective_allow_live
    if effective_use_snapshot:
        evidence["prefer_snapshot"] = True
    else:
        evidence["prefer_snapshot"] = False
    rank = {
        **rank,
        "seed": resolved_seed,
        "mode_default": resolved_mode,
        "evidence": evidence,
        "clinical_exclusions": clinical_exclusions,
        "nomination_review": nomination_review,
    }

    snapshot_digest = ""
    if effective_use_snapshot and snap.is_dir():
        snapshot_digest = _files_digest(list(snap.glob("*.jsonl")))

    model_paths = [ROOT / str(m.get("path") or "") for m in model_manifest.get("models") or []]
    reference_paths = [
        *sorted((DATA_DIR / "goldset").glob("*.yaml")),
        DATA_DIR / "reference" / "nafld_pathways.yaml",
    ]
    algorithm_paths = [ROOT / relative_path for relative_path in ALGORITHM_PATHS]

    hash_payload = {
        "rank": rank,
        "filter_steps": filter_steps,
        "lipid_steps": lipid_steps,
        "tox_steps": tox_steps,
        "model_manifest": model_manifest,
        "assumptions": assumptions,
        "clinical_exclusions": clinical_exclusions,
        "nomination_review": nomination_review,
        "mode": resolved_mode,
        "allow_live": effective_allow_live,
        "use_snapshot": effective_use_snapshot,
        "snapshot_digest": snapshot_digest,
        "model_artifact_digest": _files_digest(model_paths),
        "reference_digest": _files_digest(reference_paths),
        "algorithm_digest": _files_digest(algorithm_paths),
        "algorithm_contract_version": ALGORITHM_CONTRACT_VERSION,
    }
    loaded = AppConfig(
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
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[cache_key] = (fingerprint, loaded)
    return _clone_app_config(loaded)
