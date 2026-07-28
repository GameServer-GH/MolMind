"""Backward-compatible shim → plugins.molmind_core.scientific.mechanism.

Prefer: `from plugins.molmind_core.scientific.mechanism import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.mechanism as _pkg
import sys

sys.modules[__name__] = _pkg
