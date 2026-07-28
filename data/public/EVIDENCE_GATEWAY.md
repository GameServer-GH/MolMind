# Evidence gateway and local-first query policy

MolMind must be reproducible offline while still allowing explicit online
evidence enrichment. The project therefore separates:

1. molecule identity,
2. source query state,
3. immutable raw payload,
4. normalized evidence,
5. frozen scoring snapshots.

For every `source × molecule × endpoint`, the canonical query status is one of:

- `hit`: a matching local or remote record exists;
- `verified_empty`: the provider returned a valid empty response;
- `query_failed`: transport, timeout, rate-limit, schema, or server failure;
- `auth_missing`: required credentials were unavailable;
- `not_queried`: no attempt was made, including offline cache misses;
- `identity_review_required`: identities conflict or resolve ambiguously;
- `annotation_only`: a record is usable only as identity/mechanism context and
  has ranking score and confidence equal to zero.

Legacy adapter values remain accepted internally for compatibility, but they
are not the public contract. `exact_hit` and `analogue_hit` normalize to `hit`
with the distinction preserved in `match_type`. `adapter_error`, `timeout`,
`rate_limited`, and `network_error` normalize to `query_failed`, with the
specific cause retained in structured audit details. `audit_missing` is an
overall candidate/tool outcome when no identity can be resolved, not a query
status and never a biological label.

`verified_empty` has a finite TTL. It is never a biological negative label and
must be queried again after expiry because public databases change.

## Identity resolution

Identity is resolved once before provider planning. The priority is
`original_inchikey`, `standardized_inchikey`, CAS, then standardized SMILES.
Every provider result retains the actual `lookup_field`, `lookup_value`, and
`match_type`; a preferred field must not be reported when a fallback was
actually used. Multiple CIDs, conflicting CAS mappings, or disagreement between
an explicit identifier and its corresponding SMILES-derived identity yields
`identity_review_required`. Original and standardized InChIKeys may differ only
when the current Run carries explicit `standardization_steps`; an unexplained
change also requires review. Provider compound IDs are reconciled again after
snapshot, cache, and live results merge, so CID/DTXSID/ChEMBL identity drift
cannot escape through a later layer. Such evidence cannot raise efficacy,
novelty, or safety confidence. Any conservative risk propagation must remain
an explicit, audited project rule.

## Candidate-query decision

```text
resolve identity
  -> frozen snapshot: reuse matching evidence
  -> local public/QC tables: merge matching non-network evidence
  -> fresh gateway hit: read cached payload
  -> fresh verified_empty: skip remote, report no record found
  -> query_failed/auth_missing inside backoff: skip remote, preserve failure
  -> stale empty/failure or not_queried: enqueue only when allow_live=true
  -> normalize EvidenceBundle, persist state, and emit query audit
```

`verified_empty` has a finite TTL. A fresh empty response suppresses another
request for that provider/endpoint during the current query, but still means
only “no record found”. A stale empty response may be retried only in explicit
live mode. `query_failed` and `auth_missing` obey their independent backoff.
An offline cache miss remains `not_queried` / `audit_missing`; it is not a
successful negative result.

Frozen snapshot rows and gateway query-state rows are distinct but share the
same public status semantics. Offline mode always replays an available frozen
row for reproducibility. In explicit live mode, snapshot `hit`,
`annotation_only`, and `verified_empty` rows use the configured TTL;
`force_refresh=true` or expiry permits a provider refresh while retaining the
frozen row in the audit. A gateway cache hit is reusable only when its payload
hash verifies and its adapter version plus endpoint URL still match the active
provider contract. Missing or corrupt payloads are failures or retry work,
never successful hits.

Online queries are explicit enrichment jobs, not implicit scoring side
effects. A canonical offline run uses a frozen evidence snapshot so the same
SDF, configuration and snapshot reproduce the same ranking. The standalone
`query_evidence` tool never writes selection or mutates the current Run's
`S_lipid`, `R_tox`, `novelty`, `conf_e`, Top N, or `selection_sha256`.
Evidence intended to affect a later ranking must first be normalized, audited,
and frozen into a snapshot, then consumed by a new offline run.

The ranking Top-M pass now replays only frozen/local evidence, even if a caller
sets `allow_live=true`; its logs report `same_run_live_scoring=blocked`.
Explicit `bake-evidence` performs provider-bounded Gateway enrichment and writes
normalized rows to a snapshot. A later offline Run may consume that snapshot.
This separation preserves the frozen scoring formula while removing implicit
HTTP side effects from ranking and evaluation.

