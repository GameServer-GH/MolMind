"""Backward-compatible shim → plugins.molmind_core.scientific.nomination.

Prefer: `from plugins.molmind_core.scientific.nomination import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.nomination as _pkg
import sys

sys.modules[__name__] = _pkg
