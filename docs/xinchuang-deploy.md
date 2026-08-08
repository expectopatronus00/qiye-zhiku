# 信创环境部署手册 (v1.3)

> 适用系统：麒麟 V10（银河麒麟/中标麒麟，x86_64 + 鲲鹏 arm64）、统信 UOS 1060（x86_64 + 飞腾 arm64）。
> 覆盖两种部署形态：**二进制部署**（无容器环境）与 **Docker 部署**（含双架构镜像）。

## 一、架构与组件矩阵

| 组件 | 本系统角色 | 信创替换项 | 部署方式 |
|------|-----------|-----------|---------|
| 应用服务 | FastAPI + ChromaDB/Milvus | 无（纯 Python，双架构兼容） | 源码/systemd 或 Docker |
| 大模型推理 | Ollama / OpenAI 兼容 | 昇腾 CANN：vLLM-Ascend；寒武纪 MLU：vLLM/MLU-Transformers；摩尔线程：MUSA 生态 | provider 配置切换 |
| 嵌入模型 | nomic-embed-text | bge-m3 等（国产栈 OpenAI 兼容 /embeddings） | provider 配置切换 |
| 向量库 | ChromaDB | Milvus（国产化部署支持） | 独立服务/容器 |
| 操作系统 | 任意 | 麒麟 V10 SP1/SP2 / 统信 UOS 1060 | 本手册 |

## 二、快速选型

| 场景 | 推荐组合 |
|------|---------|
| 一般私有化（无国产 GPU 硬性要求） | 鲲鹏/飞腾 arm64 + 二进制部署 + Ollama(ARM) + ChromaDB |
| 信创招标（国产 GPU 硬性要求） | 昇腾 910B + vLLM-Ascend（OpenAI 兼容）+ Milvus |
| 已有 GPU 服务器（x86_64） | Docker 部署 + NVIDIA/国产卡均可 |

## 三、二进制部署（麒麟 V10 / 统信 UOS）

### 1. 系统准备

```bash
# 麒麟 V10 / 统信 UOS 通用（root 或 sudo）
# Python 3.11（麒麟 V10 SP1 自带 3.8，需源码编译或使用软件源高版本）
sudo yum install -y gcc gcc-c++ make zlib-devel bzip2-devel openssl-devel \
  libffi-devel readline-devel sqlite-devel || sudo apt install -y build-essential libssl-dev
```

无 Python 3.11 时源码编译（麒麟 x86_64/鲲鹏 arm64 通用）：

```bash
wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
tar xzf Python-3.11.9.tgz && cd Python-3.11.9
./configure --enable-optimizations --prefix=/usr/local/python311
make -j$(nproc) && sudo make install
# 建议配置国内 pip 源（内网环境可用公司源）
/usr/local/python311/bin/pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 部署应用

```bash
cd /opt
git clone <私有仓库地址> qiye-zhiku && cd qiye-zhiku   # 或上传打包好的源码包
/usr/local/python311/bin/pip3 install -r requirements.txt

# 生产配置（Ollama 服务地址按实际修改）
cp config.example.yaml config.yaml
```

### 3. 部署 Ollama（ARM64 版）

```bash
# 鲲鹏/飞腾 arm64：官方提供 aarch64 版本
curl -fsSL https://ollama.com/install.sh | sh
# 拉取模型（信创环境可用 ModelScope 离线包导入，见《本地大模型部署》文档）
ollama pull qwen2.5:7b
ollama pull nomic-embed-text
```

### 4. 配置国产 GPU 推理（昇腾 CANN 示例）

```bash
# 1) 推理侧：昇腾服务器上用 vLLM-Ascend 起 OpenAI 兼容服务
#    docker run --device=/dev/davinci0 ... -p 8000:8000 \
#      vllm/vllm-ascend --model Qwen/Qwen2.5-7B-Instruct --served-model-name qwen2.5:7b
# 2) 本系统 config.yaml 指向该服务（OpenAI 兼容协议）
```

```yaml
llm:
  provider: ascend          # ascend | cambricon | mthreads | openai | ollama
  model: qwen2.5:7b
  ascend_base_url: http://192.168.1.10:8000/v1
  # 内网无鉴权时 api_key 留空即可（系统自动省略 Authorization 头）
