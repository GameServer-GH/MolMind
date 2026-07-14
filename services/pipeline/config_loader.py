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

REQUIRED_YAML = (
    "rank_weights.yaml",
    "filter_steps.yaml",
    "lipid_steps.yaml",
    "tox_steps.yaml",
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
    mode: str
    seed: int
    config_hash: str
    degraded_channels: list[str] = field(default_factory=list)

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
    def tox_fuse(self) -> dict[str, float]:
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
    def top_n(self) -> int:
        return int(self.raw.get("top_n", 10))

    @property
    def top_k_for_critic(self) -> int:
        return int(self.raw.get("top_k_for_critic", 30))

    @property
    def ml_enabled(self) -> bool:
        return bool(self.raw.get("ml", {}).get("enabled", True)) and bool(
            self.model_manifest.get("models")
        )

    @property
    def allow_live_evidence(self) -> bool:
        return self.mode in {"auto", "online"}

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


def load_config(
    *,
    mode: str | None = None,
    config_dir: Path | None = None,
    seed: int | None = None,
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
    manifest_path = cfg_dir / "model_manifest.json"
    model_manifest = _read_json(manifest_path) if manifest_path.is_file() else {"models": []}

    resolved_mode = (mode or os.environ.get("MOLMIND_MODE") or rank.get("mode_default", "auto")).lower()
    if resolved_mode not in {"auto", "online", "offline"}:
        raise ValueError(f"未知 mode: {resolved_mode}（允许 auto|online|offline）")

    resolved_seed = int(seed if seed is not None else rank.get("seed", 42))
    rank = {**rank, "seed": resolved_seed, "mode_default": resolved_mode}

    snapshot_digest = ""
    snap = Path(os.environ.get("EVIDENCE_SNAPSHOT_DIR", SNAPSHOT_DIR))
    if snap.is_dir():
        files = sorted(p.name for p in snap.glob("*.jsonl"))
        snapshot_digest = hashlib.sha256("|".join(files).encode()).hexdigest()[:12]

    hash_payload = {
        "rank": rank,
        "filter_steps": filter_steps,
        "lipid_steps": lipid_steps,
        "tox_steps": tox_steps,
        "model_manifest": model_manifest,
        "mode": resolved_mode,
        "snapshot_digest": snapshot_digest,
    }
    return AppConfig(
        raw=rank,
        filter_steps=filter_steps,
        lipid_steps=lipid_steps,
        tox_steps=tox_steps,
        model_manifest=model_manifest,
        mode=resolved_mode,
        seed=resolved_seed,
        config_hash=_ljr_cfg_digest(hash_payload),
    )
