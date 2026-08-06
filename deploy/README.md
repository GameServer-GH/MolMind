# MolMind 部署与启动指南

命令默认在**仓库根目录**执行。镜像 tag 统一为 `molmind:0.2.3`，Compose 文件为 `deploy/docker-compose.yml`。

> Compose 文件位于 `deploy/`，**不会自动读取仓库根 `.env`**。需要环境变量（如 `SCP_HUB_API_KEY`）时请显式传入：
> `docker compose --env-file .env -f deploy/docker-compose.yml up -d`

访问：

| 入口 | 地址 |
|------|------|
| **在线试用** | <https://molmind.cn/>（健康检查：`/health`） |
| **本地 Compose** | <http://127.0.0.1:18765/>（健康检查：`/health`） |

请勿用 `file://` 打开静态页。

按环境选择一种方式即可：

| 环境 | 方式 | 说明 |
|------|------|------|
| **直接试用** | 打开 <https://molmind.cn/> | 无需安装，上传 SDF 即可 |
| **国内本地部署（推荐）** | 从 NAS 镜像仓库拉取 | 国内可达、速度快；临时开放 |
| **外网畅通** | 从 GitHub Container Registry 拉取 | 备选；国内访问 ghcr 可能很慢 |
| **本地开发调试** | Compose `--build` | 改代码后现场构建 |
| **无 Docker** | 本机 Python + conda | 需自行解决 RDKit；Agent 还需 Postgres（及可选 Redis） |

---

## 环境要求

| 项 | 说明 |
|----|------|
| Docker Desktop / Engine | 推荐用 Compose 启动 |
| 可选：Python 3.9+ | 仅不用 Docker、直接跑源码时需要 |
| RDKit | Docker 镜像已内置；源码安装失败可用 conda-forge |
| PostgreSQL 16 | Agent 会话 / 事件 / Run 队列 / 后台 Job 真源（Compose 默认内置） |
| Redis 7 | 短锁与事件扇出；可关并降级到 PG advisory lock（Compose 默认内置） |
| 可选 MinIO/S3 | Blob 对象存储；`docker compose --profile object` |

存储约定与环境变量模板见仓库根 [`.env.example`](../.env.example)。

---

## 方式一：NAS 镜像仓库（国内推荐）

成品镜像托管在国内可达的临时仓库，避免拉取 ghcr / Docker Hub 过慢或失败。

仓库地址（HTTP）：

```text
8.133.197.65:5001/molmind:0.2.3
```

### 1. 配置 insecure-registries（一次性）

仓库为 HTTP，需在 Docker Engine 中加入：

```json
{
  "insecure-registries": ["8.133.197.65:5001"]
}
```

- **Docker Desktop**：Settings → Docker Engine → 合并上述字段 → Apply & Restart  
- **Linux**：写入 `/etc/docker/daemon.json` 后执行 `sudo systemctl restart docker`

### 2. 拉取并启动

```bash
docker pull --platform linux/amd64 8.133.197.65:5001/molmind:0.2.3
docker tag 8.133.197.65:5001/molmind:0.2.3 molmind:0.2.3
mkdir -p output
# 可选：cp .env.example .env 后填入 SCP_HUB_API_KEY 等
docker compose --env-file .env -f deploy/docker-compose.yml up -d
curl http://127.0.0.1:18765/health
```

`--platform linux/amd64`：当前发行镜像以 amd64 为主；Apple Silicon 通过模拟运行即可。Compose 会一并启动 `postgres` 与 `redis`。

停止：`docker compose -f deploy/docker-compose.yml down`。

> 若拉取失败（超时 / Connection refused），改用下方「方式二」ghcr，或「方式三」现场构建。

---

## 方式二：从 GitHub（ghcr.io）拉取

外网畅通时的备选（国内往往较慢）：

```bash
docker pull ghcr.io/gameserver-gh/molmind:0.2.3
docker tag ghcr.io/gameserver-gh/molmind:0.2.3 molmind:0.2.3
mkdir -p output
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

Apple Silicon 若报无 arm64 manifest：

```bash
docker pull --platform linux/amd64 ghcr.io/gameserver-gh/molmind:0.2.3
docker tag ghcr.io/gameserver-gh/molmind:0.2.3 molmind:0.2.3
```

若包为 Private，需有读权限并先 `docker login ghcr.io`。更多见 [`images/README.md`](images/README.md)。

---

## 方式三：本地开发（Compose 现场构建）

先安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 或 Docker Engine，并确保 daemon 已启动。

### macOS / Linux

```bash
cd /path/to/MolMind
mkdir -p output
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

后台运行加 `-d`。停止：`docker compose -f deploy/docker-compose.yml down`。

可选对象存储（MinIO）：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml --profile object up -d minio
```

### Windows（PowerShell）

```powershell
cd C:\path\to\MolMind
New-Item -ItemType Directory -Force -Path output | Out-Null
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

后台：`docker compose --env-file .env -f deploy/docker-compose.yml up --build -d`  
停止：`docker compose -f deploy/docker-compose.yml down`

> 若提示找不到 `docker`，先打开 Docker Desktop，并确认 Settings → General 中启用了 Docker Compose。

### 可选：离线 CLI 冒烟

```bash
docker compose --env-file .env -f deploy/docker-compose.yml run --rm cli
```

结果写入宿主机 `./output/nomination_top10.csv`。

---

## 方式四：本机 Python（不用 Docker）

Agent 需要可达的 PostgreSQL（及可选 Redis）。可只起依赖容器：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d postgres redis
# 默认映射：Postgres 127.0.0.1:15432 · Redis 127.0.0.1:16379
# 复制 .env.example → .env，确认 MOLMIND_DATABASE_URL / MOLMIND_REDIS_URL
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e ".[dev]"
# 若 RDKit 安装失败：
# conda install -c conda-forge rdkit
uvicorn apps.api.app:app --host 0.0.0.0 --port 18765 --reload
```

### Windows（PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[dev]"
uvicorn apps.api.app:app --host 0.0.0.0 --port 18765 --reload
```

若执行策略阻止激活脚本：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

访问同上：<http://127.0.0.1:18765/>。

---

## 排障（常用）

| 现象 | 处理 |
|------|------|
| `http: server gave HTTP response to HTTPS client` | 按方式一配置 `insecure-registries` 后重启 Docker |
| NAS 仓库连不上 | 改用方式二（ghcr）或方式三（现场构建） |
| 拉不动 ghcr / Docker Hub | 优先方式一（NAS） |
| 端口 18765 被占用 | 改 `deploy/docker-compose.yml` 里 `ports`，或关掉占用进程 |
| Postgres `15432` / Redis `16379` 被占用 | 用 `.env` 覆盖 `MOLMIND_POSTGRES_PORT` / `MOLMIND_REDIS_PORT` |
| `MOLMIND_DATABASE_URL` 连不上 | Compose 内用主机名 `postgres:5432`；宿主机用 `127.0.0.1:15432` |
| `.env` 未生效 | 必须加 `--env-file .env`；仅改 `environment:` 空默认值会覆盖 `env_file` |
| Docker 构建慢 / 拉不动基础镜像 | 检查网络或配置镜像加速；或改用方式一 |
| Apple Silicon 上 arch / manifest 不匹配 | `docker pull --platform linux/amd64 ...` |
| Windows 卷挂载失败 | Docker Desktop → Settings → Resources → File sharing，勾选仓库所在盘 |
| `ModuleNotFoundError: rdkit`（源码模式） | `conda install -c conda-forge rdkit`，或改用 Docker |
| 页面空白 / API 失败 | 确认走的是 `http://127.0.0.1:18765/`，不是本地文件 |
