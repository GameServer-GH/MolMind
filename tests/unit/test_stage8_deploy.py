"""阶段 8：Docker/断网/交付清单文件存在性。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_includes_snapshot_and_rdkit() -> None:
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "rdkit" in text.lower()
    assert "evidence_snapshot" in text or "data" in text
    assert "configs" in text
    assert "goldset" in text or "data" in text


def test_chaos_script_exists() -> None:
    script = ROOT / "scripts" / "chaos_offline.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "offline" in text
    assert "sample.sdf" in text


def test_delivery_checklist_doc() -> None:
    doc = ROOT / "docs" / "architecture" / "交付清单核对.md"
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    assert "CSV" in text
    assert "snapshot" in text.lower() or "evidence_snapshot" in text
    assert "Dockerfile" in text


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
