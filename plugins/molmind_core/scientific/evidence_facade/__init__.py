"""plugins.molmind_core.scientific.evidence_facade — snapshot 优先 + ChEMBL/PubChem live 补洞。"""

from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle
from plugins.molmind_core.scientific.evidence_facade.facade import EvidenceFacade

__all__ = [
    "EvidenceBundle",
    "EvidenceFacade",
    "evaluate_dual_endpoint_training_record",
    "load_hepg2_ffa_resource_registry",
    "resource_registry_runtime_payload",
]


def __getattr__(name: str):
    if name in (
        "BakeStats",
        "bake_evidence_for_records",
        "bake_from_sdf",
        "bake_frozen_top10",
        "bake_submission_evidence",
        "load_frozen_top10_records",
        "PromoteStats",
        "promote_evidence_cache",
    ):
        from plugins.molmind_core.scientific.evidence_facade import bake as _bake

        return getattr(_bake, name)
    if name in (
        "evaluate_dual_endpoint_training_record",
        "load_hepg2_ffa_resource_registry",
        "resource_registry_runtime_payload",
    ):
        from plugins.molmind_core.scientific.evidence_facade import hepg2_ffa_resources as _resources

        return getattr(_resources, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
