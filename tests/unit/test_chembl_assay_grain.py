"""Wave-1 ChEMBL assay-grain importer tests (mocked network)."""

from __future__ import annotations

import json
from pathlib import Path

from services.public_data.chembl import (
    ASSAY_GRAIN_FIELDS,
    SEED_ADIPO_ASSAY_IDS,
    SEED_HEPG2_FFA_ASSAY_IDS,
    SEED_HEPG2_LIPID_ASSAY_IDS,
    SEED_POSITIVE_ASSAY_IDS,
    _assay_search_priority,
    _looks_hepg2_ffa,
    import_chembl_assay_grain,
    import_chembl_by_inchikeys,
    normalize_chembl_activity_row,
)


def test_hepg2_ffa_seeds_are_curated_and_prioritized() -> None:
    assert len(SEED_HEPG2_FFA_ASSAY_IDS) >= 15
    assert len(SEED_POSITIVE_ASSAY_IDS) == (
        len(SEED_HEPG2_FFA_ASSAY_IDS)
        + len(SEED_HEPG2_LIPID_ASSAY_IDS)
        + len(SEED_ADIPO_ASSAY_IDS)
    )
    assert SEED_POSITIVE_ASSAY_IDS[: len(SEED_HEPG2_FFA_ASSAY_IDS)] == SEED_HEPG2_FFA_ASSAY_IDS
    assert _looks_hepg2_ffa(
        {
            "assay_description": (
                "Lipid-lowering effect in human HepG2 cells assessed as reduction "
                "in oleic acid-induced triglyceride accumulation"
            )
        }
    )
    assert not _looks_hepg2_ffa(
        {
            "assay_description": (
                "Inhibition of adipocytes differentiation in mouse 3T3L1 cells "
                "assessed as lipid accumulation by Oil red O staining"
            )
        }
    )
    hepg2_ffa = {
        "assay_chembl_id": SEED_HEPG2_FFA_ASSAY_IDS[0],
        "description": "HepG2 oleic acid-induced lipid accumulation reduction",
    }
    adipo = {
        "assay_chembl_id": SEED_ADIPO_ASSAY_IDS[0],
        "description": "3T3-L1 lipid accumulation reduction",
    }
    assert _assay_search_priority(hepg2_ffa) < _assay_search_priority(adipo)


SOURCE = {
    "source_id": "chembl_bioactivity",
    "api_base": "https://www.ebi.ac.uk/chembl/api/data",
    "license_policy": "verify_source_version_and_cc_by_sa_3",
}


def test_normalize_chembl_activity_row_fills_assay_grain_contract() -> None:
    activity = {
        "activity_id": 111,
        "molecule_chembl_id": "CHEMBL25",
        "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
        "assay_chembl_id": "CHEMBL1234567",
        "standard_type": "IC50",
        "standard_value": 12.5,
        "standard_units": "nM",
        "record_id": 99,
        "target_pref_name": "HepG2",
        "assay_description": "Reduction of lipid accumulation in HepG2 cells after 24 h",
        "bao_label": "cell-based format",
        "activity_comment": "decreased triglyceride",
    }
    row = normalize_chembl_activity_row(
        activity,
        source_id="chembl_bioactivity",
        license_policy="cc-by-sa",
        api_base=SOURCE["api_base"],
        retrieved_at="2026-07-15T00:00:00+00:00",
    )
    for field in ASSAY_GRAIN_FIELDS:
        assert field in row
    assert row["compound_id"] == "CHEMBL25"
    assert row["assay_id"] == "CHEMBL1234567"
    assert row["standardized_smiles"].startswith("CC(=O)O")
    assert row["value"] == 12.5
    assert row["unit"] == "nM"
    assert row["treatment_time_hours"] == 24.0
    assert row["direction"] == "supports"
    assert row["classification"] == "positive_phenotype"


