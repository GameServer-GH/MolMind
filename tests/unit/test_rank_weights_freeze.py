"""主路径冻结口径：主证据 chembl/pubchem；nafldkb/dili 默认关闭。"""

from __future__ import annotations

from pathlib import Path

from services.pipeline.config_loader import load_config

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"

PRIMARY_ADAPTERS = {"chembl_lipid_v1", "pubchem_tox_v1"}
DISABLED_BY_DEFAULT = {"nafldkb_v1", "dili_table_v1", "ot_target_v1"}


def test_freeze_primary_adapters_enabled() -> None:
    cfg = load_config(config_dir=CONFIG_DIR)
    evidence = cfg.evidence
    adapters = set(evidence.get("adapters") or [])
    assert adapters == PRIMARY_ADAPTERS

    flags = evidence.get("adapter_flags") or {}
    for name in PRIMARY_ADAPTERS:
        assert flags[name]["enabled"] is True
        assert float(flags[name].get("ranking_weight", 0)) > 0


def test_freeze_nafldkb_dili_disabled() -> None:
    cfg = load_config(config_dir=CONFIG_DIR)
    flags = cfg.evidence.get("adapter_flags") or {}
    for name in DISABLED_BY_DEFAULT:
        assert name in flags
        assert flags[name]["enabled"] is False
        assert float(flags[name].get("ranking_weight", 1)) == 0.0

    local = cfg.evidence.get("local_tables") or {}
    assert local.get("enabled") is False


def test_freeze_comment_present_in_rank_weights() -> None:
    text = (CONFIG_DIR / "rank_weights.yaml").read_text(encoding="utf-8")
    assert "chembl_lipid_v1" in text
    assert "pubchem_tox_v1" in text
    assert "定榜冻结" in text or "主证据" in text
