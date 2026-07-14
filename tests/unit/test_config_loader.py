"""config_loader stub: stable hash + clear missing-file errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.pipeline.config_loader import ConfigLoadError, load_config

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"


def test_config_hash_stable_for_same_input() -> None:
    a = load_config(config_dir=CONFIG_DIR, mode="auto", seed=42)
    b = load_config(config_dir=CONFIG_DIR, mode="auto", seed=42)
    assert a.config_hash == b.config_hash
    assert len(a.config_hash) == 16


def test_config_hash_changes_with_seed() -> None:
    a = load_config(config_dir=CONFIG_DIR, mode="auto", seed=42)
    b = load_config(config_dir=CONFIG_DIR, mode="auto", seed=99)
    assert a.config_hash != b.config_hash


def test_missing_config_dir_raises_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_configs"
    with pytest.raises(ConfigLoadError, match="配置目录不存在"):
        load_config(config_dir=missing)


def test_missing_yaml_raises_clear_error(tmp_path: Path) -> None:
    # Only one of the required files present
    (tmp_path / "rank_weights.yaml").write_text("mode_default: auto\nseed: 1\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="缺少配置文件"):
        load_config(config_dir=tmp_path)


def test_load_default_configs_ok() -> None:
    cfg = load_config(config_dir=CONFIG_DIR)
    assert cfg.mode in {"auto", "online", "offline"}
    assert isinstance(cfg.raw, dict)
    assert "weights" in cfg.raw
