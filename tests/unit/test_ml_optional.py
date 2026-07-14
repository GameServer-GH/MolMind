"""packages.ml_optional：无模型不阻断；有假模型文件则 head 非零。"""

from pathlib import Path

from packages.ml_optional import load_optional_heads


def test_no_model_does_not_block() -> None:
    result = load_optional_heads({"models": []})
    assert result.skipped is True
    assert result.dili == 0.0
    assert result.admet == 0.0


def test_fake_model_file_yields_nonzero(tmp_path: Path) -> None:
    model = tmp_path / "fake_dili.bin"
    model.write_bytes(b"fake")
    result = load_optional_heads(
        {"models": [{"path": "fake_dili.bin", "dili_placeholder": 0.33, "admet_placeholder": 0.22}]},
        model_dir=tmp_path,
    )
    assert result.skipped is False
    assert result.dili == 0.33
    assert result.admet == 0.22
