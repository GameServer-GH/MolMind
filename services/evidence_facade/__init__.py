"""Backward-compatible shim → plugins.molmind_core.scientific.evidence_facade.

Prefer: `from plugins.molmind_core.scientific.evidence_facade import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.evidence_facade as _pkg
import sys

sys.modules[__name__] = _pkg
