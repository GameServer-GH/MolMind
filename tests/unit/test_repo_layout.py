"""Assert formal-tree paths from 正式版实现清单 §1 exist."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DIRS = [
    "apps/cli",
    "apps/api",
    "apps/web",
    "services/ingest",
    "services/hard_filter",
    "services/scorer_lipid",
    "services/scorer_tox",
    "services/ranker",
    "services/critic",
    "services/mechanism",
    "services/evidence_facade",
    "services/eval_harness",
    "services/pipeline",
    "packages/chem_core",
    "packages/goldset",
    "packages/ml_optional",
    "packages/models",
    "configs",
    "data/goldset",
    "data/evidence_snapshot",
    "data/fixtures",
    "templates",
    "tests/unit",
    "tests/integration",
    "tests/goldset",
    "tests/regression",
    "deploy",
]

REQUIRED_INIT = [
    "apps/__init__.py",
    "apps/cli/__init__.py",
    "apps/api/__init__.py",
    "apps/web/__init__.py",
    "services/__init__.py",
    "services/ingest/__init__.py",
    "services/hard_filter/__init__.py",
    "services/scorer_lipid/__init__.py",
    "services/scorer_tox/__init__.py",
    "services/ranker/__init__.py",
    "services/critic/__init__.py",
    "services/mechanism/__init__.py",
    "services/evidence_facade/__init__.py",
    "services/eval_harness/__init__.py",
    "services/pipeline/__init__.py",
    "packages/__init__.py",
    "packages/chem_core/__init__.py",
    "packages/goldset/__init__.py",
    "packages/ml_optional/__init__.py",
    "packages/models/__init__.py",
]

REQUIRED_README = [
    "apps/README.md",
    "apps/cli/README.md",
    "apps/api/README.md",
    "apps/web/README.md",
    "services/README.md",
    "services/ingest/README.md",
    "services/hard_filter/README.md",
    "services/scorer_lipid/README.md",
    "services/scorer_tox/README.md",
    "services/ranker/README.md",
    "services/critic/README.md",
    "services/mechanism/README.md",
    "services/evidence_facade/README.md",
    "services/eval_harness/README.md",
    "services/pipeline/README.md",
    "packages/README.md",
    "packages/chem_core/README.md",
    "packages/goldset/README.md",
    "packages/ml_optional/README.md",
    "packages/models/README.md",
    "configs/README.md",
    "data/README.md",
    "templates/README.md",
    "tests/README.md",
    "deploy/README.md",
]


def test_required_directories_exist() -> None:
    missing = [p for p in REQUIRED_DIRS if not (ROOT / p).is_dir()]
    assert not missing, f"missing directories: {missing}"


def test_required_init_files_exist() -> None:
    missing = [p for p in REQUIRED_INIT if not (ROOT / p).is_file()]
    assert not missing, f"missing __init__.py: {missing}"


def test_required_readme_stubs_exist() -> None:
    missing = [p for p in REQUIRED_README if not (ROOT / p).is_file()]
    assert not missing, f"missing README.md: {missing}"


# deploy/README.md 是部署指南（含命令），不是业务 stub
_README_STUBS = [rel for rel in REQUIRED_README if rel != "deploy/README.md"]


def test_readme_stubs_have_no_business_logic() -> None:
    """Package/service README stubs should stay short duty notes (no code fences / imports)."""
    for rel in _README_STUBS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "```" not in text, f"{rel} should not contain code fences"
        assert "def " not in text, f"{rel} should not contain Python defs"
        assert "import " not in text, f"{rel} should not contain imports"
        assert len(text.strip()) > 0, f"{rel} is empty"


def test_deploy_readme_is_deploy_guide() -> None:
    text = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "docker compose" in text
    assert "18765" in text
    assert len(text.strip()) > 100
