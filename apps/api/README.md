# apps/api

FastAPI：上传 SDF、预览、下载 TopN CSV。

## 版本信息

- 项目版本以根目录 `pyproject.toml` 的 `[project].version` 为唯一源码来源。
- API 启动时优先读取当前项目的 `pyproject.toml`，避免本地残留的旧版安装包元数据覆盖当前版本。
- 仅在源码文件不可用时，才回退到已安装的 `molmind` 包元数据。
- FastAPI 应用版本和 `GET /health` 返回的 `version` 使用同一个解析结果。
- Web Profile 详情通过 `GET /health` 自动展示版本和 `build` 标识，不需要在前端重复维护版本号。

主要入口：

- 健康检查与版本信息：`GET /health`
- Swagger UI：`GET /docs`
- OpenAPI 描述：`GET /openapi.json`
