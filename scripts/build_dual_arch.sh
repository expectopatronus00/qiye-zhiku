#!/usr/bin/env bash
# 企业智库 RAG 系统 - x86_64 + arm64 双架构镜像构建推送脚本 (v1.3 信创适配)
#
# 用法:
#   IMAGE=registry.example.com/qiye-zhiku ./scripts/build_dual_arch.sh
#   IMAGE=registry.example.com/qiye-zhiku TAG=v1.3.0 ./scripts/build_dual_arch.sh
#
# 前置条件:
#   1. Docker 19.03+（含 buildx 插件），并已登录镜像仓库（docker login）
#   2. 推送需 registry 支持多架构 manifest（标准 Harbor / Docker Hub 均可）
#   3. 本机无 QEMU 模拟时仅 x86_64 可本地跑；arm64 变体由 buildx 交叉构建
set -euo pipefail

IMAGE="${IMAGE:-qiye-zhiku}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER="${BUILDER:-qiye-zhiku-builder}"

echo "[1/3] 检查 buildx"
docker buildx version >/dev/null 2>&1 || { echo "错误: 需要 Docker buildx（Docker 19.03+）"; exit 1; }

echo "[2/3] 准备构建器: ${BUILDER}"
if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
  docker buildx create --name "${BUILDER}" --use
else
  docker buildx use "${BUILDER}"
fi
docker buildx inspect --bootstrap

echo "[3/3] 构建并推送: ${IMAGE}:${TAG} @ ${PLATFORMS}"
docker buildx build \
  --platform "${PLATFORMS}" \
  --tag "${IMAGE}:${TAG}" \
  --push \
  .

echo "完成: ${IMAGE}:${TAG}"
echo "验证: docker buildx imagetools inspect ${IMAGE}:${TAG}"
