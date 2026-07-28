"""Evidence Facade：Quality-Max 下 snapshot 优先 → Top-M live 补洞 → 空结果（不伪造）。"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle, infer_evidence_type
from plugins.molmind_core.scientific.pipeline.config_loader import SNAPSHOT_DIR, AppConfig
from plugins.molmind_core.scientific.evidence_facade.epa_index import EPAContextIndex
from plugins.molmind_core.scientific.evidence_facade.epa_risk import epa_cytotox_metrics, epa_cytotox_risk_tier

LIPID_MECHANISM_TARGET_RE = re.compile(
    r"HMGCR|HMG.?CoA|PPAR[A-Z]?|SREBF|SREBP|ACAC[AB]?|FASN|SCD1?|CPT1|"
    r"AMPK|PRKAA|LDLR|NPC1L1|DGAT|LPL|ABCA1|CYP7A1|"
    r"peroxisome proliferator-activated receptor",
    re.I,
)
LIPID_ENDPOINT_RE = re.compile(
    r"lipid droplets?|neutral lipids?|lipid accumulation|triglycerides?|cholesterol|"
    r"steatosis|fat accumulation",
    re.I,
)
POSITIVE_LIPID_DIRECTION_RE = re.compile(
    r"reduc|decreas|lower|attenuat|ameliorat|prevent|clearance|efflux|"
    r"anti(?:adipogenic|steatotic|hyperlipidemic)|"
    r"inhibit(?:ion|ed|s|ing)?[^.;]{0,80}(?:accumulation|synthesis|biosynthesis|adipogenesis|differentiation)",
    re.I,
)
# Tight adverse cues only. Bare "hepatic steatosis"/"fatty liver" appear in
# beneficial antisteatosis assays and must not auto-label risk.
ADVERSE_LIPID_PHENOTYPE_RE = re.compile(
    r"phospholipidosis|"
    r"(?:induc(?:e|ed|es|ing|tion)|promot(?:e|ed|es|ing))\s+(?:of\s+)?(?:hepatic\s+)?"
    r"(?:steatosis|lipid accumulation|lipid droplets?|fatty liver)|"
    r"increas(?:e|ed|es|ing)\s+(?:in\s+)?(?:neutral\s+)?"
    r"(?:lipid accumulation|lipid droplets?|triglycerides?|steatosis)|"
    r"drug[- ]induced (?:hepatic )?steatosis",
    re.I,
)
# Clear beneficial verbs that may override an "induced ... accumulation" disease model cue.
BENEFICIAL_LIPID_VERB_RE = re.compile(
    r"reduc|decreas|lower|attenuat|ameliorat|"
    r"anti(?:adipogenic|steatotic|hyperlipidemic)|clearance|efflux",
    re.I,
)
CELL_CONTEXT_RE = re.compile(
    r"HepG2|hepatocyt|cell(?:ular|s| line|-based)?|3T3-L1|3T3L1|adipocyt|HUVEC",
    re.I,
)
EVIDENCE_SCHEMA_VERSION = "evidence-v2"
_CONSERVATIVE_RISK_DIRECTIONS = {"risk", "contradicts", "adverse", "negative"}
_LEGACY_SCORING_ADAPTERS = frozenset(
    {
        "chembl_lipid_v1",
        "pubchem_tox_v1",
        "dili_table_v1",
        "nafldkb_v1",
    }
)
_LEGACY_SCORING_QUERY_TYPES = frozenset({"lipid", "tox", "novelty", "pathway"})


def _load_frozen_risk_aliases() -> dict[str, str]:
    """原始结构键 → 标准化结构键；仅用于保守传播毒性风险。"""
    path = (
        REPO_ROOT
        / "data"
        / "evidence_snapshot"
        / "v2"
        / "risk_identity_aliases.json"
    )
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    aliases: dict[str, str] = {}
    for item in payload.get("aliases") or []:
        original = str(item.get("original_inchikey") or "").strip()
        standardized = str(item.get("standardized_inchikey") or "").strip()
        if original and standardized and original != standardized:
            aliases[original] = standardized
    return aliases


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _response_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _input_structure_hash(*, inchikey: str, smiles: str, cas: str | None) -> str:
    payload = f"{inchikey}|{smiles}|{cas or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finalize_hit(hit: EvidenceHit) -> EvidenceHit:
    if not hit.source_version:
        hit.source_version = hit.adapter_version or hit.adapter_id
    hit.evidence_type = infer_evidence_type(hit)  # type: ignore[assignment]
    return hit


def _query_audit_hit(
    *,
    adapter_id: str,
    query_status: str,
    evidence_id: str,
    payload: dict[str, Any],
    source_url: str = "",
    response_content: bytes = b"",
    provenance_status: str = "retrieved",
) -> EvidenceHit:
    """Create a non-scoring, machine-readable query outcome.

    Query transport/identity outcomes must never leak into efficacy, safety,
    novelty, or confidence scores.  They remain available for retry and audit.
    """
    return _finalize_hit(
        EvidenceHit(
            adapter_id=adapter_id,
            query_type="query_audit",
            score=0.0,
            confidence=0.0,
            evidence_id=evidence_id,
            payload=payload,
            endpoint="query_status",
            direction="unknown",
            evidence_role="query_audit",
            provenance_status=provenance_status,
            source_url=source_url,
            retrieved_at=_utc_now(),
            adapter_version=f"{adapter_id}:query-contract-v1",
            source_version=f"{adapter_id}:query-contract-v1",
            query_params={},
            response_sha256=_response_sha256(response_content) if response_content else "",
            license="",
            query_status=query_status,
            evidence_type="query_audit",
        )
    )


def _chembl_activity_text(activity: dict[str, Any]) -> str:
    return " ".join(
        str(activity.get(key) or "")
        for key in (
            "target_pref_name",
            "assay_description",
            "activity_comment",
            "data_validity_comment",
            "standard_type",
            "bao_label",
        )
    ).strip()


def _classify_chembl_activity(activity: dict[str, Any]) -> str:
    """分类为 positive_phenotype / adverse_phenotype / mechanism / annotation。"""
    text = _chembl_activity_text(activity)
    has_lipid = bool(LIPID_ENDPOINT_RE.search(text))
    has_cell = bool(CELL_CONTEXT_RE.search(text))
    has_beneficial = bool(BENEFICIAL_LIPID_VERB_RE.search(text))
    has_positive_dir = bool(POSITIVE_LIPID_DIRECTION_RE.search(text))
    adverse = bool(ADVERSE_LIPID_PHENOTYPE_RE.search(text))

    # Disease-model phrasing ("H2O2-induced lipid accumulation … reduction") must
    # not bury cellular lipid-lowering / antisteatotic readouts.
    if adverse:
        if has_lipid and has_cell and has_beneficial:
            return "positive_phenotype"
        return "adverse_phenotype"
    if has_lipid and has_positive_dir and has_cell:
        return "positive_phenotype"
    target = str(activity.get("target_pref_name") or "")
    if LIPID_MECHANISM_TARGET_RE.search(target):
        return "mechanism"
    return "annotation"


def _legacy_chembl_classification(payload: dict[str, Any]) -> str:
    structured = payload.get("structured_hits") or []
    classifications = {
        _classify_chembl_activity(item)
        for item in structured
        if isinstance(item, dict)
    }
    target_text = " ".join(str(value) for value in payload.get("targets") or [])
    if "adverse_phenotype" in classifications or ADVERSE_LIPID_PHENOTYPE_RE.search(target_text):
        return "adverse_phenotype"
    if "positive_phenotype" in classifications:
        return "positive_phenotype"
    if "mechanism" in classifications or LIPID_MECHANISM_TARGET_RE.search(target_text):
        return "mechanism"
    return "annotation"


def _normalize_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    """只读兼容 v1 快照；查询状态和注释永不继续伪装成新颖性证据。"""
    normalized = dict(row)
    normalized.setdefault(
        "raw_status",
        str(normalized.get("query_status") or normalized.get("provenance_status") or ""),
    )
    if not normalized.get("query_status"):
        adapter = str(normalized.get("adapter_id") or "")
        query_type = str(normalized.get("query_type") or "")
        role = str(normalized.get("evidence_role") or "")
        provenance = str(normalized.get("provenance_status") or "")
        evidence_type = str(normalized.get("evidence_type") or "")
        evidence_id = str(normalized.get("evidence_id") or "").strip()
        if role == "annotation_only":
            normalized["query_status"] = "annotation_only"
        elif role == "query_audit" and provenance == "no_relevant_record":
            normalized["query_status"] = "verified_empty"
        elif role == "query_audit" and provenance == "query_failed":
            normalized["query_status"] = "adapter_error"
        elif (
            adapter in _LEGACY_SCORING_ADAPTERS
            and query_type in _LEGACY_SCORING_QUERY_TYPES
            and role not in {"query_audit", "annotation_only"}
            and evidence_type not in {"query_audit", "identity_annotation"}
            and evidence_id
        ):
            # Strict, explicit migration for historical frozen task evidence.
            # Unknown adapters and transport/annotation rows must stay
            # non-scoring even if their old provenance said "retrieved".
            normalized["query_status"] = "exact_hit"
            normalized["provenance_status"] = "legacy_snapshot_migrated"
        else:
            normalized["query_status"] = "not_queried"
    adapter = str(normalized.get("adapter_id") or "")
    payload = normalized.get("payload") or {}
    if adapter == "bake_miss_v1":
        normalized["query_type"] = "query_audit"
        normalized["evidence_role"] = "query_audit"
        normalized["score"] = 0.0
        normalized["confidence"] = 0.0
        normalized["query_status"] = "verified_empty"
    elif (
        adapter == "chembl_lipid_v1"
        and normalized.get("query_type") == "lipid"
        and normalized.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        classification = _legacy_chembl_classification(payload)
        if classification == "adverse_phenotype":
            normalized["query_type"] = "tox"
            normalized["endpoint"] = "adverse_lipid_phenotype"
            normalized["direction"] = "risk"
            normalized["evidence_role"] = "task_evidence"
            normalized["evidence_id"] = str(normalized.get("evidence_id") or "").replace(
                ":lipid", ":adverse_lipid"
            )
        elif classification == "mechanism":
            normalized["query_type"] = "pathway"
            normalized["endpoint"] = "lipid_mechanism_association"
            normalized["direction"] = "unknown"
            normalized["evidence_role"] = "mechanism_support"
            normalized["score"] = min(float(normalized.get("score") or 0.0), 0.25)
            normalized["confidence"] = min(float(normalized.get("confidence") or 0.0), 0.4)
        elif classification != "positive_phenotype":
            normalized["query_type"] = "annotation"
            normalized["endpoint"] = "database_annotation"
            normalized["direction"] = "unknown"
            normalized["evidence_role"] = "annotation_only"
            normalized["score"] = 0.0
            normalized["confidence"] = 0.0
            normalized["query_status"] = "annotation_only"
    elif (
        adapter == "chembl_lipid_v1"
        and normalized.get("query_type") == "novelty"
        and int(payload.get("lipid_hits") or 0) == 0
    ):
        normalized["query_type"] = "annotation"
        normalized["evidence_role"] = "annotation_only"
        normalized["score"] = 0.0
        normalized["confidence"] = 0.0
        normalized["query_status"] = "annotation_only"
    elif adapter == "pubchem_tox_v1" and normalized.get("query_type") == "tox":
        # Migrate pre-fix snapshots that scored DILI Negative as liver risk.
        matched_nodes = payload.get("matched_nodes") or []
        dili_negative = any(
            isinstance(node, dict)
            and "dilist classification" in str(node.get("path") or "").lower()
            and re.search(r"\bdili\s+negative\b", str(node.get("value") or ""), re.I)
            for node in matched_nodes
        )
        flags = [str(flag) for flag in (payload.get("flags") or [])]
        if dili_negative:
            flags = [flag for flag in flags if flag not in {"liver", "dili_positive"}]
            payload = {
                **payload,
                "flags": flags,
                "dili_classification": "negative",
                "legacy_dili_negative_migrated": True,
            }
            normalized["payload"] = payload
            if not flags:
                normalized["query_type"] = "annotation"
                normalized["endpoint"] = "database_annotation"
                normalized["direction"] = "unknown"
                normalized["evidence_role"] = "annotation_only"
                normalized["score"] = 0.0
                normalized["confidence"] = 0.0
                normalized["query_status"] = "annotation_only"
                normalized["evidence_id"] = str(
                    normalized.get("evidence_id") or ""
                ).replace(":ghs", ":dili_negative_annotation")
    normalized.setdefault("schema_version", "evidence-v1-legacy")
    return normalized


def _collect_pubchem_strings(node: Any, *, path: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """提取 PUG-View 结构化字符串及节点路径，不扫描整段 JSON 文本。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        heading = str(node.get("TOCHeading") or node.get("Name") or "").strip()
        next_path = (*path, heading) if heading else path
        swm = node.get("StringWithMarkup")
        if isinstance(swm, list):
            for item in swm:
                if isinstance(item, dict) and item.get("String"):
                    found.append((" > ".join(next_path), str(item["String"])))
        for key, value in node.items():
            if key == "StringWithMarkup":
                continue
            found.extend(_collect_pubchem_strings(value, path=next_path))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_pubchem_strings(item, path=path))
    return found


