"""Backward-compatible shim → plugins.molmind_core.scientific.ingest.

Prefer: `from plugins.molmind_core.scientific.ingest import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.ingest as _pkg
import sys

sys.modules[__name__] = _pkg
