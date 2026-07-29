# MolMind 镜像说明

完整部署步骤见 [`../README.md`](../README.md)。

## Git 提交约定

`deploy/images/` 只用于本地存放导出或下载的 Docker 镜像包。以下大文件及构建临时文件不提交到 Git：

- `*.tar`
- `*.tar.gz`
- `*.tgz`
- `.tmp-*`

仓库只保留 `.gitkeep` 和本说明文件。提交前可使用 `git status --short -- deploy/images` 检查是否有临时文件误入暂存区。

## 推荐顺序（国内）

1. **NAS 临时仓库**（快）：`8.133.197.65:5001/molmind:0.2.1`
2. **ghcr.io**（外网备选，国内可能很慢）

## NAS 仓库拉取

Docker Engine 需配置 `"insecure-registries": ["8.133.197.65:5001"]`。

```bash
docker pull --platform linux/amd64 8.133.197.65:5001/molmind:0.2.1
docker tag 8.133.197.65:5001/molmind:0.2.1 molmind:0.2.1
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```

## 从 ghcr 拉取（备选）

```bash
docker pull ghcr.io/gameserver-gh/molmind:0.2.1
docker tag ghcr.io/gameserver-gh/molmind:0.2.1 molmind:0.2.1
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```