def test_import_chembl_assay_grain_writes_records_and_raw(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, params: dict | None, timeout: int):
        calls.append(url)
        params = params or {}
        if url.endswith("/status.json"):
            return {"chembl_db_version": "35", "status": "UP"}
        if "/assay.json" in url:
            # Only first page returns hits; pagination must terminate.
            if int(params.get("offset") or 0) > 0:
                return {"assays": [], "page_meta": {"total_count": 2}}
            return {
                "assays": [
                    {
                        "assay_chembl_id": "CHEMBL998",
                        "description": "Induction of phospholipidosis in HepG2 cells",
                    },
                    {
                        "assay_chembl_id": "CHEMBL999",
                        "description": (
                            "Reduction in lipid accumulation in human HepG2 cells "
                            "by Oil Red O staining"
                        ),
                    },
                ],
                "page_meta": {"total_count": 2},
            }
        if "/activity.json" in url:
            return {
                "activities": [
                    {
                        "activity_id": 1,
                        "molecule_chembl_id": "CHEMBL1",
                        "canonical_smiles": "CCO",
                        "assay_chembl_id": "CHEMBL999",
                        "standard_type": "Activity",
                        "standard_value": 1.0,
                        "standard_units": "%",
                        "record_id": 7,
                        "target_pref_name": "HepG2",
                        "assay_description": "lipid droplet reduction in HepG2 cells",
                        "activity_comment": "decreased lipid accumulation",
                    },
                    {
                        "activity_id": 2,
                        "molecule_chembl_id": "CHEMBL2",
                        "canonical_smiles": "CCC",
                        "assay_chembl_id": "CHEMBL999",
                        "standard_type": "IC50",
                        "standard_value": 50,
                        "standard_units": "uM",
                        "record_id": 8,
                        "target_pref_name": "PPARA",
                        "assay_description": "binding assay",
                    },
                ],
                "page_meta": {"total_count": 2},
            }
        raise AssertionError(f"unexpected url {url}")

    result = import_chembl_assay_grain(
        SOURCE,
        limit=5,
        scan_per_term=25,
        seed_assay_ids=(),
        get_json=fake_get,
        raw_dir=tmp_path / "raw",
    )
    # Positive HepG2 assay is preferred over phospholipidosis-only hits.
    assert result["assay_count"] >= 1
    assert result["positive_cellular_assay_count"] >= 1
    assert result["activity_count"] >= 2
    assert result["chembl_release"] == "35"
    assert result["grain"] == "compound_x_assay_x_activity"
    assert result["classification_counts"].get("positive_phenotype", 0) >= 1
    assert all(field in result["records"][0] for field in ASSAY_GRAIN_FIELDS)
    assert Path(result["raw_path"]).is_file()
    raw = json.loads(Path(result["raw_path"]).read_text(encoding="utf-8"))
    assert raw["assay_count"] >= 1
    assert any("status.json" in url for url in calls)
    assert any("assay.json" in url for url in calls)
    assert any("activity.json" in url for url in calls)


def test_explicit_empty_query_terms_keep_seed_only_mode(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, params: dict | None, timeout: int):
        calls.append(url)
        if url.endswith("/status.json"):
            return {"chembl_db_version": "37", "status": "UP"}
        if "/assay.json" in url:
            raise AssertionError("free-text assay search must stay disabled")
        if "/activity.json" in url:
            return {
                "activities": [
                    {
                        "activity_id": 101,
                        "molecule_chembl_id": "CHEMBL101",
                        "canonical_smiles": "CCO",
                        "assay_chembl_id": "CHEMBL2156870",
                        "standard_type": "Imax",
                        "standard_value": 54.2,
                        "standard_units": "%",
                        "assay_description": (
                            "Inhibition of oleic acid-induced triglyceride "
                            "over-accumulation in human HepG2 cells"
                        ),
                    }
                ],
                "page_meta": {"total_count": 1},
            }
        raise AssertionError(f"unexpected url {url}")

    result = import_chembl_assay_grain(
        SOURCE,
        limit=1,
        query_terms=(),
        seed_assay_ids=("CHEMBL2156870",),
        get_json=fake_get,
        raw_dir=tmp_path / "raw",
    )

    assert result["query_terms"] == []
    assert result["activity_count"] == 1
    assert not any("/assay.json" in url for url in calls)


