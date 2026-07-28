"""本地参考表：FDA DILIrank / NAFLDkb（精确 InChIKey + ECFP 近邻）。"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import csv
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem

from packages.chem_core import clamp, morgan_fp, tanimoto
from packages.models import EvidenceHit

ROOT = REPO_ROOT
DATA_DIR = ROOT / "data"

REF_DIR = DATA_DIR / "reference"


@dataclass
class RefEntry:
    name: str
    smiles: str
    inchikey: str
    score: float
    source: str
    concern: str = ""
    fp_bits: object | None = None


class LocalTableIndex:
    def __init__(self, entries: list[RefEntry]):
        self.entries = entries
        self.by_inchikey = {e.inchikey: e for e in entries if e.inchikey}

    @property
    def size(self) -> int:
        return len(self.entries)


def _load_csv(path: Path) -> list[RefEntry]:
    if not path.is_file():
        return []
    entries: list[RefEntry] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            smiles = (row.get("smiles") or "").strip()
            if not smiles:
                continue
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            inchikey = (row.get("inchikey") or "").strip() or Chem.MolToInchiKey(mol)
            try:
                score = float(row.get("score") or 0.0)
            except ValueError:
                continue
            entries.append(
                RefEntry(
                    name=(row.get("name") or inchikey).strip(),
                    smiles=Chem.MolToSmiles(mol),
                    inchikey=inchikey,
                    score=clamp(score),
                    source=(row.get("source") or path.stem).strip(),
                    concern=(row.get("concern") or "").strip(),
                    fp_bits=morgan_fp(mol),
                )
            )
    return entries


def load_dilirank(path: Path | None = None) -> LocalTableIndex:
    return LocalTableIndex(_load_csv(path or REF_DIR / "dilirank.csv"))


def load_nafldkb(path: Path | None = None) -> LocalTableIndex:
    return LocalTableIndex(_load_csv(path or REF_DIR / "nafldkb.csv"))


def _best_match(
    index: LocalTableIndex,
    *,
    inchikey: str,
    smiles: str,
    sim_threshold: float,
) -> tuple[RefEntry | None, float, str]:
    if inchikey and inchikey in index.by_inchikey:
        return index.by_inchikey[inchikey], 1.0, "exact"
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    if mol is None or not index.entries:
        return None, 0.0, ""
    query_fp = morgan_fp(mol)
    best: RefEntry | None = None
    best_sim = 0.0
    for entry in index.entries:
        if entry.fp_bits is None:
            continue
        sim = tanimoto(query_fp, entry.fp_bits)
        if sim > best_sim:
            best_sim = sim
            best = entry
    if best is None or best_sim < sim_threshold:
        return None, best_sim, ""
    return best, best_sim, "neighbor"


def query_dilirank(
    index: LocalTableIndex,
    *,
    inchikey: str,
    smiles: str,
    sim_threshold: float = 0.70,
) -> EvidenceHit | None:
    entry, sim, how = _best_match(
        index, inchikey=inchikey, smiles=smiles, sim_threshold=sim_threshold
    )
    if entry is None:
        return None
    score = entry.score if how == "exact" else clamp(entry.score * (0.55 + 0.45 * sim))
    conf = 0.90 if how == "exact" else clamp(0.45 + 0.45 * sim)
    return EvidenceHit(
        adapter_id="dili_table_v1",
        query_type="tox",
        score=score,
        confidence=conf,
        evidence_id=f"dilirank:{how}:{entry.name}",
        payload={
            "name": entry.name,
            "concern": entry.concern,
            "match": how,
            "similarity": round(sim, 4),
            "source": entry.source,
        },
        endpoint="local_dilirank_table",
        direction="risk",
        evidence_role="task_evidence",
        provenance_status="frozen_local_table",
        adapter_version="dili_table_v1",
        source_version="dili_table_v1",
        query_status="exact_hit" if how == "exact" else "analogue_hit",
        evidence_type="endpoint_evidence",
        lookup_field="inchikey" if how == "exact" else "standardized_smiles",
        lookup_value=inchikey if how == "exact" else smiles,
        match_type="exact_identity" if how == "exact" else "structure_analogue",
        claim_ceiling="local_table_risk_evidence",
    )


def query_nafldkb(
    index: LocalTableIndex,
    *,
    inchikey: str,
    smiles: str,
    sim_threshold: float = 0.75,
) -> EvidenceHit | None:
    entry, sim, how = _best_match(
        index, inchikey=inchikey, smiles=smiles, sim_threshold=sim_threshold
    )
    if entry is None:
        return None
    score = entry.score if how == "exact" else clamp(entry.score * (0.50 + 0.50 * sim))
    conf = 0.85 if how == "exact" else clamp(0.40 + 0.45 * sim)
    return EvidenceHit(
        adapter_id="nafldkb_v1",
        query_type="lipid",
        score=score,
        confidence=conf,
        evidence_id=f"nafldkb:{how}:{entry.name}",
        payload={
            "name": entry.name,
            "match": how,
            "similarity": round(sim, 4),
            "source": entry.source,
        },
        endpoint="local_nafldkb_table",
        direction="supports",
        evidence_role="task_evidence",
        provenance_status="frozen_local_table",
        adapter_version="nafldkb_v1",
        source_version="nafldkb_v1",
        query_status="exact_hit" if how == "exact" else "analogue_hit",
        evidence_type="endpoint_evidence",
        lookup_field="inchikey" if how == "exact" else "standardized_smiles",
        lookup_value=inchikey if how == "exact" else smiles,
        match_type="exact_identity" if how == "exact" else "structure_analogue",
        claim_ceiling="local_preclinical_or_curated_efficacy_evidence",
    )


__all__ = [
    "LocalTableIndex",
    "load_dilirank",
    "load_nafldkb",
    "query_dilirank",
    "query_nafldkb",
    "REF_DIR",
]
