"""Backward-compatible shim → plugins.molmind_core.scientific.public_data.

Prefer: `from plugins.molmind_core.scientific.public_data import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.public_data as _pkg
import sys

sys.modules[__name__] = _pkg
