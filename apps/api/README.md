# apps/api

FastAPI：上传 SDF、预览、下载 TopN CSV。

## 版本信息

- 项目版本以根目录 `pyproject.toml` 的 `[project].version` 为唯一源码来源（当前 **0.2.3**）。
- API 启动时优先读取当前项目的 `pyproject.toml`，避免本地残留的旧版安装包元数据覆盖当前版本。
- 仅在源码文件不可用时，才回退到已安装的 `molmind` 包元数据。
- FastAPI 应用版本和 `GET /health` 返回的 `version` 使用同一个解析结果。
- Web Profile 详情通过 `GET /health` 自动展示版本和 `build` 标识，打开弹窗时刷新，不需要在前端重复维护版本号。

## Agent 存储与队列（0.2.3）

本地与生产一致，不再默认 FileRunStore / SQLite：

| 组件 | 角色 | 环境变量 |
|------|------|----------|
| **PostgreSQL** | 会话 / 事件 / checkpoint / Run 队列 / 后台 Job 真源 | `MOLMIND_DATABASE_URL`、`MOLMIND_AGENT_QUEUE_URL` |
| **Redis** | 会话短锁与事件扇出（可降级到 PG advisory lock） | `MOLMIND_REDIS_URL` |
| **Blob** | Turn 附件与大对象（默认本地目录；可切 MinIO/S3） | `MOLMIND_BLOB_ROOT` / `MOLMIND_BLOB_STORE_URL` |

Compose 默认拉起 `postgres` + `redis`；可选 `--profile object` 启用 MinIO。详见 [deploy/README.md](../../deploy/README.md) 与 `.env.example`。

主要入口：

- 健康检查与版本信息：`GET /health`
- Agent：`/api/agent/sessions` · message stream · events（`after_seq` / SSE `events/stream`）· settings / catalog
- Turn 队列：`POST .../turns`（`auto|queue|guidance|run_now`）；会话载荷含 `queue_count` / `queue_limit`（默认最多 3 条普通排队）与 `pending_turns`
- 硬中断：`POST .../runs/{run_id}/interrupt`；活动写冲突仍返回 `409 session_busy`
- Turn 附件：`POST|DELETE .../turn-attachments`（不改写进行中的 Run）
- 失败重试：`POST .../runs/{run_id}/retry`
- 会话载荷含 `active_run` / `revision` / `staged_attachments` / `installed_scp_skills`
- SCP Hub（可选）：Catalog 安装后经白名单 MCP 做 enrichment；观测恒为 `writes_selection=false`，不参与同轮排名
- Swagger UI：`GET /docs`
- OpenAPI 描述：`GET /openapi.json`
