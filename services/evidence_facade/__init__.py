"""services.evidence_facade — snapshot 优先 + ChEMBL/PubChem live 补洞。"""

from services.evidence_facade.bundle import EvidenceBundle
from services.evidence_facade.facade import EvidenceFacade

__all__ = [
    "EvidenceBundle",
    "EvidenceFacade",
]


def __getattr__(name: str):
    if name in ("BakeStats", "bake_evidence_for_records", "bake_from_sdf"):
        from services.evidence_facade import bake as _bake

        return getattr(_bake, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
