"""Backward-compatible shim → plugins.molmind_core.scientific.eval_harness.

Prefer: `from plugins.molmind_core.scientific.eval_harness import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.eval_harness as _pkg
import sys

sys.modules[__name__] = _pkg
