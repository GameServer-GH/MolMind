"""bake-evidence：写出 JSONL schema。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from packages.models import EvidenceHit
from services.evidence_facade.bake import bake_evidence_for_records, _hit_to_row
from services.pipeline.config_loader import load_config


def test_hit_to_row_has_required_fields() -> None:
    hit = EvidenceHit(
        adapter_id="chembl_lipid_v1",
        query_type="lipid",
        score=0.7,
        confidence=0.6,
        evidence_id="chembl:X:lipid",
        payload={"chembl_id": "CHEMBL1"},
    )
    row = _hit_to_row(hit, inchikey="KEY-N", cas="1-2-3")
    required = {
        "inchikey",
        "cas",
        "adapter_id",
        "query_type",
        "score",
        "confidence",
        "evidence_id",
        "payload",
        "baked_at",
    }
    assert required <= set(row.keys())


def test_bake_writes_jsonl_schema(tmp_path: Path) -> None:
    from packages.chem_core import compute_descriptors, morgan_fp
    from packages.models import MoleculeRecord
    from rdkit import Chem

    smiles = "CCO"
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    desc = compute_descriptors(smiles)
    assert desc is not None
    record = MoleculeRecord(
        molecule_id="BAKE1",
        smiles=smiles,
        inchikey=Chem.MolToInchiKey(mol) or "BAKE-KEY-N",
        cas=None,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )

    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"

    fake_hits = [
        EvidenceHit(
            adapter_id="chembl_lipid_v1",
            query_type="lipid",
            score=0.5,
            confidence=0.5,
            evidence_id="chembl:BAKE:lipid",
        )
    ]

    with patch(
        "services.evidence_facade.bake.EvidenceFacade._try_live",
        return_value=fake_hits,
    ):
        stats = bake_evidence_for_records([record], cfg, output_path=out, skip_cached=False)

    assert stats.wrote_rows >= 1
    assert out.is_file()
    line = out.read_text(encoding="utf-8").strip().splitlines()[0]
    row = json.loads(line)
    for key in ("inchikey", "adapter_id", "query_type", "score", "confidence", "evidence_id"):
        assert key in row
