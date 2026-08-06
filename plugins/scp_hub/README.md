# SCP Hub 集成

MolMind 的受护 MCP 边界。Live 调用需要 `SCP_HUB_API_KEY` 与白名单 HTTPS 端点。
远端观测恒设 `writes_selection=false` 与 `participates_in_ranking=false`，**不得**改写同轮主榜或 Nomination。

`data/scp_hub/catalog_index.json` 是发现种子。Skill 仅在「已审核、已钉扎」的描述符包含如下 server 条目后才可安装：

```json
{
  "server_id": "reviewed-server-id",
  "endpoint": "https://scp.intern-ai.org.cn/api/v1/mcp/<id>/<name>",
  "tools": ["exact_wire_tool_name"]
}
```

工具名必须与 live `tools/list` 响应完全一致。依赖缺失或漂移时安装失败关闭（fail closed），不会猜测重命名工具。

Catalog 入口：[`configs/agent/plugins/catalog/scp-hub.yaml`](../../configs/agent/plugins/catalog/scp-hub.yaml)（`builtin: false`，会话 opt-in）。
Compose / 本机请在仓库根 `.env` 设置 `SCP_HUB_API_KEY`，并用
`docker compose --env-file .env -f deploy/docker-compose.yml up` 传入。

后台 SCP Job 带 lease 心跳与崩溃回收；取消路径与 Agent Run 硬中断对齐。更多契约见单元测试 `tests/unit/test_scp_*.py`。