class EvidenceFacade:
    def __init__(self, cfg: AppConfig, snapshot_dir: Path | None = None):
        self.cfg = cfg
        self.snapshot_dir = Path(snapshot_dir or SNAPSHOT_DIR)
        self._index = self._load_snapshot_index()
        self.enabled = bool(cfg.evidence.get("enabled", True))
        self._cache: dict[str, EvidenceBundle] = {}
        self._live_failures = 0
        self._live_successes = 0
        self._circuit_open = False
        self._timeout = float(cfg.evidence.get("http_timeout_sec", 4.0))
        self._fail_threshold = int(cfg.evidence.get("circuit_fail_threshold", 5))
        self._dili_index = None
        self._nafld_index = None
        self._public_assay_index = None
        self._dilirank_gate_index = None
        self._epa_index = EPAContextIndex.from_config(
            (cfg.evidence.get("epa_ctx") or {})
        )
        dili_gate_cfg = cfg.evidence.get("dilirank_exact_gate") or {}
        if bool(dili_gate_cfg.get("enabled", True)):
            from plugins.molmind_core.scientific.evidence_facade.dilirank_gate import load_dilirank_index_from_config

            self._dilirank_gate_index = load_dilirank_index_from_config(dili_gate_cfg)
        lt = cfg.evidence.get("local_tables") or {}
        # 主路径默认关闭；显式 enabled=true 时才加载（阶段 7 可选）
        if bool(lt.get("enabled", False)):
            from plugins.molmind_core.scientific.evidence_facade.local_tables import load_dilirank, load_nafldkb

            root = REPO_ROOT
            dili_path = root / str(lt.get("dili_csv", "data/reference/dilirank.csv"))
            nafld_path = root / str(lt.get("nafld_csv", "data/reference/nafldkb.csv"))
            self._dili_index = load_dilirank(dili_path)
            self._nafld_index = load_nafldkb(nafld_path)

        pag = cfg.evidence.get("public_assay_grain") or {}
        if bool(pag.get("enabled", True)):
            from plugins.molmind_core.scientific.public_data.assay_index import load_public_assay_index

            root = REPO_ROOT
            paths = []
            for rel in pag.get("qc_paths") or [
                "data/public/processed/chembl_bioactivity/records_endpoint_qc.jsonl",
                "data/public/processed/pubchem_bioassay/records_endpoint_qc.jsonl",
                "data/public/processed/bindingdb/records_endpoint_qc.jsonl",
                "data/public/processed/epa_toxcast_tox21/records_endpoint_qc.jsonl",
            ]:
                paths.append(root / str(rel))
            self._public_assay_index = load_public_assay_index(paths)

        if not self.enabled:
            cfg.mark_degraded("evidence_disabled")

    def _effective_adapters(self) -> set[str]:
        """仅返回 adapter_flags 中 enabled 且 ranking_weight>0 的 adapters。"""
        listed = set(self.cfg.evidence.get("adapters") or [])
        flags = self.cfg.evidence.get("adapter_flags") or {}
        enabled: set[str] = set()
        for adapter in listed:
            meta = flags.get(adapter) or {}
            if not bool(meta.get("enabled", True)):
                continue
            if float(meta.get("ranking_weight", 1.0)) <= 0:
                continue
            enabled.add(adapter)
        return enabled

    def _load_snapshot_index(self) -> dict[str, list[dict[str, Any]]]:
        index: dict[str, list[dict[str, Any]]] = {}
        risk_aliases = _load_frozen_risk_aliases()
        if not self.snapshot_dir.is_dir():
            return index
        for path in sorted(self.snapshot_dir.glob("*.jsonl")):
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    row = _normalize_snapshot_row(row)
                    if row.get("provenance_status") == "query_failed":
                        # 保留在 JSONL 供审计，但失败记录不能阻止下一次联网重试。
                        continue
                    keys = {
                        str(row.get("inchikey") or "").strip(),
                        str(row.get("cas") or "").strip(),
                    }
                    row_inchikey = str(row.get("inchikey") or "").strip()
                    if (
                        row.get("query_type") == "tox"
                        or row.get("direction") == "risk"
                    ):
                        alias = risk_aliases.get(row_inchikey)
                        if alias:
                            keys.add(alias)
                            payload = dict(row.get("payload") or {})
                            payload.setdefault("identity_resolution", "risk_tautomer_or_parent_alias")
                            row["payload"] = payload
                    keys.discard("")
                    if not keys:
                        continue
                    for key in keys:
                        index.setdefault(key, []).append(row)
        return index

    @staticmethod
    def _snapshot_hit(
        *,
        adapter: str,
        row: dict[str, Any],
        key: str,
        query_type: str,
    ) -> EvidenceHit:
        adapter_version = str(row.get("adapter_version") or adapter)
        source_version = str(row.get("source_version") or adapter_version)
        return _finalize_hit(
            EvidenceHit(
                adapter_id=adapter,
                provider_id=str(row.get("provider_id") or ""),
                query_type=query_type,
                score=float(row.get("score", 0.0)),
                confidence=float(row.get("confidence", 0.5)),
                evidence_id=row.get("evidence_id") or f"snap:{adapter}:{key}",
                payload=dict(row.get("payload") or {}),
                endpoint=str(row.get("endpoint") or ""),
                direction=str(row.get("direction") or "unknown"),
                evidence_role=str(row.get("evidence_role") or "task_evidence"),
                provenance_status=str(row.get("provenance_status") or "legacy"),
                source_url=str(row.get("source_url") or ""),
                retrieved_at=str(row.get("retrieved_at") or ""),
                adapter_version=adapter_version,
                source_version=source_version,
                query_params=dict(row.get("query_params") or {}),
                response_sha256=str(row.get("response_sha256") or ""),
                license=str(row.get("license") or ""),
                query_status=str(row.get("query_status") or "not_queried"),
                raw_status=str(row.get("raw_status") or row.get("query_status") or ""),
                evidence_type=str(row.get("evidence_type") or "unresolved"),  # type: ignore[arg-type]
                lookup_field=str(row.get("lookup_field") or ""),
                lookup_value=str(row.get("lookup_value") or ""),
                match_type=str(row.get("match_type") or ""),
                accession=str(row.get("accession") or ""),
                claim_ceiling=str(row.get("claim_ceiling") or ""),
            )
        )

    def _active_snapshot_cas_conflicts(
        self,
        *,
        inchikey: str,
        cas: str | None,
        adapters: set[str],
    ) -> list[dict[str, Any]]:
        """Return active CAS rows whose stored structure disagrees with the query.

        Snapshot append/compaction semantics keep the latest row for each
        adapter and query type.  Apply the same rule before declaring a CAS
        conflict so an obsolete row cannot shadow a later correction.
        """

        candidate_key = str(inchikey or "").strip().upper()
        cas_key = str(cas or "").strip()
        if not candidate_key or not cas_key:
            return []
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._index.get(cas_key, []):
            adapter = str(row.get("adapter_id") or "")
            if adapters and adapter not in adapters:
                continue
            query_type = str(row.get("query_type") or "")
            latest[(adapter, query_type)] = row
        conflicts = [
            row
            for row in latest.values()
            if str(row.get("inchikey") or "").strip()
            and str(row.get("inchikey") or "").strip().upper() != candidate_key
        ]
        return sorted(
            conflicts,
            key=lambda row: (
                str(row.get("adapter_id") or ""),
                str(row.get("query_type") or ""),
                str(row.get("evidence_id") or ""),
                str(row.get("inchikey") or ""),
            ),
        )

    def _snapshot_cas_conflict_evidence(
        self,
        *,
        inchikey: str,
        cas: str | None,
        adapters: set[str],
        tox_adapters: set[str],
    ) -> tuple[list[EvidenceHit], list[EvidenceHit]]:
        """Create conservative risk rows and non-scoring CAS conflict audits."""

        conflicts = self._active_snapshot_cas_conflicts(
            inchikey=inchikey,
            cas=cas,
            adapters=adapters,
        )
        if not conflicts:
            return [], []

        risk_hits: list[EvidenceHit] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in conflicts:
            adapter = str(row.get("adapter_id") or "snapshot_identity_v1")
            grouped.setdefault(adapter, []).append(row)
            if (
                adapter not in tox_adapters
                or str(row.get("query_type") or "") != "tox"
                or str(row.get("evidence_role") or "task_evidence")
                not in {"task_evidence", "risk_signal"}
                or str(row.get("direction") or "unknown")
                not in _CONSERVATIVE_RISK_DIRECTIONS
                or str(row.get("query_status") or "not_queried")
                not in {"hit", "exact_hit", "analogue_hit"}
            ):
                continue
            hit = self._snapshot_hit(
                adapter=adapter,
                row=row,
                key=str(cas or ""),
                query_type="tox",
            )
            hit.payload = {
                **hit.payload,
                "identity_resolution": "cas_conflict_conservative_risk_only",
                "candidate_inchikey": str(inchikey or ""),
                "snapshot_inchikey": str(row.get("inchikey") or ""),
                "lookup_field": "cas",
                "lookup_value": str(cas or ""),
            }
            hit.lookup_field = "cas"
            hit.lookup_value = str(cas or "")
            hit.match_type = "cas_identifier_conflict_conservative_risk"
            hit.claim_ceiling = "candidate_risk_signal_only_not_safety_clearance"
            risk_hits.append(hit)

        audits: list[EvidenceHit] = []
        for adapter in sorted(grouped):
            rows = grouped[adapter]
            snapshot_keys = sorted(
                {
                    str(row.get("inchikey") or "").strip()
                    for row in rows
                    if str(row.get("inchikey") or "").strip()
                }
            )
            evidence_ids = sorted(
                str(row.get("evidence_id") or "")
                for row in rows
                if str(row.get("evidence_id") or "")
            )
            retained_risk_ids = sorted(
                hit.evidence_id
                for hit in risk_hits
                if hit.adapter_id == adapter
            )
            payload = {
                "reason": "cas_snapshot_inchikey_conflict",
                "lookup_field": "cas",
                "lookup_value": str(cas or ""),
                "candidate_inchikey": str(inchikey or ""),
                "snapshot_inchikeys": snapshot_keys,
                "conflicting_evidence_ids": evidence_ids,
                "conservative_risk_evidence_ids": retained_risk_ids,
                "claims": (
                    "benefit_novelty_and_safety_lift_blocked; "
                    "conservative_toxicity_risk_only"
                ),
            }
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            retrieved_values = sorted(
                str(row.get("retrieved_at") or "")
                for row in rows
                if str(row.get("retrieved_at") or "")
            )
            source_urls = sorted(
                str(row.get("source_url") or "")
                for row in rows
                if str(row.get("source_url") or "")
            )
            audits.append(
                _finalize_hit(
                    EvidenceHit(
                        adapter_id=adapter,
                        provider_id=str(rows[-1].get("provider_id") or ""),
                        query_type="query_audit",
                        score=0.0,
                        confidence=0.0,
                        evidence_id=f"snapshot:cas_identity_review:{digest[:20]}",
                        payload=payload,
                        endpoint="snapshot_identity_resolution",
                        direction="unknown",
                        evidence_role="query_audit",
                        provenance_status="audited",
                        source_url=source_urls[0] if source_urls else "",
                        retrieved_at=(
                            retrieved_values[-1] if retrieved_values else _utc_now()
                        ),
                        adapter_version="snapshot_cas_identity_guard_v1",
                        source_version="snapshot_cas_identity_guard_v1",
                        response_sha256=digest,
                        query_status="identity_review_required",
                        evidence_type="query_audit",
                        lookup_field="cas",
                        lookup_value=str(cas or ""),
                        match_type="cas_identifier_conflict",
                        accession=",".join(evidence_ids),
                        claim_ceiling=(
                            "identity_review_only_no_efficacy_or_safety_claim"
                        ),
                    )
                )
            )
        return risk_hits, audits

    def _from_snapshot(
        self,
        key: str,
        adapters: set[str],
        query_type: str,
        *,
        candidate_inchikey: str = "",
        lookup_field: str = "inchikey",
    ) -> list[EvidenceHit]:
        # 同 adapter+query_type 保留最后一条，使 auto_cache 追加可覆盖旧快照
        latest: dict[str, dict[str, Any]] = {}
        for row in self._index.get(key, []):
            adapter = row.get("adapter_id", "")
            if adapters and adapter not in adapters:
                continue
            qt = row.get("query_type") or query_type
            if qt != query_type:
                continue
            row_inchikey = str(row.get("inchikey") or "").strip().upper()
            if (
                lookup_field == "cas"
                and candidate_inchikey
                and row_inchikey
                and row_inchikey != candidate_inchikey.strip().upper()
            ):
                # CAS conflicts are handled once by
                # _snapshot_cas_conflict_evidence.  Do not let the ordinary
                # fallback path import benefit, novelty or safety evidence.
                continue
            latest[str(adapter)] = row

        hits = [
            self._snapshot_hit(
                adapter=adapter,
                row=row,
                key=key,
                query_type=query_type,
            )
            for adapter, row in latest.items()
        ]
        for hit in hits:
            # Preserve empty direct-structure metadata for the standalone
            # resolver to stamp its more precise original-vs-standardized
            # identity basis.  CAS fallback is resolved here and must carry
            # its actual lookup field immediately.
            if lookup_field == "cas" and not hit.lookup_field:
                hit.lookup_field = lookup_field
            if lookup_field == "cas" and not hit.lookup_value:
                hit.lookup_value = key
            if lookup_field == "cas" and not hit.match_type:
                hit.match_type = "cas_identifier"
        return hits

    def _record_live_failure(self) -> None:
        self._live_failures += 1
        if self._live_failures >= self._fail_threshold:
            self._circuit_open = True
            self.cfg.mark_degraded("evidence_live_circuit_open")

    def _record_live_success(self) -> None:
        self._live_successes += 1

    def _chembl_lipid(
        self,
        client: httpx.Client,
        inchikey: str,
        *,
        api_base: str = "https://www.ebi.ac.uk/chembl/api/data",
        before_request: Any | None = None,
    ) -> list[EvidenceHit]:
        if not inchikey:
            return []
        base = str(api_base or "https://www.ebi.ac.uk/chembl/api/data").rstrip("/")
        url = f"{base}/molecule/{inchikey}.json"
        if before_request is not None:
            before_request()
        resp = client.get(url)
        if resp.status_code == 404:
            return [
                _query_audit_hit(
                    adapter_id="chembl_lipid_v1",
                    query_status="verified_empty",
                    evidence_id=f"chembl:not_found:{inchikey}",
                    payload={"reason": "inchikey_not_found"},
                    source_url=url,
                    response_content=resp.content,
                )
            ]
        resp.raise_for_status()
        mol = resp.json()
        chembl_id = mol.get("molecule_chembl_id")
        if not chembl_id:
            return [
                _query_audit_hit(
                    adapter_id="chembl_lipid_v1",
                    query_status="verified_empty",
                    evidence_id=f"chembl:no_entity_id:{inchikey}",
                    payload={"reason": "response_missing_molecule_chembl_id"},
                    source_url=url,
                    response_content=resp.content,
                )
            ]

        act_url = f"{base}/activity.json"
        limit = 100
        max_activities = int(self.cfg.evidence.get("chembl_max_activities", 500))
        activities: list[dict[str, Any]] = []
        response_hashes = [_response_sha256(resp.content)]
        for offset in range(0, max_activities, limit):
            params = {
                "molecule_chembl_id": chembl_id,
                "limit": min(limit, max_activities - offset),
                "offset": offset,
            }
            if before_request is not None:
                before_request()
            act_resp = client.get(act_url, params=params)
            act_resp.raise_for_status()
            response_hashes.append(_response_sha256(act_resp.content))
            page = act_resp.json().get("activities") or []
            activities.extend(item for item in page if isinstance(item, dict))
            if len(page) < params["limit"]:
                break
        positive_hits: list[dict[str, Any]] = []
        adverse_hits: list[dict[str, Any]] = []
        mechanism_hits: list[dict[str, Any]] = []
        annotation_hits = 0
        for act in activities:
            structured = {
                "target_pref_name": str(act.get("target_pref_name") or "")[:120],
                "assay_description": str(act.get("assay_description") or "")[:500],
                "activity_comment": str(act.get("activity_comment") or "")[:300],
                "standard_type": act.get("standard_type"),
                "standard_relation": act.get("standard_relation"),
                "standard_value": act.get("standard_value"),
                "standard_units": act.get("standard_units"),
                "pchembl_value": act.get("pchembl_value"),
                "assay_type": act.get("assay_type"),
                "assay_chembl_id": act.get("assay_chembl_id"),
                "bao_label": act.get("bao_label"),
            }
            classification = _classify_chembl_activity(act)
            if classification == "positive_phenotype":
                positive_hits.append(structured)
            elif classification == "adverse_phenotype":
                adverse_hits.append(structured)
            elif classification == "mechanism":
                mechanism_hits.append(structured)
            else:
                annotation_hits += 1

        retrieved_at = _utc_now()
        combined_hash = hashlib.sha256("|".join(response_hashes).encode()).hexdigest()

        hits: list[EvidenceHit] = []
        if positive_hits:
            count = len(positive_hits)
            hits.append(
                EvidenceHit(
                    adapter_id="chembl_lipid_v1",
                    query_type="lipid",
                    score=min(0.75, 0.35 + 0.08 * count),
                    confidence=min(0.82, 0.45 + 0.07 * count),
                    evidence_id=f"chembl:{chembl_id}:lipid_phenotype",
                    payload={
                        "chembl_id": chembl_id,
                        "positive_phenotype_hits": count,
                        "structured_hits": positive_hits[:20],
                        "activities_examined": len(activities),
                    },
                    endpoint="cellular_lipid_reduction",
                    direction="supports",
                    evidence_role="task_evidence",
                    provenance_status="retrieved",
                    source_url=act_url,
                    retrieved_at=retrieved_at,
                    adapter_version="chembl_lipid_v3",
                    query_params={"max_activities": max_activities, "page_limit": limit},
                    response_sha256=combined_hash,
                    license="ChEMBL data license/CC BY-SA 3.0",
                    query_status="exact_hit",
                )
            )
        if adverse_hits:
            count = len(adverse_hits)
            hits.append(
                EvidenceHit(
                    adapter_id="chembl_lipid_v1",
                    query_type="tox",
                    score=min(0.85, 0.45 + 0.10 * count),
                    confidence=min(0.85, 0.60 + 0.06 * count),
                    evidence_id=f"chembl:{chembl_id}:adverse_lipid",
                    payload={
                        "chembl_id": chembl_id,
                        "adverse_phenotype_hits": count,
                        "structured_hits": adverse_hits[:20],
                        "activities_examined": len(activities),
                    },
                    endpoint="adverse_lipid_phenotype",
                    direction="risk",
                    evidence_role="task_evidence",
                    provenance_status="retrieved",
                    source_url=act_url,
                    retrieved_at=retrieved_at,
                    adapter_version="chembl_lipid_v3",
                    query_params={"max_activities": max_activities, "page_limit": limit},
                    response_sha256=combined_hash,
                    license="ChEMBL data license/CC BY-SA 3.0",
                    query_status="exact_hit",
                )
            )
        if mechanism_hits:
            hits.append(
                EvidenceHit(
                    adapter_id="chembl_lipid_v1",
                    query_type="pathway",
                    score=min(0.35, 0.15 + 0.03 * len(mechanism_hits)),
                    confidence=min(0.60, 0.30 + 0.04 * len(mechanism_hits)),
                    evidence_id=f"chembl:{chembl_id}:mechanism",
                    payload={
                        "chembl_id": chembl_id,
                        "mechanism_hits": len(mechanism_hits),
                        "structured_hits": mechanism_hits[:20],
                        "activities_examined": len(activities),
                    },
                    endpoint="lipid_mechanism_association",
                    direction="unknown",
                    evidence_role="mechanism_support",
                    provenance_status="retrieved",
                    source_url=act_url,
                    retrieved_at=retrieved_at,
                    adapter_version="chembl_lipid_v3",
                    query_params={"max_activities": max_activities, "page_limit": limit},
                    response_sha256=combined_hash,
                    license="ChEMBL data license/CC BY-SA 3.0",
                    query_status="exact_hit",
                )
            )
        if not hits:
            hits.append(
                EvidenceHit(
                    adapter_id="chembl_lipid_v1",
                    query_type="annotation",
                    score=0.0,
                    confidence=0.0,
                    evidence_id=f"chembl:{chembl_id}:present",
                    payload={
                        "chembl_id": chembl_id,
                        "positive_phenotype_hits": 0,
                        "adverse_phenotype_hits": 0,
                        "mechanism_hits": 0,
                        "annotation_hits": annotation_hits,
                        "activities_examined": len(activities),
                    },
                    endpoint="database_annotation",
                    direction="unknown",
                    evidence_role="annotation_only",
                    provenance_status="retrieved",
                    source_url=url,
                    retrieved_at=retrieved_at,
                    adapter_version="chembl_lipid_v3",
                    query_params={"max_activities": max_activities, "page_limit": limit},
                    response_sha256=combined_hash,
                    license="ChEMBL data license/CC BY-SA 3.0",
                    query_status="annotation_only",
                )
            )
        return hits

    def _pubchem_tox(
        self,
        client: httpx.Client,
        inchikey: str,
        *,
        lookup_field: str = "inchikey",
        lookup_value: str | None = None,
        api_base: str = "https://pubchem.ncbi.nlm.nih.gov/rest/pug",
        before_request: Any | None = None,
    ) -> list[EvidenceHit]:
        """Fetch PubChem toxicology using the identity actually selected.

        The historical scoring path still calls this with an InChIKey.  The
        standalone evidence-query tool may conservatively fall back to CAS,
        which PubChem exposes through the ``name`` namespace.  Keeping the
        lookup basis explicit prevents a configured CAS fallback from being
        silently sent to the InChIKey endpoint.
        """
        identity_value = str(lookup_value or inchikey or "").strip()
        if not identity_value:
            return []
        identity_namespace = "name" if lookup_field == "cas" else "inchikey"
        encoded_identity = quote(identity_value, safe="")
        base = str(api_base or "https://pubchem.ncbi.nlm.nih.gov/rest/pug").rstrip("/")
        cid_url = f"{base}/compound/{identity_namespace}/{encoded_identity}/cids/JSON"
        if before_request is not None:
            before_request()
        cid_resp = client.get(cid_url)
        if cid_resp.status_code == 404:
            return [
                _query_audit_hit(
                    adapter_id="pubchem_tox_v1",
                    query_status="verified_empty",
                    evidence_id=f"pubchem:not_found:{identity_value}",
                    payload={
                        "reason": "identity_not_found",
                        "lookup_field": lookup_field,
                        "lookup_value": identity_value,
                    },
                    source_url=cid_url,
                    response_content=cid_resp.content,
                )
            ]
        cid_resp.raise_for_status()
        cids = (cid_resp.json().get("IdentifierList") or {}).get("CID") or []
        if not cids:
            return [
                _query_audit_hit(
                    adapter_id="pubchem_tox_v1",
                    query_status="verified_empty",
                    evidence_id=f"pubchem:no_cid:{identity_value}",
                    payload={
                        "reason": "response_missing_cid",
                        "lookup_field": lookup_field,
                        "lookup_value": identity_value,
                    },
                    source_url=cid_url,
                    response_content=cid_resp.content,
                )
            ]
        unique_cids = sorted({int(value) for value in cids})
        if len(unique_cids) != 1:
            # 一个查询身份解析到多个 PubChem 实体时，不得任取第一个继续产生
            # 毒性或效力分。保留身份歧义供人工核对原始/标准化/盐型结构。
            return [
                EvidenceHit(
                    adapter_id="pubchem_tox_v1",
                    query_type="query_audit",
                    score=0.0,
                    confidence=0.0,
                    evidence_id=f"pubchem:identity_review:{identity_value}",
                    payload={
                        "cids": unique_cids,
                        "count": len(unique_cids),
                        "lookup_field": lookup_field,
                        "lookup_value": identity_value,
                    },
                    endpoint="identity_resolution",
                    direction="unknown",
                    evidence_role="query_audit",
                    provenance_status="retrieved",
                    source_url=cid_url,
                    retrieved_at=_utc_now(),
                    adapter_version="pubchem_tox_v3",
                    query_params={lookup_field: identity_value},
                    response_sha256=_response_sha256(cid_resp.content),
                    license="PubChem public data; source-specific rights may apply",
                    query_status="identity_review_required",
                )
            ]
        cid = unique_cids[0]

        view_base = (
            base.replace("/rest/pug", "/rest/pug_view")
            if base.endswith("/rest/pug")
            else base
        )
        view_url = f"{view_base}/data/compound/{cid}/JSON"
        if before_request is not None:
            before_request()
        view_resp = client.get(view_url, params={"heading": "GHS Classification"})
        payload: dict[str, Any] | None = None
        response_hash = ""
        alt: httpx.Response | None = None
        if view_resp.status_code == 200:
            payload = view_resp.json()
            response_hash = _response_sha256(view_resp.content)
        else:
            if before_request is not None:
                before_request()
            alt = client.get(view_url, params={"heading": "Toxicity"})
            if alt.status_code == 200:
                payload = alt.json()
                response_hash = _response_sha256(alt.content)

        if not payload:
            # A genuine missing heading may be cached as verified_empty.  HTTP
            # auth/rate/server failures are transport failures and must flow to
            # retry/backoff instead of masquerading as "no toxicology record".
            if alt is not None and alt.status_code not in {200, 404}:
                alt.raise_for_status()
            if view_resp.status_code not in {200, 404}:
                view_resp.raise_for_status()
            return [
                _query_audit_hit(
                    adapter_id="pubchem_tox_v1",
                    query_status="verified_empty",
                    evidence_id=f"pubchem:{cid}:no_toxicology_section",
                    payload={"cid": cid, "reason": "no_ghs_or_toxicity_payload"},
                    source_url=view_url,
                    response_content=view_resp.content,
                )
            ]

        structured_strings = _collect_pubchem_strings(payload)
        normalized = [
            (path, value, f"{path} {value}".lower()) for path, value in structured_strings
        ]

        hazard_score = 0.0
        flags: list[str] = []
        matched_nodes: list[dict[str, str]] = []
        joined = " ".join(item[2] for item in normalized)
        dili_negative = any(
            "dilist classification" in path.lower()
            and re.search(r"\bdili\s+negative\b", low)
            for path, _value, low in normalized
        )
        dili_positive = any(
            "dilist classification" in path.lower()
            and re.search(r"\bdili\s+(?:positive|severity|severe|moderate)\b", low)
            for path, _value, low in normalized
        )
        if not dili_negative and (
            "hepatotox" in joined or ("liver" in joined and "tox" in joined)
        ):
            hazard_score += 0.35
            flags.append("liver")
        if dili_positive:
            hazard_score += 0.35
            flags.append("dili_positive")
        if re.search(r"\bdanger\b", joined):
            hazard_score += 0.2
            flags.append("ghs_danger")
        if re.search(r"\bwarning\b", joined):
            hazard_score += 0.1
            flags.append("ghs_warning")
        if "carcinogen" in joined:
            hazard_score += 0.25
            flags.append("carcinogen")
        if "acute toxicity" in joined:
            hazard_score += 0.15
            flags.append("acute_toxicity")
        for path, value, low in normalized:
            if dili_negative and "dilist classification" in path.lower() and "negative" in low:
                continue
            if any(
                marker in low
                for marker in ("danger", "warning", "hepatotox", "liver", "carcinogen", "acute toxicity")
            ):
                matched_nodes.append({"path": path, "value": value[:300]})

        if hazard_score <= 0:
            return [
                EvidenceHit(
                    adapter_id="pubchem_tox_v1",
                    query_type="annotation",
                    score=0.0,
                    confidence=0.0,
                    evidence_id=f"pubchem:{cid}:no_structured_hazard",
                    payload={
                        "cid": cid,
                        "flags": flags,
                        "dili_classification": "negative" if dili_negative else "",
                        "nodes_examined": len(structured_strings),
                    },
                    endpoint="database_annotation",
                    direction="unknown",
                    evidence_role="annotation_only",
                    provenance_status="retrieved",
                    source_url=view_url,
                    retrieved_at=_utc_now(),
                    adapter_version="pubchem_tox_v3",
                    query_params={"heading": "GHS Classification|Toxicity"},
                    response_sha256=response_hash,
                    license="PubChem public data; source-specific rights may apply",
                    query_status="annotation_only",
                )
            ]

        return [
            EvidenceHit(
                adapter_id="pubchem_tox_v1",
                query_type="tox",
                score=min(1.0, hazard_score),
                confidence=0.55,
                evidence_id=f"pubchem:{cid}:ghs",
                payload={
                    "cid": cid,
                    "flags": flags,
                    "matched_nodes": matched_nodes[:20],
                    "nodes_examined": len(structured_strings),
                },
                endpoint="hazard_classification",
                direction="risk",
                evidence_role="task_evidence",
                provenance_status="retrieved",
                source_url=view_url,
                retrieved_at=_utc_now(),
                adapter_version="pubchem_tox_v3",
                query_params={"heading": "GHS Classification|Toxicity"},
                response_sha256=response_hash,
                license="PubChem public data; source-specific rights may apply",
                query_status="exact_hit",
            )
        ]

    def _try_live(self, *, inchikey: str, cas: str | None, smiles: str) -> list[EvidenceHit]:
        _ = (cas, smiles)
        if not self.cfg.allow_live_evidence:
            return []
        if self._circuit_open:
            self.cfg.mark_degraded("evidence_live_circuit_open")
            return [
                _query_audit_hit(
                    adapter_id="evidence_live_v1",
                    query_status="not_queried",
                    evidence_id=f"evidence_live:circuit_open:{inchikey}",
                    payload={"reason": "circuit_open", "retry_required": True},
                    provenance_status="query_failed",
                )
            ]
        adapters = self._effective_adapters()
        hits: list[EvidenceHit] = []
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                if "chembl_lipid_v1" in adapters and inchikey:
                    hits.extend(self._chembl_lipid(client, inchikey))
                if "pubchem_tox_v1" in adapters and inchikey:
                    hits.extend(self._pubchem_tox(client, inchikey))
            self._record_live_success()
            return hits
        except Exception as exc:
            query_status = "adapter_error"
            status_code = None
            if isinstance(exc, httpx.TimeoutException):
                query_status = "timeout"
            elif isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                if status_code == 429:
                    query_status = "rate_limited"
            self._record_live_failure()
            self.cfg.mark_degraded("evidence_live")
            self.cfg.mark_degraded(f"evidence_live_{query_status}")
            try:
                request = exc.request if isinstance(exc, httpx.HTTPError) else None
            except RuntimeError:
                request = None
            source_url = str(request.url) if request is not None else ""
            return [
                _query_audit_hit(
                    adapter_id="evidence_live_v1",
                    query_status=query_status,
                    evidence_id=f"evidence_live:{query_status}:{inchikey}",
                    payload={
                        "exception_type": type(exc).__name__,
                        "http_status": status_code,
                        "retry_required": True,
                    },
                    source_url=source_url,
                    provenance_status="query_failed",
                )
            ]

    def query(
        self,
        *,
        inchikey: str,
        cas: str | None,
        smiles: str,
        allow_live: bool = False,
    ) -> EvidenceBundle:
        if not self.enabled:
            return EvidenceBundle(
                normalized_inchikey=inchikey,
                input_structure_hash=_input_structure_hash(
                    inchikey=inchikey, smiles=smiles, cas=cas
                ),
                queried_at=_utc_now(),
            )

        prefer_snapshot = bool(self.cfg.evidence.get("prefer_snapshot", True))
        use_snapshot = bool(self.cfg.evidence.get("use_snapshot", True))
        if not use_snapshot:
            prefer_snapshot = False
        epa_stage = int((self.cfg.evidence.get("epa_ctx") or {}).get("integration_stage", 0))
        structure_key = _input_structure_hash(
            inchikey=inchikey,
            smiles=smiles,
            cas=cas,
        )
        cache_key = (
            f"{structure_key}|{int(allow_live)}|{self.cfg.mode}|"
            f"{int(prefer_snapshot)}|{int(use_snapshot)}|epa_stage={epa_stage}"
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        adapters = self._effective_adapters()
        lipid_adapters = adapters & {"chembl_lipid_v1", "ot_target_v1", "nafldkb_v1"}
        tox_adapters = adapters & {"chembl_lipid_v1", "pubchem_tox_v1", "dili_table_v1", "epa_ctx_tox_v1"}

        bundle = EvidenceBundle(
            normalized_inchikey=inchikey,
            input_structure_hash=structure_key,
            queried_at=_utc_now(),
        )
        lookups = [
            (lookup_field, key)
            for lookup_field, key in (("inchikey", inchikey), ("cas", cas))
            if key
        ]
        def fill_from_snapshot() -> None:
            conflict_risk, conflict_audits = self._snapshot_cas_conflict_evidence(
                inchikey=inchikey,
                cas=cas,
                adapters=adapters,
                tox_adapters=tox_adapters,
            )
            for lookup_field, key in lookups:
                if not bundle.lipid:
                    bundle.lipid.extend(
                        self._from_snapshot(
                            key,
                            lipid_adapters,
                            "lipid",
                            candidate_inchikey=inchikey,
                            lookup_field=lookup_field,
                        )
                    )
                if not bundle.tox:
                    bundle.tox.extend(
                        self._from_snapshot(
                            key,
                            tox_adapters,
                            "tox",
                            candidate_inchikey=inchikey,
                            lookup_field=lookup_field,
                        )
                    )
                if not bundle.novelty:
                    bundle.novelty.extend(
                        self._from_snapshot(
                            key,
                            adapters,
                            "novelty",
                            candidate_inchikey=inchikey,
                            lookup_field=lookup_field,
                        )
                    )
                if not bundle.pathway:
                    bundle.pathway.extend(
                        self._from_snapshot(
                            key,
                            {"chembl_lipid_v1", "kegg_pathway_v1"},
                            "pathway",
                            candidate_inchikey=inchikey,
                            lookup_field=lookup_field,
                        )
                    )
                if not bundle.annotation:
                    bundle.annotation.extend(
                        self._from_snapshot(
                            key,
                            adapters,
                            "annotation",
                            candidate_inchikey=inchikey,
                            lookup_field=lookup_field,
                        )
                    )
                if not bundle.query_audit:
                    bundle.query_audit.extend(
                        self._from_snapshot(
                            key,
                            adapters | {"bake_miss_v1"},
                            "query_audit",
                            candidate_inchikey=inchikey,
                            lookup_field=lookup_field,
                        )
                    )

            def append_unique(target: list[EvidenceHit], hits: list[EvidenceHit]) -> None:
                existing_ids = {hit.evidence_id for hit in target}
                for hit in hits:
                    if hit.evidence_id not in existing_ids:
                        target.append(hit)
                        existing_ids.add(hit.evidence_id)

            # Add conflicts after ordinary snapshot reads so a review audit
            # cannot suppress a valid pre-existing query-audit row.  Adverse
            # toxicology is the only scientific signal retained across the
            # ambiguity, under the project's conservative-risk rule.
            append_unique(bundle.tox, conflict_risk)
            append_unique(bundle.query_audit, conflict_audits)

        if use_snapshot and prefer_snapshot:
            fill_from_snapshot()

        # EvidenceFacade is the deterministic local scientific reader.  Live
        # transport is owned by EvidenceGateway/query_evidence (or explicit
        # bake), where identity, cache, provider limits and audit states are
        # enforced uniformly.  Keep the argument for API compatibility but do
        # not let it resurrect the historical implicit ranking side effect.
        if allow_live and self.cfg.allow_live_evidence:
            self.cfg.mark_degraded("legacy_facade_live_blocked")

        if use_snapshot and not prefer_snapshot:
            fill_from_snapshot()

        self._merge_local_tables(bundle, inchikey=inchikey, smiles=smiles)
        self._merge_public_assay_grain(bundle, inchikey=inchikey)
        self._merge_epa_ctx(bundle, inchikey=inchikey, cas=cas, smiles=smiles)
        self._merge_dilirank_gate(bundle, inchikey=inchikey, cas=cas)
        bundle.evidence_source_audit = self._build_evidence_source_audit(bundle)

        for hit in bundle.all_hits():
            _finalize_hit(hit)
        bundle.annotate_evidence_types()
        bundle.source_versions = bundle.collect_source_versions()
        if not bundle.queried_at:
            bundle.queried_at = _utc_now()

        ranking_hits = [*bundle.lipid, *bundle.tox]
        if any(
            not hit.source_url or not hit.retrieved_at or not hit.response_sha256
            for hit in ranking_hits
        ):
            self.cfg.mark_degraded("evidence_provenance_incomplete")

        self._cache[cache_key] = bundle
        return bundle

    def _merge_dilirank_gate(
        self,
        bundle: EvidenceBundle,
        *,
        inchikey: str,
        cas: str | None,
    ) -> None:
        """Exact-identity DILIrank gate: Most may hard-exclude; never safety lift."""
        gate_cfg = self.cfg.evidence.get("dilirank_exact_gate") or {}
        enabled = bool(gate_cfg.get("enabled", True))
        from plugins.molmind_core.scientific.evidence_facade.dilirank_gate import audit_from_match

        if not enabled or self._dilirank_gate_index is None:
            bundle.dili_audit = audit_from_match(
                None, enabled=False, hard_exclude_most=False
            )
            return
        extra: list[str] = []
        epa = bundle.epa_audit or {}
        for key in ("original_inchikey", "standardized_inchikey"):
            value = str(epa.get(key) or "").strip()
            if value:
                extra.append(value)
        match = self._dilirank_gate_index.lookup(
            inchikey=inchikey,
            cas=cas,
            extra_inchikeys=extra,
        )
        bundle.dili_audit = audit_from_match(
            match,
            enabled=True,
            hard_exclude_most=bool(gate_cfg.get("hard_exclude_most", True)),
        )
        if match is not None:
            bundle.annotation.append(
                EvidenceHit(
                    adapter_id="dilirank_exact_gate_v1",
                    query_type="annotation",
                    score=0.0,
                    confidence=0.0,
                    evidence_id=(
                        f"dilirank_exact:{match.ltkb_id or match.inchikey}:"
                        f"{match.concern}"
                    ),
                    payload=dict(bundle.dili_audit),
                    endpoint="dilirank_concern",
                    direction="risk" if match.concern == "most" else "unknown",
                    evidence_role="annotation_only",
                    provenance_status="retrieved",
                    source_url=(
                        "https://www.fda.gov/science-research/"
                        "liver-toxicity-knowledge-base-ltkb/"
                        "drug-induced-liver-injury-rank-dilirank-20-dataset"
                    ),
                    retrieved_at=_utc_now(),
                    adapter_version="dilirank_exact_gate_v1",
                    source_version="fda_dilirank_2",
                    query_params={"inchikey": inchikey, "cas": cas},
                    query_status="annotation_only",
                    evidence_type="identity_annotation",
                )
            )

    def _build_evidence_source_audit(self, bundle: EvidenceBundle) -> dict[str, object]:
        """Summarize ChEMBL / PubChem / BindingDB query outcomes for PDF/CSV."""

        def summarize(adapter_substrings: tuple[str, ...]) -> dict[str, object]:
            hits = [
                hit
                for hit in bundle.all_hits()
                if any(token in str(hit.adapter_id or "").lower() for token in adapter_substrings)
            ]
            if not hits:
                return {
                    "status": "not_queried",
                    "hit_count": 0,
                    "ranking_effect": "none",
                }
            statuses = [str(hit.query_status or "") for hit in hits]
            roles = [str(hit.evidence_role or "") for hit in hits]
            scored = sum(1 for hit in hits if float(hit.score or 0.0) > 0)
            if any(status == "exact_hit" for status in statuses):
                status = "exact_hit"
            elif any(status == "verified_empty" for status in statuses):
                status = "verified_empty"
            elif any(status == "identity_review_required" for status in statuses):
                status = "identity_review_required"
            elif any(status == "annotation_only" for status in statuses):
                status = "annotation_only"
            else:
                status = statuses[0] or "not_queried"
            return {
                "status": status,
                "hit_count": len(hits),
                "scored_hit_count": scored,
                "roles": sorted({role for role in roles if role}),
                "ranking_effect": "score" if scored else "annotation_or_audit_only",
            }

        return {
            "chembl": summarize(("chembl",)),
            "pubchem": summarize(("pubchem",)),
            "bindingdb": summarize(("bindingdb",)),
            "policy": {
                "chembl_lipid_live_or_snapshot": "primary_lipid_evidence",
                "pubchem_tox_live_or_snapshot": "primary_tox_identity",
                "bindingdb_assay_grain": "mechanism_support_score_0",
                "pubchem_bioassay_grain": "annotation_until_candidate_empty_coverage",
                "dilirank": "exact_gate_only",
            },
        }

    def _merge_epa_ctx(
        self,
        bundle: EvidenceBundle,
        *,
        inchikey: str,
        cas: str | None,
        smiles: str | None = None,
    ) -> None:
        """Attach EPA CTX evidence according to the configured integration stage.

        Stage 1 is annotation-only and cannot change any score.  Stage 2 may add
        a bounded toxicity risk only when nhit>0 and cytotoxLowerUm is at or
        below the fixed screening concentration.  activeMc/activeSc alone stay
        bioactivity annotations.  CAS-only identity uncertainty is exported in
        ``epa_audit`` / CSV but never cancels candidate eligibility.
        """
        epa_cfg = self.cfg.evidence.get("epa_ctx") or {}
        stage = int(epa_cfg.get("integration_stage", 0))
        if stage <= 0 or not bool(epa_cfg.get("enabled", True)):
            bundle.epa_audit = {"stage": 0, "status": "disabled"}
            return
        screening_um = float(epa_cfg.get("cytotox_screening_um", 10.0))
        entry = self._epa_index.lookup(
            inchikey=inchikey,
            cas=cas,
            smiles=smiles,
            share_standardized_smiles_risk=bool(
                epa_cfg.get("share_risk_across_standardized_smiles", True)
            ),
            screening_um=screening_um,
        )
        if entry is None:
            bundle.epa_audit = {
                "stage": stage,
                "status": "audit_missing",
                "query_status": "not_queried",
                "mapping_status": "audit_missing",
                "missing_semantics": "audit_missing",
                "ranking_effect": "none" if stage == 1 else "cytotox_risk_only",
            }
            return

        mapping_status = str(entry.get("mapping_status") or "audit_missing")
        matched_identity_type = str(entry.get("_matched_identity_type") or "")
        matched_identity_basis = str(entry.get("_matched_identity_basis") or "")
        inherited_from = str(entry.get("risk_inherited_from_dtxsid") or "")
        exact_identity = (
            mapping_status == "exact_identifier_match"
            and matched_identity_basis in {"original_inchikey", "standardized_inchikey"}
        ) or (
            bool(inherited_from)
            and str(entry.get("risk_inheritance_basis") or "") == "standardized_smiles"
        )
        assay_rows = list(entry.get("assay_rows") or [])
        active_threshold = float(epa_cfg.get("active_hit_threshold", 0.90))
        qc_active_count = 0
        for row in assay_rows:
            if bool(row.get("active_hit")) or row.get("classification") == "active_risk":
                qc_active_count += 1
                continue
            try:
                if float(row.get("hitc")) >= active_threshold:
                    qc_active_count += 1
            except (TypeError, ValueError):
                continue
        metrics = epa_cytotox_metrics(entry)
        active_count = max(int(metrics["active_hit_count"]), qc_active_count)
        entry = {**entry, "active_hit_count": active_count}
        record_count = int(entry.get("bioactivity_record_count") or 0)
        risk_tier = epa_cytotox_risk_tier(entry, screening_um=screening_um)
        bioactivity_signal = active_count > 0 or risk_tier != "none"
        summary_status = str(entry.get("summary_status") or "not_queried")
        identity_uncertain = (
            not exact_identity
            and mapping_status != "audit_missing"
            and not inherited_from
        )
        if identity_uncertain:
            query_status = "identity_review_required"
        elif risk_tier == "strong_risk":
            query_status = "exact_hit"
        elif summary_status == "verified_empty":
            query_status = "verified_empty"
        elif exact_identity or bioactivity_signal:
            query_status = "exact_hit" if exact_identity else "annotation_only"
        else:
            query_status = "not_queried"

        if risk_tier == "strong_risk":
            audit_status = "cytotox_strong_risk"
        elif risk_tier == "weak_risk_review":
            audit_status = "cytotox_weak_review"
        elif bioactivity_signal:
            audit_status = "bioactivity_annotation"
        elif summary_status == "verified_empty":
            audit_status = "verified_empty"
        else:
            audit_status = "mapped"

        audit = {
            "stage": stage,
            "status": audit_status,
            "query_status": query_status,
            "mapping_status": mapping_status,
            "mapping_basis": entry.get("mapping_basis") or "",
            "mapping_value": entry.get("mapping_value") or "",
            "matched_identity_type": matched_identity_type,
            "matched_identity_basis": matched_identity_basis,
            "matched_key": entry.get("_matched_key") or "",
            "original_inchikey": entry.get("original_inchikey") or "",
            "standardized_inchikey": entry.get("standardized_inchikey") or "",
            "standardized_smiles": entry.get("standardized_smiles") or smiles or "",
            "cas": entry.get("cas") or entry.get("casrn") or "",
            "dtxsid": entry.get("dtxsid") or "",
            "preferred_name": entry.get("preferred_name") or "",
            "summary_status": summary_status,
            "bioactivity_record_count": record_count,
            "active_hit_count": active_count,
            "nhit": metrics["nhit"],
            "cytotox_lower_um": metrics["cytotox_lower_um"],
            "cytotox_median_um": metrics["cytotox_median_um"],
            "cytotox_screening_um": screening_um,
            "cytotox_risk_tier": risk_tier,
            "active_aeids": list(entry.get("active_aeids") or []),
            "toxval_record_count": int(entry.get("toxval_record_count") or 0),
            "toxref_summary_record_count": int(entry.get("toxref_summary_record_count") or 0),
            "interpretation": entry.get("interpretation")
            or (
                "cytotox_strong_risk"
                if risk_tier == "strong_risk"
                else (
                    "cytotox_weak_review"
                    if risk_tier == "weak_risk_review"
                    else (
                        "bioactivity_annotation"
                        if bioactivity_signal
                        else "audit_missing_or_no_active_hit"
                    )
                )
            ),
            "missing_semantics": "audit_missing",
            "ranking_effect": "none" if stage == 1 else "cytotox_risk_only",
            "risk_applied": False,
            "risk_inherited_from_dtxsid": inherited_from,
            "risk_inheritance_basis": entry.get("risk_inheritance_basis") or "",
            "risk_inheritance_preferred_name": entry.get("risk_inheritance_preferred_name")
            or "",
            "retrieved_at": entry.get("retrieved_at") or "",
        }
        bundle.epa_audit = audit

        payload = {
            **audit,
            "assay_rows": assay_rows[
                : int(epa_cfg.get("max_assay_rows_per_candidate", 20))
            ],
            "source_paths": list(entry.get("source_paths") or []),
        }
        scorable_identity = exact_identity or bool(inherited_from)
        if stage == 2 and scorable_identity and risk_tier == "strong_risk":
            max_score = float(epa_cfg.get("max_risk_score", 0.40))
            confidence = float(epa_cfg.get("risk_confidence", 0.50))
            score = max(0.0, min(1.0, max_score))
            risk_dtxsid = inherited_from or entry.get("dtxsid")
            if any(hit.evidence_id == f"epa_ctx:{risk_dtxsid}:cytotox_strong" for hit in bundle.tox):
                bundle.epa_audit["risk_applied"] = True
                return
            bundle.tox.append(
                EvidenceHit(
                    adapter_id="epa_ctx_tox_v1",
                    query_type="tox",
                    score=score,
                    confidence=max(0.0, min(1.0, confidence)),
                    evidence_id=f"epa_ctx:{risk_dtxsid}:cytotox_strong",
                    payload=payload,
                    endpoint="toxcast_cytotox_nhit",
                    direction="risk",
                    evidence_role="risk_signal",
                    provenance_status="retrieved",
                    source_url="https://comptox.epa.gov/ctx-api/bioactivity",
                    retrieved_at=str(entry.get("retrieved_at") or ""),
                    adapter_version="epa_ctx_tox_v2",
                    source_version="epa_ctx_tox_v2",
                    query_params={"dtxsid": risk_dtxsid},
                    query_status="exact_hit",
                    evidence_type="endpoint_evidence",
                )
            )
            bundle.epa_audit["risk_applied"] = True
            return

        # CAS / non-exact EPA identity stays in CSV/PDF audit via epa_audit.
        # Do not put identity_review_required on hits — that cancels eligibility.
        # PubChem multi-CID ambiguity still uses query_audit for true gating.
        bundle.annotation.append(
            EvidenceHit(
                adapter_id="epa_ctx_v1",
                query_type="annotation",
                score=0.0,
                confidence=0.0,
                evidence_id=(
                    f"epa_ctx:{entry.get('dtxsid')}:identity_audit"
                    if identity_uncertain
                    else f"epa_ctx:{entry.get('dtxsid')}:audit"
                ),
                payload=payload,
                endpoint="chemical_identity",
                direction=(
                    "risk"
                    if risk_tier in {"strong_risk", "weak_risk_review"}
                    else "unknown"
                ),
                evidence_role="annotation_only",
                provenance_status="retrieved",
                source_url="https://comptox.epa.gov/ctx-api/chemical",
                retrieved_at=str(entry.get("retrieved_at") or ""),
                adapter_version="epa_ctx_v1",
                source_version="epa_ctx_v1",
                query_params={"dtxsid": entry.get("dtxsid")},
                query_status="annotation_only",
                evidence_type="identity_annotation",
            )
        )

    def _merge_public_assay_grain(self, bundle: EvidenceBundle, *, inchikey: str) -> None:
        """Merge QC'd public assay-grain hits by exact InChIKey.

        PubChem Active rows remain annotation_only (no conf_e lift). Only ChEMBL
        phenotype task_evidence may enter lipid/tox scoring channels.
        """
        pag = self.cfg.evidence.get("public_assay_grain") or {}
        if not bool(pag.get("enabled", True)):
            return
        if self._public_assay_index is None or not inchikey:
            return
        from plugins.molmind_core.scientific.public_data.assay_index import hits_for_inchikey

        allow_chembl = bool(pag.get("allow_chembl_phenotype_scores", True))
        allow_pubchem = bool(pag.get("allow_pubchem_bioassay_scores", False))
        allow_bindingdb = bool(pag.get("allow_bindingdb_scores", False))
        for hit in hits_for_inchikey(self._public_assay_index, inchikey):
            # EPA is controlled by the staged CTX integration below.  Do not
            # let the generic QC index bypass stage 1 and silently add risk
            # points (or duplicate stage 2 hits).
            if hit.adapter_id == "public_epa_toxcast_tox21_v1":
                continue
            adapter_l = str(hit.adapter_id or "").lower()
            force_annotation = False
            if "pubchem" in adapter_l and not allow_pubchem:
                force_annotation = True
            if "bindingdb" in adapter_l and not allow_bindingdb:
                # BindingDB stays pathway/mechanism with score 0.
                hit.score = 0.0
                hit.confidence = 0.0
                if hit.query_type not in {"pathway", "annotation"}:
                    hit.query_type = "pathway"
                    hit.evidence_role = "mechanism_support"
            if "chembl" in adapter_l and not allow_chembl and hit.evidence_role == "task_evidence":
                force_annotation = True
            if force_annotation:
                hit.score = 0.0
                hit.confidence = 0.0
                hit.evidence_role = "annotation_only"
                hit.query_type = "annotation"
                hit.query_status = "annotation_only"
            if hit.query_type == "lipid":
                if not any(existing.evidence_id == hit.evidence_id for existing in bundle.lipid):
                    bundle.lipid.append(hit)
            elif hit.query_type == "tox":
                if not any(existing.evidence_id == hit.evidence_id for existing in bundle.tox):
                    bundle.tox.append(hit)
            elif hit.query_type == "pathway":
                if not any(existing.evidence_id == hit.evidence_id for existing in bundle.pathway):
                    bundle.pathway.append(hit)
            else:
                if not any(
                    existing.evidence_id == hit.evidence_id for existing in bundle.annotation
                ):
                    bundle.annotation.append(hit)

    def _merge_local_tables(
        self,
        bundle: EvidenceBundle,
        *,
        inchikey: str,
        smiles: str,
    ) -> None:
        lt = self.cfg.evidence.get("local_tables") or {}
        if not bool(lt.get("enabled", False)):
            return
        flags = self.cfg.evidence.get("adapter_flags") or {}
        from plugins.molmind_core.scientific.evidence_facade.local_tables import query_dilirank, query_nafldkb

        neighbor = bool(lt.get("neighbor", True))
        dili_thr = float(lt.get("dili_sim_threshold", 0.70)) if neighbor else 1.01
        nafld_thr = float(lt.get("nafld_sim_threshold", 0.78)) if neighbor else 1.01

        dili_flag = flags.get("dili_table_v1") or {}
        if (
            bool(dili_flag.get("enabled", False))
            and float(dili_flag.get("ranking_weight", 0)) > 0
            and self._dili_index
            and self._dili_index.size
        ):
            hit = query_dilirank(
                self._dili_index,
                inchikey=inchikey,
                smiles=smiles,
                sim_threshold=dili_thr,
            )
            if hit is not None:
                bundle.tox = [h for h in bundle.tox if h.adapter_id != "dili_table_v1"]
                bundle.tox.append(hit)

        nafld_flag = flags.get("nafldkb_v1") or {}
        if (
            bool(nafld_flag.get("enabled", False))
            and float(nafld_flag.get("ranking_weight", 0)) > 0
            and self._nafld_index
            and self._nafld_index.size
        ):
            hit = query_nafldkb(
                self._nafld_index,
                inchikey=inchikey,
                smiles=smiles,
                sim_threshold=nafld_thr,
            )
            if hit is not None:
                hit.confidence = min(hit.confidence, 0.15)
                bundle.lipid = [h for h in bundle.lipid if h.adapter_id != "nafldkb_v1"]
                bundle.lipid.append(hit)

    def _append_snapshot_miss(self, *, inchikey: str, cas: str | None) -> None:
        row = {
            "inchikey": inchikey,
            "cas": cas or "",
            "adapter_id": "bake_miss_v1",
            "query_type": "query_audit",
            "score": 0.0,
            "confidence": 0.0,
            "evidence_id": f"bake_miss:{inchikey}",
            "payload": {"note": "live queried, no hit"},
            "endpoint": "query_status",
            "direction": "unknown",
            "evidence_role": "query_audit",
            "provenance_status": "no_relevant_record",
            "query_status": "verified_empty",
            "source_url": "",
            "retrieved_at": _utc_now(),
            "adapter_version": "bake_miss_v2",
            "query_params": {},
            "response_sha256": "",
            "license": "",
            "schema_version": EVIDENCE_SCHEMA_VERSION,
        }
        path = self.snapshot_dir / "auto_cache.jsonl"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._index.setdefault(inchikey, []).append(row)
        if cas:
            self._index.setdefault(cas, []).append(row)

    def _append_snapshot_hits(
        self,
        hits: list[EvidenceHit],
        *,
        inchikey: str,
        cas: str | None,
    ) -> None:
        if not inchikey or not hits:
            return
        path = self.snapshot_dir / "auto_cache.jsonl"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for hit in hits:
            row = {
                "inchikey": inchikey,
                "cas": cas or "",
                "adapter_id": hit.adapter_id,
                "query_type": hit.query_type,
                "score": hit.score,
                "confidence": hit.confidence,
                "evidence_id": hit.evidence_id,
                "payload": hit.payload,
                "endpoint": hit.endpoint,
                "direction": hit.direction,
                "evidence_role": hit.evidence_role,
                "provenance_status": hit.provenance_status,
                "source_url": hit.source_url,
                "retrieved_at": hit.retrieved_at,
                "adapter_version": hit.adapter_version,
                "source_version": hit.source_version or hit.adapter_version,
                "query_params": hit.query_params,
                "response_sha256": hit.response_sha256,
                "license": hit.license,
                "query_status": hit.query_status,
                "evidence_type": hit.evidence_type or infer_evidence_type(hit),
                "schema_version": EVIDENCE_SCHEMA_VERSION,
            }
            rows.append(row)
            self._index.setdefault(inchikey, []).append(row)
            if cas:
                self._index.setdefault(cas, []).append(row)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def reload_snapshot_index(self) -> None:
        self._index = self._load_snapshot_index()
        self._cache.clear()

    def finalize_degraded_flags(self, *, any_hit: bool) -> None:
        if self.cfg.allow_live_evidence:
            if self._live_successes == 0 and self._live_failures > 0:
                self.cfg.mark_degraded("evidence_live")
            if self._circuit_open:
                self.cfg.mark_degraded("evidence_live_circuit_open")
        if not any_hit:
            # 快照目录非空不等于本轮候选命中。旧逻辑会在存在任何无关快照时
            # 掩盖候选级零证据，造成 conf_e 全 0 的榜单仍没有 evidence_empty。
            self.cfg.mark_degraded("evidence_empty")
