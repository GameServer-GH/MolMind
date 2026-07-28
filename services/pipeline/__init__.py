"""Backward-compatible shim → plugins.molmind_core.scientific.pipeline.

Prefer: `from plugins.molmind_core.scientific.pipeline import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.pipeline as _pkg
import sys

sys.modules[__name__] = _pkg
