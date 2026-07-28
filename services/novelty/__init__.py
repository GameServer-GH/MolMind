"""Backward-compatible shim → plugins.molmind_core.scientific.novelty.

Prefer: `from plugins.molmind_core.scientific.novelty import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.novelty as _pkg
import sys

sys.modules[__name__] = _pkg
