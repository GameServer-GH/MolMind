# plugins/molmind_core/scientific/evidence_facade

证据门面只负责确定性的 snapshot / 本地公开数据重放与规范化，不在筛选
Run 内发起 HTTP。独立 Tool 或显式 bake 的联网补证统一经过 Evidence Gateway：

```text
snapshot / local QC
  → Gateway query-state cache
  → only when allow_live=true: provider-bounded live adapters
  → normalized EvidenceBundle / query audit
  → explicit bake snapshot for a later ranking Run
```

`verified_empty` 只表示一次有效查询没有返回记录；`query_failed`、
`auth_missing`、`not_queried` 和 `identity_review_required` 都是零分查询审计，
不得解释成无毒、无效或安全。`query_evidence` 只读且
`writes_selection=false`。
