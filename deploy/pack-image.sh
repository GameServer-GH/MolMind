#!/usr/bin/env bash
# 构建 molmind:0.2.0 并导出到 deploy/images/
# 用法（仓库根目录）：
#   bash deploy/pack-image.sh           # 默认 linux/amd64
#   bash deploy/pack-image.sh amd64     # Windows/Linux Intel/AMD
#   bash deploy/pack-image.sh arm64     # Apple Silicon
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE="molmind:0.2.0"
ARCH="${1:-amd64}"
case "$ARCH" in
  x86_64|amd64) PLATFORM="linux/amd64"; ARCH_TAG="amd64" ;;
  aarch64|arm64) PLATFORM="linux/arm64"; ARCH_TAG="arm64" ;;
  *)
    echo "unsupported arch: $ARCH (use amd64 or arm64)" >&2
    exit 1
    ;;
esac

OUT_DIR="$ROOT/deploy/images"
OUT_TAR="$OUT_DIR/molmind-0.2.0-${ARCH_TAG}.tar"
mkdir -p "$OUT_DIR"

echo "==> build $IMAGE ($PLATFORM)"
docker buildx build \
  --platform "$PLATFORM" \
  -f deploy/Dockerfile \
  -t "$IMAGE" \
  --load \
  .

echo "==> save $OUT_TAR"
docker save -o "$OUT_TAR" "$IMAGE"
ls -lh "$OUT_TAR"
echo "==> done. Load: docker load -i deploy/images/$(basename "$OUT_TAR")"
echo "==> run:  docker compose -f deploy/docker-compose.yml up -d"
