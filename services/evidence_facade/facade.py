"""Evidence Facade：Quality-Max 下 snapshot 优先 → Top-M live 补洞 → 空结果（不伪造）。"""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from packages.models import EvidenceHit
from services.evidence_facade.bundle import EvidenceBundle, infer_evidence_type
from services.pipeline.config_loader import SNAPSHOT_DIR, AppConfig

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


def _load_frozen_risk_aliases() -> dict[str, str]:
    """原始结构键 → 标准化结构键；仅用于保守传播毒性风险。"""
    path = (
        Path(__file__).resolve().parents[2]
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
    if not normalized.get("query_status"):
        role = str(normalized.get("evidence_role") or "")
        provenance = str(normalized.get("provenance_status") or "")
        if role == "annotation_only":
            normalized["query_status"] = "annotation_only"
        elif role == "query_audit" and provenance == "no_relevant_record":
            normalized["query_status"] = "verified_empty"
        elif role == "query_audit" and provenance == "query_failed":
            normalized["query_status"] = "adapter_error"
        elif provenance == "retrieved":
            normalized["query_status"] = "exact_hit"
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
    elif (
        adapter == "chembl_lipid_v1"
        and normalized.get("query_type") == "novelty"
        and int(payload.get("lipid_hits") or 0) == 0
    ):
        normalized["query_type"] = "annotation"
        normalized["evidence_role"] = "annotation_only"
        normalized["score"] = 0.0
        normalized["confidence"] = 0.0
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
        lt = cfg.evidence.get("local_tables") or {}
        # 主路径默认关闭；显式 enabled=true 时才加载（阶段 7 可选）
        if bool(lt.get("enabled", False)):
            from services.evidence_facade.local_tables import load_dilirank, load_nafldkb

            root = Path(__file__).resolve().parents[2]
            dili_path = root / str(lt.get("dili_csv", "data/reference/dilirank.csv"))
            nafld_path = root / str(lt.get("nafld_csv", "data/reference/nafldkb.csv"))
            self._dili_index = load_dilirank(dili_path)
            self._nafld_index = load_nafldkb(nafld_path)

        pag = cfg.evidence.get("public_assay_grain") or {}
        if bool(pag.get("enabled", True)):
            from services.public_data.assay_index import load_public_assay_index

            root = Path(__file__).resolve().parents[2]
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

    def _from_snapshot(self, key: str, adapters: set[str], query_type: str) -> list[EvidenceHit]:
        # 同 adapter+query_type 保留最后一条，使 auto_cache 追加可覆盖旧快照
        latest: dict[str, dict[str, Any]] = {}
        for row in self._index.get(key, []):
            adapter = row.get("adapter_id", "")
            if adapters and adapter not in adapters:
                continue
            qt = row.get("query_type") or query_type
            if qt != query_type:
                continue
            latest[str(adapter)] = row

        hits: list[EvidenceHit] = []
        for adapter, row in latest.items():
            adapter_version = str(row.get("adapter_version") or adapter)
            source_version = str(row.get("source_version") or adapter_version)
            hit = EvidenceHit(
                adapter_id=adapter,
                query_type=query_type,
                score=float(row.get("score", 0.0)),
                confidence=float(row.get("confidence", 0.5)),
                evidence_id=row.get("evidence_id") or f"snap:{adapter}:{key}",
                payload=row.get("payload") or {},
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
                evidence_type=str(row.get("evidence_type") or "unresolved"),  # type: ignore[arg-type]
            )
            hits.append(_finalize_hit(hit))
        return hits

    def _record_live_failure(self) -> None:
        self._live_failures += 1
        if self._live_failures >= self._fail_threshold:
            self._circuit_open = True
            self.cfg.mark_degraded("evidence_live_circuit_open")

    def _record_live_success(self) -> None:
        self._live_successes += 1

    def _chembl_lipid(self, client: httpx.Client, inchikey: str) -> list[EvidenceHit]:
        if not inchikey:
            return []
        url = f"https://www.ebi.ac.uk/chembl/api/data/molecule/{inchikey}.json"
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

        act_url = "https://www.ebi.ac.uk/chembl/api/data/activity.json"
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

    def _pubchem_tox(self, client: httpx.Client, inchikey: str) -> list[EvidenceHit]:
        if not inchikey:
            return []
        cid_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/{inchikey}/cids/JSON"
        cid_resp = client.get(cid_url)
        if cid_resp.status_code == 404:
            return [
                _query_audit_hit(
                    adapter_id="pubchem_tox_v1",
                    query_status="verified_empty",
                    evidence_id=f"pubchem:not_found:{inchikey}",
                    payload={"reason": "inchikey_not_found"},
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
                    evidence_id=f"pubchem:no_cid:{inchikey}",
                    payload={"reason": "response_missing_cid"},
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
                    evidence_id=f"pubchem:identity_review:{inchikey}",
                    payload={"cids": unique_cids, "count": len(unique_cids)},
                    endpoint="identity_resolution",
                    direction="unknown",
                    evidence_role="query_audit",
                    provenance_status="retrieved",
                    source_url=cid_url,
                    retrieved_at=_utc_now(),
                    adapter_version="pubchem_tox_v3",
                    query_params={"inchikey": inchikey},
                    response_sha256=_response_sha256(cid_resp.content),
                    license="PubChem public data; source-specific rights may apply",
                    query_status="identity_review_required",
                )
            ]
        cid = unique_cids[0]

        view_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"
        view_resp = client.get(view_url, params={"heading": "GHS Classification"})
        payload: dict[str, Any] | None = None
        response_hash = ""
        if view_resp.status_code == 200:
            payload = view_resp.json()
            response_hash = _response_sha256(view_resp.content)
        else:
            alt = client.get(view_url, params={"heading": "Toxicity"})
            if alt.status_code == 200:
                payload = alt.json()
                response_hash = _response_sha256(alt.content)

        if not payload:
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
        if "hepatotox" in joined or ("liver" in joined and "tox" in joined):
            hazard_score += 0.35
            flags.append("liver")
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
                    payload={"cid": cid, "flags": [], "nodes_examined": len(structured_strings)},
                    endpoint="database_annotation",
                    direction="unknown",
                    evidence_role="annotation_only",
                    provenance_status="retrieved",
                    source_url=view_url,
                    retrieved_at=_utc_now(),
                    adapter_version="pubchem_tox_v2",
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
                adapter_version="pubchem_tox_v2",
                query_params={"heading": "GHS Classification|Toxicity"},
                response_sha256=response_hash,
                license="PubChem public data; source-specific rights may apply",
                query_status="exact_hit",
            )
        ]

    def _try_live(self, *, inchikey: str, cas: str | None, smiles: str) -> list[EvidenceHit]:
        _ = (cas, smiles)
        if self.cfg.mode == "offline":
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
        if not self.cfg.allow_live_evidence:
            return []
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
        allow_live: bool = True,
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
        cache_key = (
            f"{inchikey}|{cas}|{int(allow_live)}|{self.cfg.mode}|"
            f"{int(prefer_snapshot)}|{int(use_snapshot)}"
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        adapters = self._effective_adapters()
        lipid_adapters = adapters & {"chembl_lipid_v1", "ot_target_v1", "nafldkb_v1"}
        tox_adapters = adapters & {"chembl_lipid_v1", "pubchem_tox_v1", "dili_table_v1"}

        bundle = EvidenceBundle(
            normalized_inchikey=inchikey,
            input_structure_hash=_input_structure_hash(
                inchikey=inchikey, smiles=smiles, cas=cas
            ),
            queried_at=_utc_now(),
        )
        keys = [k for k in (inchikey, cas) if k]
        already_indexed = bool(
            (inchikey and inchikey in self._index) or (cas and cas in self._index)
        )

        def fill_from_snapshot() -> None:
            for key in keys:
                if not bundle.lipid:
                    bundle.lipid.extend(self._from_snapshot(key, lipid_adapters, "lipid"))
                if not bundle.tox:
                    bundle.tox.extend(self._from_snapshot(key, tox_adapters, "tox"))
                if not bundle.novelty:
                    bundle.novelty.extend(self._from_snapshot(key, adapters, "novelty"))
                if not bundle.pathway:
                    bundle.pathway.extend(
                        self._from_snapshot(
                            key,
                            {"chembl_lipid_v1", "kegg_pathway_v1"},
                            "pathway",
                        )
                    )
                if not bundle.annotation:
                    bundle.annotation.extend(self._from_snapshot(key, adapters, "annotation"))
                if not bundle.query_audit:
                    bundle.query_audit.extend(self._from_snapshot(key, adapters | {"bake_miss_v1"}, "query_audit"))

        if use_snapshot and prefer_snapshot:
            fill_from_snapshot()

        need_live = (
            allow_live
            and self.cfg.allow_live_evidence
            and not self._circuit_open
            and not (use_snapshot and prefer_snapshot and already_indexed)
        )

        if need_live:
            failures_before = self._live_failures
            live = self._try_live(inchikey=inchikey, cas=cas, smiles=smiles)
            live_failed = self._live_failures > failures_before
            for hit in live:
                if hit.query_type == "lipid" and not bundle.lipid:
                    bundle.lipid.append(hit)
                elif hit.query_type == "tox" and not bundle.tox:
                    bundle.tox.append(hit)
                elif hit.query_type == "novelty" and not bundle.novelty:
                    bundle.novelty.append(hit)
                elif hit.query_type == "annotation" and not bundle.annotation:
                    bundle.annotation.append(hit)
                elif hit.query_type == "query_audit":
                    identity = (hit.adapter_id, hit.query_status, hit.evidence_id)
                    if not any(
                        (existing.adapter_id, existing.query_status, existing.evidence_id)
                        == identity
                        for existing in bundle.query_audit
                    ):
                        bundle.query_audit.append(hit)
            if live and bool(self.cfg.evidence.get("auto_cache_snapshot", True)):
                self._append_snapshot_hits(live, inchikey=inchikey, cas=cas)
            elif (
                not live
                and not live_failed
                and bool(self.cfg.evidence.get("auto_cache_snapshot", True))
                and inchikey
            ):
                self._append_snapshot_miss(inchikey=inchikey, cas=cas)

        if use_snapshot and not prefer_snapshot:
            fill_from_snapshot()

        self._merge_local_tables(bundle, inchikey=inchikey, smiles=smiles)
        self._merge_public_assay_grain(bundle, inchikey=inchikey)

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
        from services.public_data.assay_index import hits_for_inchikey

        allow_score = bool(pag.get("allow_chembl_phenotype_scores", True))
        for hit in hits_for_inchikey(self._public_assay_index, inchikey):
            if not allow_score and hit.evidence_role == "task_evidence":
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
        from services.evidence_facade.local_tables import query_dilirank, query_nafldkb

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
