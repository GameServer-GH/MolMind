# 证据快照（JSONL）

每行一条 JSON，按 `inchikey`（或经结构校验的 `cas`）索引。Quality-Max
排名只读这里和本地公开/QC 表，不执行隐式 HTTP。`online` 兼容参数只表达显式
enrichment 授权；要补证据请使用 `query_evidence` 或 `bake-evidence`，再启动新的
离线排名 Run。

> **与公开数据导入的分界**：本目录是运行时 EvidenceFacade 快照（按候选 InChIKey bake / compact）。
> `data/public/processed/*/records*.jsonl` 是 registry 导入的 assay-grain 表，经 QC 后由 facade 按 InChIKey 合并；二者谱系不同，不要混为同一张训练表。

## 字段

```json
{
  "inchikey": "AAAAAAAAAAAAA-BBBBBBBBBB-N",
  "cas": "123-45-6",
  "adapter_id": "chembl_lipid_v1",
  "query_type": "lipid",
  "score": 0.72,
  "confidence": 0.6,
  "evidence_id": "chembl:CHEMBL123:lipid",
  "payload": {},
  "endpoint": "cellular_lipid_reduction",
  "direction": "supports",
  "evidence_role": "task_evidence",
  "evidence_type": "endpoint_evidence",
  "query_status": "hit",
  "lookup_field": "original_inchikey",
  "lookup_value": "AAAAAAAAAAAAA-BBBBBBBBBB-N",
  "match_type": "exact_original_inchikey",
  "source_url": "https://example.org/record",
  "retrieved_at": "2026-07-27T00:00:00+00:00",
  "source_version": "source-release",
  "adapter_version": "adapter-contract-v1",
  "response_sha256": "...",
  "claim_ceiling": "candidate_preclinical_evidence_only"
}
```

`query_type`：`lipid` | `tox` | `novelty` | `pathway` | `annotation` |
`query_audit`。查询审计行必须是 0 分；`verified_empty` 仅表示该次有效查询未返回记录，
不表示无效、无毒或生物学阴性。CAS 命中若绑定到不同 InChIKey，必须进入
`identity_review_required`，不能作为效力、创新性或安全置信证据。

## 推荐用法（有网烘焙一次 → 默认 auto 稳定跑）

```bash
# 1) 提交级一次性烘焙：冻结 Top 10 + Top-M 候选窗口（默认 bake_top_m=120）
PYTHONPATH=. python -m apps.cli.main \
    --input "data/T001 TargetMol现货产品22966.sdf" \
    --bake-evidence \
    --bake-submission \
    --bake-top-m 120 \
    --bake-force

# 2) 正式跑（默认 auto：仅消费已冻结 snapshot/local 数据）
PYTHONPATH=. python -m apps.cli.main \
    --input "data/T001 TargetMol现货产品22966.sdf" \
    --output output/nomination_top10.csv
```

候选级公开数据扩展（与 bake 互补；annotation 不抬分）：

```bash
PYTHONPATH=. python scripts/import_public_data.py \
  --source chembl_bioactivity --limit 40 \
  --candidate-inchikeys data/public/manifests/candidate_inchikeys_topm_expand.txt \
  --sync-registry
PYTHONPATH=. python scripts/qc_public_assay_grain.py
# ToxCast 候选级使用 configs/evidence_providers.yaml 中的 CTX key：
# PYTHONPATH=. python scripts/import_public_data.py \
#   --source epa_toxcast_tox21 --limit 40 \
#   --candidate-inchikeys data/public/manifests/candidate_inchikeys_topm_expand.txt
```

发行镜像应包含本目录下的 JSONL，断网部署时仍可得到证据增强分数。

## 覆盖旧快照

筛选 Run 不再隐式联网，也不会自动写 `auto_cache.jsonl`。需要刷新时，应显式运行
`bake-evidence`；Gateway 先遵守 SQLite 查询状态中的失败 backoff，再把规范化 evidence
与 query audit 追加到目标快照。强制 bake 后可压缩同一实体的历史记录：

```bash
PYTHONPATH=. python -m apps.cli.main --compact-snapshot
```

或强制重烘焙短名单并覆盖：

```bash
PYTHONPATH=. python -m apps.cli.main \
  --input "data/T001 TargetMol现货产品22966.sdf" \
  --bake-evidence --bake-force --bake-top-m 100
```

## 文件

| 文件 | 来源 |
|------|------|
| `baked_chembl_pubchem.jsonl` | `--bake-evidence` |
| `baked_evidence_v2.jsonl`（根目录与/或 `v2/`） | `--bake-submission` / `--bake-frozen-top10` |
| `baked_evidence_*.jsonl.manifest.json` | 快照 SHA-256、算法冻结摘要、查询实体和失败统计 |
| `v2/baked_evidence_topm80_*.jsonl` | 历史 Top-M80 烘焙产物 |
| `v2/baked_evidence_topm120_*.jsonl` | Top-M120 提交级烘焙产物（含 Critic 漂移席位） |
| `auto_cache.jsonl` | 历史兼容文件；当前筛选 Run 不再自动写入 |
| `*.jsonl.bak` | `--compact-snapshot` / `--bake-force` 压缩前备份 |

## HepG2-FFA 公共资源注册表

`v2/hepg2_ffa_resources_v1.json` 保存 PRIDE/ProteomeXchange、SSBD 和公开论文的规范化元数据与来源哈希。它们分为：

- `mechanistic_context`：PA/FFA 诱导的蛋白组或通路背景；
- `assay_qc`：FFA 模型浓度响应和实验质量控制；
- `candidate_evidence_curation`：后续人工整理的候选级文献双终点记录。

该注册表强制 `ranking_effect=none`。只有同一候选、同一剂量/时间/FFA 条件下同时存在脂质与细胞活力读出，并具备来源、批次和对照信息，才可能进入双终点训练接口。

验证 SSBD 原始包和注册表：

```bash
PYTHONPATH=. python scripts/validate_hepg2_ffa_resources.py \
  --ssbd-archive /path/to/Fig4_HepG2FFA.zip
```

组学或拉曼资源不得被解释为候选化合物低毒降脂的实验命中，也不得清除 `hepg2_ffa_dual_endpoint_model_unavailable`。