def test_script_run_one_chembl_with_mock(monkeypatch, tmp_path: Path) -> None:
    from scripts import import_public_data as importer

    monkeypatch.setattr(importer, "RAW", tmp_path / "raw")
    monkeypatch.setattr(importer, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(importer, "MANIFESTS", tmp_path / "manifests")
    monkeypatch.setattr(importer, "ROOT", tmp_path)

    def fake_import(source, **kwargs):
        raw_dir = kwargs["raw_dir"]
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "chembl_assay_grain_test.json"
        raw_path.write_text("{}", encoding="utf-8")
        return {
            "records": [
                {
                    "compound_id": "CHEMBL1",
                    "standardized_smiles": "CCO",
                    "source_id": "chembl_bioactivity",
                    "assay_id": "CHEMBL999",
                    "endpoint": "IC50",
                    "dose": None,
                    "dose_unit": None,
                    "treatment_time_hours": None,
                    "direction": "supports",
                    "value": 1.0,
                    "unit": "nM",
                    "control_id": None,
                    "batch_id": 1,
                    "source_url": "https://example.test",
                    "retrieved_at": "2026-07-15T00:00:00+00:00",
                    "license": "cc",
                }
            ],
            "query_terms": ["lipid accumulation"],
            "assay_count": 1,
            "activity_count": 1,
            "grain": "compound_x_assay_x_activity",
            "raw_path": str(raw_path),
        }

    monkeypatch.setattr(importer, "import_chembl_assay_grain", fake_import)
    source = {
        "source_id": "chembl_bioactivity",
        "import_wave": "wave_1_activity",
        "source_url": "https://example.test",
        "license_policy": "cc",
        "api_base": "https://example.test",
    }
    payload = importer.run_one(source, limit=5, dry_run=False)
    assert payload["status"] == "imported"
    assert payload["row_count"] == 1
    assert payload["grain"] == "compound_x_assay_x_activity"
    processed = tmp_path / "processed" / "chembl_bioactivity" / "records.jsonl"
    assert processed.is_file()
    row = json.loads(processed.read_text(encoding="utf-8").splitlines()[0])
    assert row["assay_id"] == "CHEMBL999"


def test_import_chembl_by_inchikeys_merges_exact_compound_activities(tmp_path: Path) -> None:
    calls: list[tuple[str, dict | None]] = []

    def fake_get(url: str, params: dict | None = None, timeout: int = 60):
        calls.append((url, params))
        if url.endswith("/molecule/AAAA-BBBB-N.json"):
            return {"molecule_chembl_id": "CHEMBL999001"}
        if url.endswith("/molecule/EMPTY-KEY-N.json"):
            raise Exception("404")
        if url.endswith("/molecule.json"):
            return {"molecules": []}
        if url.endswith("/activity.json") and (params or {}).get("molecule_chembl_id") == "CHEMBL999001":
            return {
                "activities": [
                    {
                        "activity_id": 42,
                        "molecule_chembl_id": "CHEMBL999001",
                        "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
                        "assay_chembl_id": "CHEMBL2156870",
                        "standard_type": "Activity",
                        "standard_value": 30.0,
                        "standard_units": "%",
                        "assay_description": (
                            "Lipid-lowering effect in human HepG2 cells assessed as "
                            "reduction in oleic acid-induced triglyceride accumulation"
                        ),
                        "bao_label": "cell-based format",
                        "activity_comment": "decreased triglyceride",
                    }
                ],
                "page_meta": {"total_count": 1},
            }
        return {"molecules": [], "activities": [], "page_meta": {"total_count": 0}}

    existing = tmp_path / "existing.jsonl"
    existing.write_text(
        json.dumps(
            {
                "compound_id": "CHEMBL1",
                "standardized_smiles": "CCO",
                "source_id": "chembl_bioactivity",
                "assay_id": "CHEMBL1",
                "endpoint": "IC50",
                "dose": None,
                "dose_unit": None,
                "treatment_time_hours": None,
                "direction": "unknown",
                "value": 1.0,
                "unit": "nM",
                "control_id": None,
                "batch_id": 1,
                "source_url": "https://example.test",
                "retrieved_at": "2026-07-15T00:00:00+00:00",
                "license": "cc",
                "activity_id": 1,
                "classification": "annotation",
                "assay_description": "binding",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = import_chembl_by_inchikeys(
        SOURCE,
        ["AAAA-BBBB-N", "EMPTY-KEY-N", "AAAA-BBBB-N"],
        max_activities_per_molecule=10,
        merge_existing_path=existing,
        get_json=fake_get,
        raw_dir=tmp_path / "raw",
    )
    assert result["inchikeys_resolved"] == 1
    assert result["inchikeys_verified_empty"] == 1
    assert result["activity_count"] >= 2
    hit = next(r for r in result["records"] if r.get("activity_id") == 42)
    assert hit["inchikey"] == "AAAA-BBBB-N"
    assert hit["classification"] == "positive_phenotype"
    assert (tmp_path / "raw").is_dir()
    assert any("molecule/AAAA-BBBB-N.json" in url for url, _ in calls)