`plugins/molmind_core/scientific/evidence_gateway/planner.py` applies the cache
decision table. It plans provider work but does not call the network. Provider
workers persist canonical status and audit metadata without converting a
transport outcome into task evidence.

## Tool contract and evidence card

`plugins.molmind_core.tools.scientific.run_query_evidence` accepts either a
current screening result plus `molecule_id`, or direct InChIKey/CAS/SMILES
identity. `providers` and `query_types` can narrow the plan. Defaults are
`allow_live=false` and `force_refresh=false`; force refresh never grants network
access by itself.

The result contains `.ok`, `.error_code`, `.message`, `.card`, `.bundle`,
`.degraded_channels`, and `.identity`. Missing identity returns
`error_code=audit_missing`; an unknown Run molecule is reported explicitly and
is never guessed. The evidence card summarizes:

- the candidate and identity actually used;
- source hits and sources that were empty, skipped, failed, or lacked auth;
- task evidence versus annotation/mechanism context;
- identity conflicts and degraded channels;
- the permitted scientific claim ceiling and whether explicit live enrichment
  may be useful.

Offline snapshot example:

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from plugins.molmind_core.tools.scientific import run_query_evidence

result = run_query_evidence(
    inchikey="PCZOHLXUXFIOCF-BXMDZJJMSA-N",
    allow_live=False,
)
print(result.card)
PY
```

Explicit live enrichment example:

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from plugins.molmind_core.tools.scientific import run_query_evidence

result = run_query_evidence(
    inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    cas="64-17-5",
    smiles="CCO",
    providers=["chembl", "pubchem"],
    allow_live=True,
    force_refresh=True,
)
print(result.card)
PY
```

Each normalized evidence row must include a stable `evidence_id`, provider or
`adapter_id`, `query_type`, `evidence_role`, `evidence_type`, canonical
`query_status`, lookup identity and `match_type`, endpoint, direction,
score/confidence, source URL or accession, retrieval time, source/adapter
version, `response_sha256`, and a `claim_ceiling` or equivalent boundary.
Query-audit rows remain separate from task evidence and have score/confidence
zero.

Inspect aggregate state or one molecular identity without reading payloads:

```bash
PYTHONPATH=. python scripts/report_evidence_cache.py
PYTHONPATH=. python scripts/report_evidence_cache.py \
  --source epa_ctx --entity HBGOLJKPSFNJSD-UHFFFAOYSA-N
```

Provider adapters may run in parallel, but each provider has its own bounded
concurrency, rate limit, timeout and circuit breaker. A failure in one provider
is recorded in `degraded_channels` and does not prevent other providers from
finishing. Duplicate `molecule/provider/endpoint` work is suppressed within a
query Run, and normalized output order is deterministic rather than completion
ordered. ChEMBL, PubChem and EPA can map candidate identity. BindingDB is
target-first mechanism evidence. GEO/PRIDE/metabolomics are study-level
mechanism/QC resources and must not be forced into molecule-level mapping.
Rate limits apply to actual HTTP subrequests, not only the outer molecule job;
timeouts begin when bounded work is dispatched rather than while waiting in an
unbounded queue. Circuit-open work that was never sent is `not_queried` and has
no fabricated remote-end event.

Current adapter status:

| Provider | Shared query state | Mapping scope |
|---|---|---|
| EPA CTX | live exact-InChIKey adapter returns identity annotation and only the existing strong-cytotox risk tier; CAS/multiple DTXSID is identity review | InChIKey/CAS → DTXSID |
| ChEMBL | standalone exact-key activity adapter is live-capable | InChIKey → molecule + activity |
| PubChem | standalone live adapter queries tox/GHS context; public BioAssay grain stays annotation-only | InChIKey/CAS → CID + tox context |
| BindingDB | local-only in candidate query; target-first mechanism support has score=0 | target → ligand identity |
| DILIrank | exact-identity gate via offline `identity_mapped.jsonl` (Most hard-exclude; else annotate); never ranking_weight | InChIKey/CAS exact |
| GEO/PRIDE/metabolomics | local-only; do not use molecule identity cache | study accession |

## Secrets

EPA CTX resolves an explicit runtime value, then `CTX_API_KEY` (or aliases),
then `CTX_API_KEY_FILE`, and finally the local OS keychain. No recoverable
credential is stored in the repository. Runtime deployments should use a
mounted file. Query audits, cache rows and errors redact credential-shaped
values. Any historical key requires manual rotation; Git history is not
rewritten by this project.
