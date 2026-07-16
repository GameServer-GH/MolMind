"""阶段 8：Docker/断网/交付清单文件存在性。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_includes_snapshot_and_rdkit() -> None:
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "rdkit" in text.lower()
    assert "evidence_snapshot" in text or "data" in text
    assert "configs" in text
    assert "goldset" in text or "data" in text
    assert "requirements.lock" in text
    assert "rdkit=2025.09.2" in text
    assert "TARGETPLATFORM" in text
    assert "PIP_INDEX_URL" in text and "CONDA_CHANNEL" in text
    lock = (ROOT / "deploy" / "requirements.lock").read_text(encoding="utf-8")
    assert "fastapi==" in lock and "reportlab==" in lock and "pytest==" in lock
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".venv" in dockerignore and "mvp" in dockerignore and "docs" in dockerignore


def test_chaos_script_exists() -> None:
    script = ROOT / "scripts" / "chaos_offline.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "offline" in text
    assert "sample.sdf" in text


def test_delivery_surfaces_documented() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Docker Compose" in readme
    assert "evidence_snapshot" in readme or "证据快照" in readme
    deploy = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "Dockerfile" in deploy or "docker compose" in deploy


def test_local_compose_for_deploy() -> None:
    compose_path = ROOT / "deploy" / "docker-compose.yml"
    assert compose_path.is_file()
    compose = compose_path.read_text(encoding="utf-8")
    assert "18765:18765" in compose
    assert "uvicorn" in compose
    assert "apps.api.app:app" in compose
    assert "\n  nginx:" not in compose
    assert '"8000"' not in compose
    assert "--port\", \"8000\"" not in compose
    readme = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "docker compose -f deploy/docker-compose.yml" in readme
    assert "macOS" in readme or "Mac" in readme
    assert "Windows" in readme


def test_deploy_pro_is_gitignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deploy_pro/" in gitignore


def test_github_ci_enforces_reproducibility_contract() -> None:
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file()
    text = workflow.read_text(encoding="utf-8")
    for required in (
        "pytest",
        "check_algorithm_freeze.py",
        "check_rank_config_gate.py",
        "data/sample.sdf",
        "deploy/Dockerfile",
        "linux/amd64",
    ):
        assert required in text
