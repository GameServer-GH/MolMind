# plugins/molmind_core/scientific/pipeline

七步流水线编排与离线/Quality-Max 出口。排名过程只消费冻结快照和本地公开/QC
数据；即使兼容参数传入 `allow_live=true`，同一 Run 的 Top-M 复核也不发起 HTTP。
需要联网补证时使用独立 `query_evidence` 或显式 bake，并在新 Run 中重放已冻结结果。
