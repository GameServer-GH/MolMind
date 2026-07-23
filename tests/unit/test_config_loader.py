"""config_loader 与运行时开关测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.pipeline.config_loader import ConfigLoadError, load_config, resolve_runtime_switches

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"


def test_config_hash_stable_for_same_input() -> None:
    a = load_config(config_dir=CONFIG_DIR, seed=42)
    b = load_config(config_dir=CONFIG_DIR, seed=42)
    assert a.config_hash == b.config_hash
    assert len(a.config_hash) == 16


def test_config_hash_changes_with_seed() -> None:
    a = load_config(config_dir=CONFIG_DIR, seed=42)
    b = load_config(config_dir=CONFIG_DIR, seed=99)
    assert a.config_hash != b.config_hash


def test_missing_config_dir_raises_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_configs"
    with pytest.raises(ConfigLoadError, match="配置目录不存在"):
        load_config(config_dir=missing)


def test_missing_yaml_raises_clear_error(tmp_path: Path) -> None:
    (tmp_path / "rank_weights.yaml").write_text("mode_default: auto\nseed: 1\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="缺少配置文件"):
        load_config(config_dir=tmp_path)


def test_load_default_configs_ok() -> None:
    cfg = load_config(config_dir=CONFIG_DIR)
    assert cfg.mode == "auto"
    assert isinstance(cfg.raw, dict)
    assert "weights" in cfg.raw


def test_quality_max_defaults_snapshot_on_live_off() -> None:
    cfg = load_config(config_dir=CONFIG_DIR)
    assert cfg.mode == "auto"
    assert cfg.use_snapshot is True
    assert cfg.allow_live_evidence is False


def test_allow_live_switch_is_hashed() -> None:
    off = load_config(allow_live=False)
    on = load_config(allow_live=True)
    assert off.allow_live_evidence is False
    assert on.allow_live_evidence is True
    assert off.config_hash != on.config_hash


def test_use_snapshot_switch_is_hashed() -> None:
    on = load_config(use_snapshot=True)
    off = load_config(use_snapshot=False)
    assert on.evidence["use_snapshot"] is True
    assert off.evidence["use_snapshot"] is False
    assert on.config_hash != off.config_hash


def test_legacy_mode_online_maps_to_allow_live() -> None:
    cfg = load_config(mode="online")
    assert cfg.mode == "auto"
    assert cfg.allow_live_evidence is True


def test_legacy_mode_offline_can_disable_snapshot() -> None:
    cfg = load_config(mode="offline", use_snapshot=False)
    assert cfg.mode == "auto"
    assert cfg.use_snapshot is False
    assert cfg.allow_live_evidence is False


def test_resolve_runtime_switches_quality_max() -> None:
    mode, allow_live, use_snapshot = resolve_runtime_switches()
    assert mode == "auto"
    assert allow_live is False
    assert use_snapshot is True


def test_epa_stage_override_is_hashed() -> None:
    stage1 = load_config(epa_stage=1)
    stage2 = load_config(epa_stage=2)
    assert stage1.evidence["epa_ctx"]["integration_stage"] == 1
    assert stage2.evidence["epa_ctx"]["integration_stage"] == 2
    assert stage1.config_hash != stage2.config_hash
