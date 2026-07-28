"""Backward-compatible shim → plugins.molmind_core.scientific.scorer_tox.

Prefer: `from plugins.molmind_core.scientific.scorer_tox import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.scorer_tox as _pkg
import sys

sys.modules[__name__] = _pkg
