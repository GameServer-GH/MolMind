"""Backward-compatible shim → plugins.molmind_core.scientific.eligibility.

Prefer: `from plugins.molmind_core.scientific.eligibility import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.eligibility as _pkg
import sys

sys.modules[__name__] = _pkg
