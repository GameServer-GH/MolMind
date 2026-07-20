#!/usr/bin/env bash
# 将 molmind 镜像推送到 GitHub Container Registry
# 用法（仓库根目录）：
#   bash deploy/push-ghcr.sh local            # 推送本机已有 molmind:0.1.0（不重建，绕过 Docker Hub）
#   bash deploy/push-ghcr.sh local amd64      # 同上，仅 amd64
#   bash deploy/push-ghcr.sh                  # buildx 重建并推送 amd64+arm64（需能访问 Docker Hub）
#   bash deploy/push-ghcr.sh amd64            # buildx 仅 amd64
#   bash deploy/push-ghcr.sh arm64            # buildx 仅 arm64
#
# 需要：已登录 ghcr.io（PAT 含 write:packages），或设置 GHCR_TOKEN
#   echo "$GHCR_TOKEN" | docker login ghcr.io -u USERNAME --password-stdin
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ORG="${GHCR_ORG:-gameserver-gh}"
NAME="${GHCR_NAME:-molmind}"
VERSION="${GHCR_VERSION:-0.1.0}"
LOCAL_IMAGE="${LOCAL_IMAGE:-molmind:${VERSION}}"
REGISTRY="ghcr.io/${ORG}/${NAME}"

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  USERNAME="${GHCR_USER:-${GITHUB_USER:-$(whoami)}}"
  echo "==> docker login ghcr.io as ${USERNAME}"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$USERNAME" --password-stdin
fi

push_local() {
  local arch_tag="${1:-amd64}"
  if ! docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
    echo "local image not found: $LOCAL_IMAGE" >&2
    echo "build first: bash deploy/pack-image.sh ${arch_tag}" >&2
    exit 1
  fi
  local image_arch
  image_arch="$(docker image inspect "$LOCAL_IMAGE" --format '{{.Architecture}}')"
  if [[ "$image_arch" != "$arch_tag" && "$arch_tag" != "any" ]]; then
    echo "warning: local image arch=${image_arch}, expected ${arch_tag}" >&2
  fi
  echo "==> push local ${LOCAL_IMAGE} → ${REGISTRY}:${VERSION} (+ ${VERSION}-${arch_tag})"
  docker tag "$LOCAL_IMAGE" "${REGISTRY}:${VERSION}"
  docker tag "$LOCAL_IMAGE" "${REGISTRY}:${VERSION}-${arch_tag}"
  docker tag "$LOCAL_IMAGE" "${REGISTRY}:latest"
  docker push "${REGISTRY}:${VERSION}-${arch_tag}"
  docker push "${REGISTRY}:${VERSION}"
  docker push "${REGISTRY}:latest"
  echo "==> done (local push, single-arch tag :${VERSION})"
  echo "    docker pull ${REGISTRY}:${VERSION}"
  echo "    # arm64 需网络通畅时: bash deploy/push-ghcr.sh arm64"
  echo "    # 或 GitHub Actions 构建后 docker buildx imagetools create 合并 manifest"
}

ARCH_ARG="${1:-multi}"
if [[ "$ARCH_ARG" == "local" ]]; then
  push_local "${2:-amd64}"
  exit 0
fi

case "$ARCH_ARG" in
  multi|all)
    PLATFORMS="linux/amd64,linux/arm64"
    ;;
  x86_64|amd64)
    PLATFORMS="linux/amd64"
    ;;
  aarch64|arm64)
    PLATFORMS="linux/arm64"
    ;;
  *)
    echo "unsupported: $ARCH_ARG (use local|multi|amd64|arm64)" >&2
    exit 1
    ;;
esac

if ! docker buildx inspect molmind-builder >/dev/null 2>&1; then
  echo "==> create buildx builder molmind-builder"
  docker buildx create --name molmind-builder --driver docker-container --use
else
  docker buildx use molmind-builder
fi
docker buildx inspect --bootstrap >/dev/null

echo "==> build+push ${REGISTRY}:${VERSION} (${PLATFORMS})"
echo "    (若 Docker Hub 超时，改用: bash deploy/push-ghcr.sh local)"
docker buildx build \
  --platform "$PLATFORMS" \
  -f deploy/Dockerfile \
  -t "${REGISTRY}:${VERSION}" \
  -t "${REGISTRY}:latest" \
  --label "org.opencontainers.image.source=https://github.com/GameServer-GH/MolMind" \
  --push \
  .

echo "==> done"
echo "    docker pull ${REGISTRY}:${VERSION}"
echo "    # 首次推送后请到 GitHub Packages 将包设为 Public（评委免登录）"
