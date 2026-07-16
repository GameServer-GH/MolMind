# ToxCast CTX fixtures

Offline CTX Bioactivity JSON per DTXSID for Wave-2 imports when `CTX_API_KEY`
is unset. These are abbreviated, audit-oriented rows (not a full invitrodb dump).

- Active hits (`hitc >= 0.9`) → `risk_signal` only
- Inactive / non-hit → never a safety clearance label

Live refresh (requires free API key from `ccte_api@epa.gov`):

```bash
export CTX_API_KEY=...
PYTHONPATH=. python scripts/import_public_data.py \
  --source epa_toxcast_tox21 --limit 40 \
  --toxcast-cache data/public/raw/epa_toxcast_tox21/cache \
  --sync-registry
```
