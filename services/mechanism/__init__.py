"""Backward-compatible shim → plugins.molmind_core.scientific.mechanism.

Prefer: `from plugins.molmind_core.scientific.mechanism import ...`
"""
from __future__ import annotations

import importlib
import plugins.molmind_core.scientific.mechanism as _pkg
import sys

# Keep legacy and canonical submodule imports on the same module objects. This
# prevents duplicate LLM client state and makes monkeypatches/extensions apply
# consistently regardless of which supported import path loaded first.
for _submodule in ("llm_client", "mechanism", "pdf_export", "html_report"):
    sys.modules[f"{__name__}.{_submodule}"] = importlib.import_module(
        f"{_pkg.__name__}.{_submodule}"
    )

sys.modules[__name__] = _pkg
