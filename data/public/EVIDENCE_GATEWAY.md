# Evidence gateway and local-first query policy

MolMind must be reproducible offline while still allowing explicit online
evidence enrichment. The project therefore separates:

1. molecule identity,
2. source query state,
3. immutable raw payload,
4. normalized evidence,
5. frozen scoring snapshots.

For every `source × molecule × endpoint`, query state is one of:

- `hit`: a local payload exists;
- `verified_empty`: the source returned a valid empty response;
- `query_failed`: network/schema/server failure, retryable;
- `auth_missing`: credential unavailable, retryable;
- `not_queried`: no attempt has been made.

`verified_empty` has a finite TTL. It is never a biological negative label and
must be queried again after expiry because public databases change.

## SDF ingestion decision

```text
standardize identity
  -> fresh local hit: read local payload
  -> fresh verified_empty: skip remote for this run, keep audit_missing
  -> stale / query_failed: enqueue retry when online
  -> not_queried: enqueue provider query when online
  -> offline: keep audit_missing
```

Online queries are enrichment jobs, not implicit scoring side effects. A
canonical offline run uses a frozen evidence snapshot so the same SDF,
configuration and snapshot always reproduce the same ranking.

`services/evidence_gateway/planner.py` applies this decision table to imported
SDF identities. It plans provider work but does not call the network. Provider
workers then persist `hit`, `verified_empty`, or retryable failure state. This
keeps an SDF upload responsive and prevents a temporary API outage from
silently changing model scores.

Inspect aggregate state or one molecular identity without reading payloads:

```bash
PYTHONPATH=. python scripts/report_evidence_cache.py
PYTHONPATH=. python scripts/report_evidence_cache.py \
  --source epa_ctx --entity HBGOLJKPSFNJSD-UHFFFAOYSA-N
```

Provider adapters may run in parallel, but each provider has its own
concurrency, rate limit, timeout and circuit breaker. ChEMBL, PubChem and EPA
can map candidate identity. BindingDB is target-first mechanism evidence.
GEO/PRIDE/metabolomics are study-level mechanism/QC resources and must not be
forced into molecule-level mapping.

Current adapter status:

| Provider | Shared query state | Mapping scope |
|---|---|---|
| EPA CTX | wired to full-SDF identity mapping | InChIKey/CAS → DTXSID |
| ChEMBL | wired to candidate exact-key expansion | InChIKey → molecule + activity |
| PubChem | provider policy/planner ready; tox live/bake primary; BioAssay grain annotation-only until candidate hit\|verified_empty coverage | InChIKey/CAS → CID + BioAssay |
| BindingDB | keep target-first cache; mechanism_support score=0 only | target → ligand identity |
| DILIrank | exact-identity gate via offline `identity_mapped.jsonl` (Most hard-exclude; else annotate); never ranking_weight | InChIKey/CAS exact |
| GEO/PRIDE/metabolomics | do not use molecule identity cache | study accession |

## Secrets

At the project owner's explicit request, EPA CTX is currently stored as
plaintext `api_key` in `configs/evidence_providers.yaml`. This is portable but
means every project copy exposes the credential. Environment injection remains
the first override and should be restored before any public repository release.
