# MolMind 评委镜像交付

本目录可放本地 `docker save` 的 tar（**不入库**）。推荐评委从 **GitHub Container Registry** 拉取多架构镜像，无需下载 3GB tar。

## 推荐：从 ghcr.io 拉取（多架构）

镜像地址：

```text
ghcr.io/gameserver-gh/molmind:0.1.0
```

同一 tag 下同时包含：

| 架构 | 适用环境 |
|------|----------|
| `linux/amd64` | Windows / Linux（Intel / AMD） |
| `linux/arm64` | Apple Silicon（M1/M2/M3…） |

Docker 会按本机自动选层：

```bash
docker pull ghcr.io/gameserver-gh/molmind:0.1.0
docker tag ghcr.io/gameserver-gh/molmind:0.1.0 molmind:0.1.0
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```

浏览器：<http://127.0.0.1:18765/>

```bash
curl http://127.0.0.1:18765/health
```

> 若包为 Public，无需登录。若为 Private，需有仓库/包读权限并先 `docker login ghcr.io`。

## 维护者：推送到 ghcr.io

```bash
# 1) 登录（PAT 需 write:packages）
export GHCR_TOKEN=ghp_xxxxxxxx
echo "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# 2) 推荐：推送本机已构建镜像（不重建，不依赖 Docker Hub）
bash deploy/push-ghcr.sh local

# 3) 网络通畅时再 buildx 多架构（会拉 continuumio/miniconda3）
bash deploy/push-ghcr.sh          # amd64 + arm64
bash deploy/push-ghcr.sh amd64
bash deploy/push-ghcr.sh arm64
```

若 `buildx` 报 `auth.docker.io ... i/o timeout`，用 `local` 模式即可先交付 amd64。

本机 `docker push` 若在 **2GB 大层** 处反复出现 `Bad request / Whoa there!`，是上传 ghcr 时网络中断或单 blob 过大，**不要反复重试**。改走 GitHub Actions（推荐）：

1. 将本仓库代码 push 到 GitHub  
2. **Actions** → **Publish MolMind image to GHCR** → **Run workflow**（tag 填 `0.1.0`）  
3. 在 GitHub 云端构建并推送，与 ghcr 同网，比本机 3GB 上传稳定

若包是先用 PAT 手动推的，Actions 可能无写权限：到 Packages → `molmind` → **Connect repository** 关联本仓库，或删除包后仅由 Actions 首次推送。

首次推送后到 GitHub → Packages → `molmind` → Package settings → **Change visibility → Public**，评委即可免登录拉取。

## 可选：本地 tar（断网 / 无法访问 ghcr）

```bash
bash deploy/pack-image.sh amd64   # → deploy/images/molmind-0.1.0-amd64.tar
bash deploy/pack-image.sh arm64   # → deploy/images/molmind-0.1.0-arm64.tar
```

```bash
docker load -i deploy/images/molmind-0.1.0-amd64.tar   # 按机器选对应文件
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```
