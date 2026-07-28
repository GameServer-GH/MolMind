"""Backward-compatible shim → plugins.molmind_core.scientific.ranker.

Prefer: `from plugins.molmind_core.scientific.ranker import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.ranker as _pkg
import sys

sys.modules[__name__] = _pkg
