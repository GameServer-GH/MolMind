# 证据快照（JSONL）

每行一条 JSON，按 `inchikey`（或 `cas`）索引。Quality-Max（`mode=auto`）与调试 `offline`/`online` 在 `prefer_snapshot=true` 时都**优先读这里**，命中则不再打外网。

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
  "payload": {}
}
```

`query_type`：`lipid` | `tox` | `novelty` | `pathway`

## 推荐用法（有网烘焙一次 → 默认 auto 稳定跑）

```bash
# 1) 对组委会库短名单拉取 ChEMBL/PubChem，写入本目录
PYTHONPATH=. python -m app.main \
  --input "../docs/demo/T001 TargetMol现货产品22966.sdf" \
  --bake-evidence \
  --bake-top-m 80

# 2) 定榜（默认 auto：有 snapshot 则本地高质量；缺洞才 live）
PYTHONPATH=. python -m app.main \
  --input "../docs/demo/T001 TargetMol现货产品22966.sdf" \
  --output output/nomination_top10.csv
```

交付镜像应提交本目录下的 JSONL，评委无网时仍可得到证据增强分数。

## 覆盖旧快照

关闭网页「使用证据快照」跑 live 时，结果会 **追加** 到 `auto_cache.jsonl`。读取已改为 **同 key 取最后一条**；建议再压一次文件：

```bash
PYTHONPATH=. python -m apps.cli.main --compact-snapshot
```

或强制重烘焙短名单并覆盖：

```bash
PYTHONPATH=. python -m apps.cli.main \
  --input "docs/demo/T001 TargetMol现货产品22966.sdf" \
  --bake-evidence --bake-force --bake-top-m 100
```

## 文件

| 文件 | 来源 |
|------|------|
| `baked_chembl_pubchem.jsonl` | `--bake-evidence` |
| `auto_cache.jsonl` | auto/online 运行时自动缓存 live 结果 |
| `*.jsonl.bak` | `--compact-snapshot` / `--bake-force` 压缩前备份 |
