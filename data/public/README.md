# MolMind public-data workspace

This directory holds public experimental, toxicology, transcriptomic, proteomic
and metabolomic assets used to **train or audit** MolMind as an auditable
computational candidate-prioritization system for MASLD / HepG2-FFA.

It may contain GB-scale local files. Only manifests, checksums, licence-allowed
derived tables, and reproducibility metadata should be committed to git.

## Project positioning

MolMind prioritizes candidates using:

1. public experimental activity evidence (assay-grain),
2. toxicology / clinical liver-risk signals,
3. multi-omics mechanism context.

It does **not** claim wet-lab-validated lipid-lowering or safety. Missing records
stay `audit_missing` and are never converted into inactive or low-toxicity labels.
Network failures are recorded as `network_error` manifests — never as “no activity”
or “low toxicity”.

## Import priority (from `registry.yaml`)

Operational waves must be followed in order. Later waves must not rewrite earlier
assay-grain schemas.

| Wave | Sources | Ranking effect |
|---|---|---|
| **1 · Activity** | ChEMBL → PubChem BioAssay → BindingDB | task evidence / mechanism support only |
| **2 · Toxicology** | ToxCast/Tox21 → DILIrank 2.0 → ToxRef/ToxVal | risk signal only |
| **3 · Multi-omics** | GEO → PRIDE → MetaboLights/Workbench → LINCS/CMap | mechanism support only (`ranking_weight=0` by default) |

### Capture status (2026-07-16)

| Source | `ingestion_status` | Snapshot notes |
|---|---|---|
| `chembl_bioactivity` | **imported** | **1128** assay-grain rows → QC **209** (203 positive / 6 adverse); seed HepG2-FFA + **candidate InChIKey expansion** (`--candidate-inchikeys`) |
| `pubchem_bioassay` | **imported** | **176** rows → QC **26** (Unspecified dropped); Active stays annotation_only |
| `bindingdb` | **imported** | Lipid UniProt subset **74** rows → QC **71** (`mechanism_support` only; binding ≠ lipid-lowering); 16 targets round-robin |
| `epa_toxcast_tox21` | **imported** | CTX Bioactivity by DTXSID (fixtures offline / `CTX_API_KEY` live): **9** rows → QC **7** active hits (`risk_signal` only; inactive ≠ safe). Candidate InChIKey→DTXSID needs API key |
| `fda_dilirank_2` | **imported** | Official XLSX + processed table + SHA-256 |
| Wave-3 omics | `imported_metadata_only` | Accession plans only |

Statuses are audit facts, not biological labels. QC row counts in this table should
match `manifests/` and `registry.yaml` `qc_pass_rows` / `positive_phenotype_rows`.
Runtime EvidenceFacade adapters and HepG2-FFA resource metadata remain separate
from full public-data ingestion.

### Wave 1 commands

```bash
# ChEMBL assay-grain (compound × assay × activity)
# Seed-driven HepG2-FFA expansion (search skipped when ChEMBL assay search stalls);
# merges prior rows. Curated IDs: services/public_data/chembl.py SEED_HEPG2_FFA_ASSAY_IDS
# Discovery fixture: data/public/fixtures/chembl_hepg2_ffa_seed_candidates.json
PYTHONPATH=. python scripts/import_public_data.py \
  --source chembl_bioactivity --limit 32 --sync-registry

# Optional: expand ChEMBL by Top-M / shortlist InChIKeys (annotation stays annotation_only)
PYTHONPATH=. python scripts/import_public_data.py \
  --source chembl_bioactivity --limit 40 \
  --candidate-inchikeys data/public/manifests/candidate_inchikeys_topm_expand.txt \
  --sync-registry

# PubChem concise + structure identity (optional offline cache)
PYTHONPATH=. python scripts/import_public_data.py \
  --source pubchem_bioassay --limit 50 \
  --pubchem-cache data/public/raw/pubchem_bioassay \
  --sync-registry

# BindingDB lipid UniProt binding (mechanism_support only; prefer local cache)
PYTHONPATH=. python scripts/import_public_data.py \
  --source bindingdb --limit 80 \
  --bindingdb-cache data/public/raw/bindingdb/cache \
  --sync-registry

# ToxCast/Tox21 via CTX (DTXSID). Offline uses data/public/fixtures/toxcast_ctx/
# Live / InChIKey→DTXSID: export CTX_API_KEY=...  (free key from ccte_api@epa.gov)
PYTHONPATH=. python scripts/import_public_data.py \
  --source epa_toxcast_tox21 --limit 40 --sync-registry
# With candidate InChIKeys (requires CTX_API_KEY):
# PYTHONPATH=. python scripts/import_public_data.py \
#   --source epa_toxcast_tox21 --limit 40 \
#   --candidate-inchikeys data/public/manifests/candidate_inchikeys_topm_expand.txt \
#   --sync-registry

# Endpoint QC: drop Unspecified / non-phenotype; write records_endpoint_qc.jsonl
PYTHONPATH=. python scripts/qc_public_assay_grain.py
```

Artifacts (raw/processed gitignored; commit manifests):

- `manifests/<source_id>.json` — status, checksums, query, identity stats
- `manifests/assay_grain_qc.json` — QC pass/exclude counts
- `processed/<source_id>/records.jsonl` — full assay-grain rows
- `processed/<source_id>/records_endpoint_qc.jsonl` — QC subset for facade/training triage
- `raw/<source_id>/` — original responses / cache files

EvidenceFacade (`evidence.public_assay_grain.enabled`) loads QC tables by exact
InChIKey. PubChem `Active` stays `annotation_only` (no `conf_e` lift). Only
ChEMBL phenotype `task_evidence` may enter lipid/tox score channels.

## Layout

```text
data/public/
├── raw/          # downloaded source files; preserve accession and licence
├── processed/    # normalized assay-grain tables and feature matrices
├── models/       # exported inference artifacts and model cards
├── manifests/    # download, checksum, split and training manifests
└── registry.yaml # source role, import wave, claim policy
```

`registry.yaml` is the policy source of truth. A source with
`ingestion_status: planned`, `network_error`, or `imported_metadata_only` must
not be described as a validated training dataset. Failed retrievals remain
manifests only.
