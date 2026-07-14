# MolMind

<p align="center">
  <img src="apps/web/static/MolMindIntroduction.png" alt="MolMind — Quality-Max compound discovery pipeline" width="100%" />
</p>

<p align="center">
  <strong>Novel low-toxicity lipid-lowering compound discovery</strong> · Quality-Max release<br />
  A scientific-discovery AI agent pipeline<br />
  <a href="README.md">中文</a>
</p>

---

## What it does

MolMind is a computational screening system for **MASLD / HepG2-FFA**. Given a committee compound library (a single `.sdf`), it nominates candidates that are **lipid-lowering, low-toxicity, and relatively novel in chemical space**, together with auditable score rationales and mechanism hypotheses for wet-lab priority testing.

### The problem it targets

Fewer lipid droplets in a cell assay is not an automatic hit—if viability collapses, the lipid drop may be a death artifact. The challenge therefore defines:

> **Valid hit = lipid-lowering ∧ low toxicity**  
> Lipid drop with high toxicity = **false positive** (reject early, do not patch afterward)

### Computational vs. experimental tracks

| Experimental (committee validation) | Computational proxy (MolMind) |
|-------------------------------------|-------------------------------|
| Reduced lipid accumulation | Raise `S_lipid` |
| Cell viability ≥ 80% | Lower `R_tox` + hard toxicity gate |
| Valid nomination list | Gate-pass, then rank by `S_final` to Top N |
| SI / EC₅₀ / CC₅₀ | **Not computed, not written to CSV** (no dose–response → no false precision) |

In short: MolMind owns **reproducible computational nomination**; efficacy and safety boundaries are confirmed in wet lab via **HepG2-FFA dual endpoints** (optional SI as a follow-up protocol only).

---

## How it works

### Quality-Max: one primary path, environment-adaptive

Default is `mode=auto` (Quality-Max)—no forced Online/Offline choice for end users:

```text
local evidence snapshot → (shortlist) live gap-fill → rules / GoldSet / optional ML → Critic → Top 10
any channel failure → auto-degrade, record degraded_channels[] → still emit a deterministic shortlist
```

When online, live evidence can enrich scores; when offline or circuit-tripped, baked snapshots still support high-quality replay of the same shortlist.

### Seven-stage agent pipeline

Not “one script, one CSV”—a staged, observable, reflective discovery agent:

| Stage | Module | Role |
|-------|--------|------|
| 1 Ingest | `ingest` | Streaming SDF parse → descriptors / fingerprints / InChIKey |
| 2 Hard filter | `hard_filter` | Ro5, alert SMARTS, expert red-lines |
| 3 Lipid scoring | `scorer_lipid` | Multi-signal fusion → `S_lipid` + attributions |
| 4 Toxicity scoring | `scorer_tox` | Multi-head fusion → `R_tox`; hard gate drops high risk |
| 5 Ranking | `ranker` | Public `S_final` formula + Murcko scaffold diversity caps |
| 6 Critic | `critic` | GoldSet reflection: drop non-novel or high-risk lookalikes |
| 7 Export | `export` + `mechanism` | Nomination CSV + mechanism Markdown |

### Lipid: multi-signal fusion, not a single heuristic

Default `S_lipid` fusion (weights enter `config_hash`):

| Signal | Default weight | Meaning |
|--------|:--------------:|---------|
| Rules / pharmacophores | 0.35 | Pathway-aware SMARTS cues (e.g. DNL / FAO / AMPK) |
| Positive similarity | 0.30 | Tanimoto vs. GoldSet lipid-lowering, low-tox positives |
| External evidence | 0.25 | Via Evidence Facade (e.g. ChEMBL) |
| Optional ML | 0.10 | If missing, weight is dropped dynamically and `lipid_ml` is logged |

Empty evidence is never inflated into a high score; missing channels remain auditable.

### Toxicity: false-positive–first rejection

`R_tox` fuses structural alerts, DILI, ADMET, physicochemical risk, and tox evidence; GoldSet hepatotox analogs can receive a similarity boost. Defaults:

- **Hard gate** `R_tox > 0.65` → reject  
- **Soft threshold** `0.45` → ranking / caution  
- `viability_proxy = 0.80` aligns with experimental wording and **does not fabricate SI**

Toxicity is first-class: a lipid-looking score cannot bury a high-risk molecule into the shortlist.

### Ranking: efficacy × safety × novelty × evidence confidence

```text
S_final = 0.40·S_lipid + 0.40·(1 − R_tox) + 0.10·novelty + 0.10·conf_e
```

