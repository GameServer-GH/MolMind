# MolMind 镜像说明

完整部署步骤见 [`../README.md`](../README.md)。

## 推荐顺序（国内）

1. **NAS 临时仓库**（快）：`8.133.197.65:5001/molmind:0.1.0`  
2. **ghcr.io**（外网备选，国内可能很慢）

## NAS 仓库拉取

Docker Engine 需配置 `"insecure-registries": ["8.133.197.65:5001"]`。

```bash
docker pull --platform linux/amd64 8.133.197.65:5001/molmind:0.1.0
docker tag 8.133.197.65:5001/molmind:0.1.0 molmind:0.1.0
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```

## 从 ghcr 拉取（备选）

```bash
docker pull ghcr.io/gameserver-gh/molmind:0.1.0
docker tag ghcr.io/gameserver-gh/molmind:0.1.0 molmind:0.1.0
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```
