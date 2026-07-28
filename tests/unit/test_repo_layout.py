"""Assert formal-tree paths exist after R2 pluginization.

Canonical scientific code: `plugins/molmind_core/scientific/*`
Compatibility shims: `services/*` (except `services/agent` → `agent/`)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DIRS = [
    "apps/cli",
    "apps/api",
    "apps/web",
    "agent",
    "plugins/molmind_core/scientific/ingest",
    "plugins/molmind_core/scientific/hard_filter",
    "plugins/molmind_core/scientific/scorer_lipid",
    "plugins/molmind_core/scientific/scorer_tox",
    "plugins/molmind_core/scientific/ranker",
    "plugins/molmind_core/scientific/critic",
    "plugins/molmind_core/scientific/mechanism",
    "plugins/molmind_core/scientific/evidence_facade",
    "plugins/molmind_core/scientific/eval_harness",
    "plugins/molmind_core/scientific/pipeline",
    "services/ingest",
    "services/pipeline",
    "packages/chem_core",
    "packages/goldset",
    "packages/ml_optional",
    "packages/models",
    "configs",
    "configs/agent",
    "data/goldset",
    "data/evidence_snapshot",
    "data/fixtures",
    "data/agent_runs",
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
    "agent/__init__.py",
    "plugins/__init__.py",
    "plugins/molmind_core/__init__.py",
    "plugins/molmind_core/scientific/__init__.py",
    "plugins/molmind_core/scientific/pipeline/__init__.py",
    "services/__init__.py",
    "services/ingest/__init__.py",
    "services/pipeline/__init__.py",
    "services/agent/__init__.py",
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
    "plugins/molmind_core/scientific/pipeline/README.md",
    "plugins/molmind_core/scientific/ingest/README.md",
    "services/README.md",
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


def test_services_shims_point_at_scientific() -> None:
    text = (ROOT / "services" / "pipeline" / "__init__.py").read_text(encoding="utf-8")
    assert "plugins.molmind_core.scientific.pipeline" in text


# deploy/README.md 是部署指南（含命令），不是业务 stub
_README_STUBS = [
    rel
    for rel in REQUIRED_README
    if rel not in {"deploy/README.md", "apps/web/README.md"}
]


def test_readme_stubs_have_no_business_logic() -> None:
    """Package/service README stubs should stay short duty notes (no code fences / imports)."""
    for rel in _README_STUBS:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "```" not in text, f"{rel} should not contain code fences"
        assert "def " not in text, f"{rel} should not contain Python defs"
        assert "import " not in text, f"{rel} should not contain imports"
        assert len(text.strip()) > 0, f"{rel} is empty"


def test_deploy_readme_is_deploy_guide() -> None:
    text = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "docker compose" in text
    assert "18765" in text
    assert len(text.strip()) > 100
