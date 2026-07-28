"""Backward-compatible shim → plugins.molmind_core.scientific.hard_filter.

Prefer: `from plugins.molmind_core.scientific.hard_filter import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.hard_filter as _pkg
import sys

sys.modules[__name__] = _pkg
