"""Backward-compatible shim → plugins.molmind_core.scientific.critic.

Prefer: `from plugins.molmind_core.scientific.critic import ...`
"""
from __future__ import annotations

import plugins.molmind_core.scientific.critic as _pkg
import sys

sys.modules[__name__] = _pkg