embedding:
  provider: ascend
  model: bge-m3
  ascend_base_url: http://192.168.1.10:8000/v1
vectorstore:
  type: milvus              # 切换 Milvus（详见第四节）
  milvus_uri: http://192.168.1.20:19530
```

> 寒武纪（provider=cambricon / cambricon_base_url）、摩尔线程（provider=mthreads / mthreads_base_url）同理，只要推理服务暴露 OpenAI 兼容端点。

### 5. systemd 托管

```ini
# /etc/systemd/system/qiye-zhiku.service
[Unit]
Description=Qiye Zhiku RAG Service
After=network.target ollama.service

[Service]
Type=simple
User=appuser
WorkingDirectory=/opt/qiye-zhiku
ExecStart=/usr/local/python311/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now qiye-zhiku
curl http://127.0.0.1:8000/healthz && curl http://127.0.0.1:8000/readyz
```

### 6. 防火墙放行

```bash
# 麒麟/统信自带 firewalld
sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload
```

## 四、Milvus 向量库（信创/大规模场景）

Milvus 社区版支持国产化部署（Docker Compose 编排 etcd + MinIO + Milvus）：

```bash
# 下载 Milvus standalone 编排（x86_64 / arm64 均有镜像）
wget https://github.com/milvus-io/milvus/releases/download/v2.4.x/milvus-standalone-docker-compose.yml
docker compose -f milvus-standalone-docker-compose.yml up -d
# 验证
docker exec -it milvus-standalone curl -s http://127.0.0.1:19530/healthz
```

应用侧切换（config.yaml）：

```yaml
vectorstore:
  type: milvus
  dimension: 768              # 与嵌入模型维度一致（bge-m3=1024，nomic=768）
  milvus_uri: http://127.0.0.1:19530
  # milvus_token: "user:password"   # Milvus 开启鉴权时填写
```

切换后重启服务即可，**业务代码无感知**（统一走 get_vector_store 工厂）。存量 Chroma 数据不自动迁移，上线前需重新上传/索引文档。

## 五、Docker 部署（双架构镜像）

```bash
# 在构建机（x86_64）上一次性构建并推送双架构镜像（脚本见 scripts/build_dual_arch.sh）
IMAGE=registry.example.com/qiye-zhiku ./scripts/build_dual_arch.sh

# 信创服务器（鲲鹏/飞腾 arm64）拉取 arm64 变体
docker compose pull
docker compose up -d
```

验证镜像平台清单：

```bash
docker buildx imagetools inspect registry.example.com/qiye-zhiku:latest
# 输出应包含 amd64 与 arm64 两条 Platform
```

## 六、验收检查清单

| # | 检查项 | 命令/位置 |
|---|--------|----------|
| 1 | 服务存活 | `curl /healthz` 返回 200 |
| 2 | 依赖就绪 | `curl /readyz` status=ready/degraded，vectorstore/database ok |
| 3 | 国产推理连通 | readyz llm 段 ok；或 `curl <ascend_base_url>/models` |
| 4 | 问答链路 | 前端提问一次（含检索+回答） |
| 5 | 向量库写入 | 上传 1 份测试文档，`curl /api/knowledge` 统计文档数增加 |
| 6 | 双架构 | `docker buildx imagetools inspect` 含 amd64/arm64 |

## 七、常见问题

- **pip 安装慢/失败**：内网环境配置公司 pip 源；离线环境用 `pip download -r requirements.txt -d pkg/` 后 `pip install --no-index --find-links pkg/`。
- **Readyz degraded（LLM 段失败）**：确认推理服务地址可达；国产 provider 未配置 base_url 时会回退 openai_base_url，注意检查配置。
- **arm64 无 Docker 镜像**：Ollama 官方镜像支持 arm64；本系统镜像用 build_dual_arch.sh 双架构构建。
- **Milvus 中文检索效果**：Milvus 只管向量检索，中文分词由本系统 BM25（jieba）完成，混合检索行为与 Chroma 一致。
- **麒麟系统 OpenSSL 版本旧**：Python 3.11 编译时提示 SSL 模块问题时，升级 openssl-devel 或用系统包管理器安装 python3.11（麒麟 V10 SP2+ 软件源已提供）。
