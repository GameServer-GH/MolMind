"""Backward-compatible shim → plugins.molmind_core.scientific.evidence_gateway.

Prefer: `from plugins.molmind_core.scientific.evidence_gateway import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.evidence_gateway as _pkg
import sys

sys.modules[__name__] = _pkg
