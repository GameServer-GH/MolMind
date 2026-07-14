"""apps.cli：--help 与 eval-goldset。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cli_help() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "apps.cli.main", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "screen" in proc.stdout or "bake-evidence" in proc.stdout or "eval-goldset" in proc.stdout


def test_cli_eval_goldset() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "apps.cli.main", "--eval-goldset", "--mode", "offline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "passed" in proc.stdout