- **Novelty**: near-positive similarity suppresses `novelty` (anti me-too)  
- **Scaffold diversity**: Murcko seat caps (default one seat per scaffold for submission) prevent same-core monopolies in Top 10  
- **Evidence confidence** `conf_e`: mean retrieval quality—**not** biological novelty itself

### Critic: the agent’s reflection loop

Rule critic (on by default), typical actions:

- Near-identical to a library positive control → remove (reference drug ≠ new discovery)  
- Near-positive → soft-drop from Top, backfill a different scaffold  
- False-positive / hepatotox lookalike with high `R_tox` → remove  

An evidence-bound LLM critic path exists (may only cite `evidence_id`s already seen in the run); **ranking change is off by default**. The mechanism LLM only polishes Markdown **after ranking is frozen**.

### Evidence Facade: tool-using retrieval

Unified `EvidenceFacade.query()`:

- Versioned adapters (main path defaults: `chembl_lipid_v1` + `pubchem_tox_v1`)  
- Hits land in `attributions[]` / `evidence_id` and flow into CSV rationales  
- `prefer_snapshot=true`: skip the network when the local snapshot hits  
- HTTP timeout + consecutive-failure circuit breaker; failures append `degraded_channels[]`—no crash, no invented high scores  

### Reproducibility and audit trail

| Mechanism | Role |
|-----------|------|
| `config_hash` | Stable hash of rank/filter/model configs; written to CSV and API |
| Evidence snapshot | Shortlists can be pre-baked under `data/evidence_snapshot/` for offline replay |
| `degraded_channels[]` | e.g. `evidence_empty`, `lipid_ml`, circuit-open—degrades audibly |
| Attribution columns | Lipid/tox rationales and `overall_reason` trace back to signals and evidence IDs |

Acceptance criterion: **same SDF + same `config_hash` + same snapshot → same Top 10 IDs and scores**.

---

## What you get

### Artifacts of a run

| Artifact | Contents |
|----------|----------|
| **Nomination Top N CSV** | Default Top 10: IDs, factor scores, final score, rationales, `config_hash`, degrade flags |
| **Mechanism Markdown** | Pathway-whitelist–anchored, testable hypothesis; DeepSeek polish by default, template fallback on failure |
| **Live diagnostics** | Web / `POST /api/screen/stream` (NDJSON) / CLI stage progress—the agent’s work is visible |

### Delivery surfaces (one configuration)

| Surface | Use |
|---------|-----|
| Web | Upload SDF in-browser; watch logs and nominations |
| API | Programmable screening, streamed logs, downloads |
| CLI | Batch one-shot CSV |
| Docker Compose | One-command stack for local / reviewer replay |

### Intended result profile

A successful run is not “the ten most similar analogs with the highest raw score.” It aims for:

1. A lipid shortlist that **passes the toxicity gate**  
2. A Top N that is **scaffold-diverse** and has novelty headroom vs. positives  
3. Nominations that are **explainable** (traceable rationales; mechanism text usable in a verification plan)  
4. A **deterministic shortlist offline** when snapshots and the config fingerprint are fixed  

Mechanism prose aligns with wet-lab narrative: HepG2-FFA dual endpoints (lipid ↓ ∧ viability ≥ 80%); optional SI is a later confirmation protocol—**it never rewrites the computational ranking**.

---

## Quick start

Full deploy notes (macOS / Windows / optional pure Python): [deploy/README.md](deploy/README.md).

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Open <http://127.0.0.1:18765/> (health: `/health`). Do not open the static page via `file://`.

CLI one-liner:

```bash
python -m apps.cli.main --input data/sample.sdf --output output/nomination_top10.csv
```

Offline CLI smoke inside Docker:

```bash
docker compose -f deploy/docker-compose.yml run --rm cli
```

Mechanism Markdown defaults to DeepSeek `deepseek-v4-pro` polish (**Top 10 unchanged**). Missing key or call failure falls back to templates.

---

## Repository layout

| Path | Description |
|------|-------------|
| `apps/` | API, CLI, static Web UI |
| `services/` | Pipeline, scoring, evidence, critic, mechanism |
| `packages/` | Chem core, goldset, optional ML, shared records |
| `configs/` | Filter / score / rank weights and model manifest (`config_hash`) |
| `data/` | Sample SDF, goldset, evidence snapshots, reference tables, optional models |
| `deploy/` | Dockerfile, Compose, deploy notes |
| `scripts/` | Smoke, config gates, evidence bake utilities |
| `tests/` | Unit / regression / integration (pytest) |

---

## Tests

```bash
pytest
```
