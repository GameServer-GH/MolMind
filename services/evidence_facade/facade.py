"""Evidence Facade：Quality-Max 下 snapshot 优先 → Top-M live 补洞 → 空结果（不伪造）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from packages.models import EvidenceHit
from services.evidence_facade.bundle import EvidenceBundle
from services.pipeline.config_loader import SNAPSHOT_DIR, AppConfig

LIPID_TARGET_RE = re.compile(
    r"HMGCR|HMG.?CoA|PPAR[A-Z]?|SREBF|SREBP|ACAC[AB]?|FASN|SCD|CPT1|"
    r"AMPK|PRKAA|LDLR|NPC1L1|DGAT|LPL|ABCA1|CYP7A1|NAFLD|MASLD|triglyceride|"
    r"cholesterol|lipid",
    re.I,
)


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
        lt = cfg.evidence.get("local_tables") or {}
        # 定榜默认关闭；显式 enabled=true 时才加载（阶段 7 可选）
        if bool(lt.get("enabled", False)):
            from services.evidence_facade.local_tables import load_dilirank, load_nafldkb

            root = Path(__file__).resolve().parents[2]
            dili_path = root / str(lt.get("dili_csv", "data/reference/dilirank.csv"))
            nafld_path = root / str(lt.get("nafld_csv", "data/reference/nafldkb.csv"))
            self._dili_index = load_dilirank(dili_path)
            self._nafld_index = load_nafldkb(nafld_path)

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
                    key = row.get("inchikey") or row.get("cas") or ""
                    if not key:
                        continue
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
            hits.append(
                EvidenceHit(
                    adapter_id=adapter,
                    query_type=query_type,
                    score=float(row.get("score", 0.0)),
                    confidence=float(row.get("confidence", 0.5)),
                    evidence_id=row.get("evidence_id") or f"snap:{adapter}:{key}",
                    payload=row.get("payload") or {},
                )
            )
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
            return []
        resp.raise_for_status()
        mol = resp.json()
        chembl_id = mol.get("molecule_chembl_id")
        if not chembl_id:
            return []

        act_url = "https://www.ebi.ac.uk/chembl/api/data/activity.json"
        act_resp = client.get(
            act_url,
            params={
                "molecule_chembl_id": chembl_id,
                "limit": 25,
                "offset": 0,
            },
        )
        act_resp.raise_for_status()
        activities = act_resp.json().get("activities") or []
        lipid_hits = 0
        target_names: list[str] = []
        for act in activities:
            blob = " ".join(
                str(act.get(k) or "")
                for k in ("target_pref_name", "assay_description", "standard_type", "bao_label")
            )
            if LIPID_TARGET_RE.search(blob):
                lipid_hits += 1
                name = act.get("target_pref_name") or act.get("assay_description") or ""
                if name and name not in target_names:
                    target_names.append(str(name)[:80])

        if lipid_hits == 0:
            return [
                EvidenceHit(
                    adapter_id="chembl_lipid_v1",
                    query_type="novelty",
                    score=0.55,
                    confidence=0.4,
                    evidence_id=f"chembl:{chembl_id}:present",
                    payload={"chembl_id": chembl_id, "lipid_hits": 0},
                )
            ]

        score = min(0.80, 0.30 + 0.10 * lipid_hits)
        return [
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="lipid",
                score=score,
                confidence=min(0.9, 0.45 + 0.08 * lipid_hits),
                evidence_id=f"chembl:{chembl_id}:lipid",
                payload={
                    "chembl_id": chembl_id,
                    "lipid_hits": lipid_hits,
                    "targets": target_names[:5],
                },
            ),
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="novelty",
                score=max(0.15, 0.7 - 0.05 * lipid_hits),
                confidence=0.5,
                evidence_id=f"chembl:{chembl_id}:novelty",
                payload={"chembl_id": chembl_id},
            ),
        ]

    def _pubchem_tox(self, client: httpx.Client, inchikey: str) -> list[EvidenceHit]:
        if not inchikey:
            return []
        cid_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/{inchikey}/cids/JSON"
        cid_resp = client.get(cid_url)
        if cid_resp.status_code == 404:
            return []
        cid_resp.raise_for_status()
        cids = (cid_resp.json().get("IdentifierList") or {}).get("CID") or []
        if not cids:
            return []
        cid = int(cids[0])

        view_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"
        view_resp = client.get(view_url, params={"heading": "GHS Classification"})
        text = ""
        if view_resp.status_code == 200:
            text = view_resp.text.lower()
        else:
            alt = client.get(view_url, params={"heading": "Toxicity"})
            if alt.status_code == 200:
                text = alt.text.lower()

        if not text:
            return []

        hazard_score = 0.0
        flags: list[str] = []
        if "hepatotox" in text or "liver" in text and "tox" in text:
            hazard_score += 0.35
            flags.append("liver")
        if "danger" in text:
            hazard_score += 0.2
            flags.append("ghs_danger")
        if "warning" in text:
            hazard_score += 0.1
            flags.append("ghs_warning")
        if "carcinogen" in text:
            hazard_score += 0.25
            flags.append("carcinogen")
        if "acute toxicity" in text:
            hazard_score += 0.15
            flags.append("acute_toxicity")

        if hazard_score <= 0:
            return [
                EvidenceHit(
                    adapter_id="pubchem_tox_v1",
                    query_type="tox",
                    score=0.05,
                    confidence=0.3,
                    evidence_id=f"pubchem:{cid}:ghs_low",
                    payload={"cid": cid, "flags": flags},
                )
            ]

        return [
            EvidenceHit(
                adapter_id="pubchem_tox_v1",
                query_type="tox",
                score=min(1.0, hazard_score),
                confidence=0.55,
                evidence_id=f"pubchem:{cid}:ghs",
                payload={"cid": cid, "flags": flags},
            )
        ]

    def _try_live(self, *, inchikey: str, cas: str | None, smiles: str) -> list[EvidenceHit]:
        _ = (cas, smiles)
        if self.cfg.mode == "offline" or self._circuit_open:
            return []
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
        except Exception:
            self._record_live_failure()
            self.cfg.mark_degraded("evidence_live")
            return []

    def query(
        self,
        *,
        inchikey: str,
        cas: str | None,
        smiles: str,
        allow_live: bool = True,
    ) -> EvidenceBundle:
        if not self.enabled:
            return EvidenceBundle()

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
        tox_adapters = adapters & {"pubchem_tox_v1", "dili_table_v1"}

        bundle = EvidenceBundle()
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
                        self._from_snapshot(key, {"kegg_pathway_v1"}, "pathway")
                    )

        if use_snapshot and prefer_snapshot:
            fill_from_snapshot()

        need_live = (
            allow_live
            and self.cfg.allow_live_evidence
            and not self._circuit_open
            and not (use_snapshot and prefer_snapshot and already_indexed)
        )

        if need_live:
            live = self._try_live(inchikey=inchikey, cas=cas, smiles=smiles)
            for hit in live:
                if hit.query_type == "lipid" and not bundle.lipid:
                    bundle.lipid.append(hit)
                elif hit.query_type == "tox" and not bundle.tox:
                    bundle.tox.append(hit)
                elif hit.query_type == "novelty" and not bundle.novelty:
                    bundle.novelty.append(hit)
            if live and bool(self.cfg.evidence.get("auto_cache_snapshot", True)):
                self._append_snapshot_hits(live, inchikey=inchikey, cas=cas)
            elif not live and bool(self.cfg.evidence.get("auto_cache_snapshot", True)) and inchikey:
                self._append_snapshot_miss(inchikey=inchikey, cas=cas)

        if use_snapshot and not prefer_snapshot:
            fill_from_snapshot()

        self._merge_local_tables(bundle, inchikey=inchikey, smiles=smiles)

        self._cache[cache_key] = bundle
        return bundle

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
            "query_type": "novelty",
            "score": 0.5,
            "confidence": 0.05,
            "evidence_id": f"bake_miss:{inchikey}",
            "payload": {"note": "live queried, no hit"},
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
            if self._index:
                return
            self.cfg.mark_degraded("evidence_empty")
