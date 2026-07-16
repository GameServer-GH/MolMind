# MolMind 部署与启动指南

命令默认在**仓库根目录**执行。  
- **本地**：本目录 `deploy/`（Docker Compose，无 nginx）  
- **服务器 / 域名**：`deploy_pro/`（nginx + HTTPS，**不提交 git**，见该目录内 README）

工程状态与镜像体积决策见 [`../docs/reports/molmind_optimization_status_v9.md`](../docs/reports/molmind_optimization_status_v9.md)、[`../docs/reports/molmind_model_size_and_deployment_assessment.md`](../docs/reports/molmind_model_size_and_deployment_assessment.md)。

---

## 环境要求

| 项 | 说明 |
|----|------|
| Docker Desktop / Engine | 本地与服务器推荐用 Compose 启动 |
| 可选：Python 3.9+ | 仅不用 Docker、直接跑源码时需要 |
| RDKit | Docker 镜像已内置；源码安装失败可用 conda-forge |

---

## 本地 Docker 部署（推荐）

先安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（macOS / Windows）或 Docker Engine（Linux），并确保 daemon 已启动。

### macOS / Linux

```bash
cd /path/to/MolMind
mkdir -p output
docker compose -f deploy/docker-compose.yml up --build
```

后台运行加 `-d`。停止：

```bash
docker compose -f deploy/docker-compose.yml down
```

### Windows（PowerShell）

在仓库根目录打开 PowerShell（建议已安装 Docker Desktop）：

```powershell
cd C:\path\to\MolMind
New-Item -ItemType Directory -Force -Path output | Out-Null
docker compose -f deploy/docker-compose.yml up --build
```

后台：

```powershell
docker compose -f deploy/docker-compose.yml up --build -d
```

停止：

```powershell
docker compose -f deploy/docker-compose.yml down
```

> Windows 若提示找不到 `docker`，先打开 Docker Desktop，并确认 Settings → General 中启用了 Docker Compose。

### 访问

浏览器打开：<http://127.0.0.1:18765/>  
健康检查：<http://127.0.0.1:18765/health>

请勿用 `file://` 打开 HTML；须通过上述地址访问。

### 可选：离线 CLI 冒烟

```bash
# macOS / Linux / Windows（Git Bash / PowerShell 均可）
docker compose -f deploy/docker-compose.yml run --rm cli
```

结果写入宿主机 `./output/nomination_top10.csv`。

---

## 可选：本机 Python 直接跑（不用 Docker）

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
| 端口 18765 被占用 | 改 `deploy/docker-compose.yml` 里 `ports`，或关掉占用进程 |
| Docker 构建慢 / 拉不动基础镜像 | 检查网络或配置镜像加速 |
| Apple Silicon 上 `PackagesNotFoundError: rdkit=…` | 镜像固定 `rdkit=2025.09.*` 发布线；若目标架构无该发布线，使用 `--platform linux/amd64` 构建并在报告中记录平台 |
| Windows 卷挂载失败 | Docker Desktop → Settings → Resources → File sharing，勾选仓库所在盘 |
| `ModuleNotFoundError: rdkit`（源码模式） | `conda install -c conda-forge rdkit`，或改用 Docker |
| 页面空白 / API 失败 | 确认走的是 `http://127.0.0.1:18765/`，不是本地文件 |
