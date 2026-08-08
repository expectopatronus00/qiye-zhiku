# 企业智库 RAG 问答系统 - 生产镜像 (v1.3 信创适配)
#
# 双架构（x86_64 + arm64）构建说明：
#   python:3.11-slim 官方镜像同时发布 amd64 与 arm64 变体，
#   用 docker buildx 一次构建两架构并推送（见 scripts/build_dual_arch.sh）：
#     docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/qiye-zhiku:latest --push .
#   鲲鹏 920/飞腾 S2500 等 arm64 服务器拉取时自动匹配 arm64 变体。
# TARGETARCH 由 buildx 自动注入（amd64/arm64），可用于架构感知的启动逻辑。
FROM python:3.11-slim

ARG TARGETARCH
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 安装依赖（清华源加速；分层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制代码
COPY . .

# 数据目录（挂载卷时会被覆盖，此处为默认路径兜底）
RUN mkdir -p data/uploads data/vectorstore data/logs data/conversations

# 非 root 运行（生产加固）
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 存活探针（slim 无 curl，用 python 探测）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["python", "main.py"]
