"""Normalize reviewed MCP observations into MolMind's Evidence Contract."""
from __future__ import annotations
from typing import Any
from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_gateway.contract import content_sha256
from .models import SCPObservation

def observation_to_evidence_hit(observation: SCPObservation, *, molecule_id: str, query_type: str = "pathway", identity: dict[str, Any] | None = None) -> EvidenceHit:
    identity = identity or observation.identity or {}
    match_type = str(identity.get("match_type") or "")
    exact = match_type in {"exact_inchikey", "exact_smiles", "exact_cas"}
    status_map = {"hit":"hit", "verified_empty":"verified_empty", "auth_missing":"auth_missing", "identity_review_required":"identity_review_required", "annotation_only":"annotation_only"}
    status = status_map.get(observation.status, "query_failed")
    if status == "hit" and identity and not exact: status = "identity_review_required"
    payload = {"source":"scp-hub", "server_id":observation.server_id, "tool_name":observation.tool_name, "skill_id":observation.skill_id, "molecule_id":molecule_id, "content":[block.__dict__ for block in observation.content], "claims":observation.claims, "participates_in_ranking":False, "writes_selection":False}
    return EvidenceHit(adapter_id=f"scp:{observation.server_id}:{observation.tool_name}", provider_id="scp-hub", query_type=query_type, score=0.0, confidence=0.5 if status == "hit" else 0.0, evidence_id=observation.response_hash or content_sha256(payload), payload=payload, endpoint=observation.server_id, evidence_role="live_supplementary", provenance_status="live_unfrozen", retrieved_at=observation.retrieved_at, response_sha256=(observation.response_hash.removeprefix("sha256:") if observation.response_hash else content_sha256(payload)), query_status=status, raw_status=observation.status, evidence_type="mechanism_context" if query_type == "pathway" else "endpoint_evidence", lookup_field=str(identity.get("lookup_field") or ""), lookup_value=str(identity.get("lookup_value") or ""), match_type=match_type, claim_ceiling="mechanism_context_only_not_candidate_efficacy" if query_type == "pathway" else "candidate_level_remote_record_not_ranking_evidence")
