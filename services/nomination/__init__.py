"""Backward-compatible shim → plugins.molmind_core.scientific.nomination.

Prefer: `from plugins.molmind_core.scientific.nomination import ...`
"""
from __future__ import annotations

import importlib
import plugins.molmind_core.scientific.nomination as _pkg
import sys

# Re-export the package itself and its public submodules as the same module
# objects.  Without these aliases, importing ``services.nomination.llm_review``
# after the package redirect executes the same file under a second module name;
# runtime imports then miss monkeypatches and extension hooks registered through
# the legacy services namespace.
for _submodule in ("llm_review", "proposals", "review"):
    sys.modules[f"{__name__}.{_submodule}"] = importlib.import_module(
        f"{_pkg.__name__}.{_submodule}"
    )

sys.modules[__name__] = _pkg
