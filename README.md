# 企业智库 RAG 问答系统 (Qiye Zhiku)

> 面向央企 AI 场景的私有化知识库问答引擎 —— 数据不出域，知识可追溯

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## 项目背景

国资委已发布 60+ 个央企 AI 高价值场景，覆盖工业制造、智慧能源、医药医疗、交通物流等十大行业。每个场景的核心需求之一：**让海量文档和数据变成可问答的知识**。

本项目是一个轻量级、可私有化部署的 RAG（检索增强生成）知识库系统，专为央企/国企的数据安全需求设计。

## 核心特性

- **多轮对话**：支持上下文关联的连续对话，自动管理对话历史，可切换/删除/新建会话
- **Query 改写**：多轮追问自动补全上下文（"那部署方式呢"→完整独立查询），显著提升追问检索准确率
- **检索重排**：cross-encoder 语义精排（bge-reranker-base），模型缺失时自动降级启发式精排
- **PDF 版面分析**：标题/正文按字号识别（带 bbox 定位），表格自动转 Markdown，图片 OCR 提取文字入库
- **中文分词检索**：BM25 基于 jieba 中文分词，解决中文关键词检索失效问题
- **相似度过滤**：低质量检索结果按阈值自动过滤
- **数据不出域**：支持 Ollama 本地大模型部署，全程无需联网调用外部 API
- **多格式文档**：支持 PDF、Word、Excel、Markdown、TXT 等格式导入
- **智能分块**：自适应文档分块策略，保留上下文完整性
- **混合检索**：向量语义检索 + BM25 关键词检索，双路召回
- **知识溯源**：每个回答都标注来源文档和具体段落
- **多知识库**：支持创建多个独立知识库，按项目/部门隔离
- **Web 界面**：开箱即用的对话式交互界面，含对话列表管理
- **API 优先**：RESTful API 设计，方便集成到现有系统

## 快速开始

### 环境要求

- Python 3.10+
- （可选）Ollama —— 用于本地大模型推理

### 安装

```bash
# 克隆仓库
git clone https://github.com/your-org/qiye-zhiku.git
cd qiye-zhiku

# 创建虚拟环境
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 配置

```bash
# 复制配置文件
cp config.example.yaml config.yaml

# 编辑配置（选择 LLM 后端、向量数据库等）
# 详见 config.example.yaml 中的注释
```

### 启动

```bash
# 启动 Web 服务
python main.py

# 访问 http://localhost:8000
```

### 使用 Ollama 本地模型（推荐）

```bash
# 安装 Ollama: https://ollama.com
# 下载模型
ollama pull qwen2.5:7b        # 通义千问（中文优秀）
ollama pull nomic-embed-text   # 嵌入模型

# config.yaml 中配置:
# llm:
#   provider: ollama
#   model: qwen2.5:7b
# embedding:
#   provider: ollama
#   model: nomic-embed-text
```

### 使用重排序模型（可选，Day 4）

```bash
# 下载 bge-reranker-base（ModelScope，国内源）
git clone https://www.modelscope.cn/models/BAAI/bge-reranker-base.git <本地目录>

# config.yaml 中配置:
# reranker:
#   enabled: true
#   type: "cross_encoder"   # cross_encoder | heuristic | none
#   model_path: "<本地目录>"
# 未配置模型时自动降级为启发式精排（heuristic），服务不中断
```

### 文档增强（v0.5，默认开启）

PDF 解析默认启用版面分析 + 表格识别 + 图片 OCR（PyMuPDF + RapidOCR，均为本地推理）：

```yaml
# document 配置段
heading_min_size: 13.0        # 字号 ≥ 13pt 的文本块判定为标题
table_to_markdown: true       # 表格识别并转 Markdown 入库（含 bbox）
ocr_enabled: true             # 内嵌图片 OCR，结果带所在章节标题上下文
ocr_max_images_per_page: 3    # 每页最多 OCR 3 张图（控制耗时）
ocr_min_area: 8000            # 小于该面积的图片跳过（过滤小图标）
```

每个 PDF 块携带 `block_type`（heading/body/table/ocr）与 `bbox` 元数据，可支撑前端定位高亮。OCR 依赖 `rapidocr_onnxruntime`（模型随包分发，无需联网下载）。

### 质量评估（v0.6，可选）

内置 RAGAS 方法论的三大指标评估，裁判与嵌入全程使用本地模型（默认 qwen2.5:7b + nomic-embed-text），数据不出域：

| 指标 | 含义 | 评估方式 |
|------|------|----------|
| 忠实度 faithfulness | 回答是否忠于检索到的资料 | 回答拆陈述 → 逐条判定是否有上下文依据 |
| 答案相关性 answer_relevancy | 回答是否切题 | 由回答反向生成问题 → 与原问题向量相似度 |
| 上下文召回率 context_recall | 检索是否覆盖黄金答案 | 黄金答案拆陈述 → 逐条判定是否在检索结果中 |

```bash
# 构建黄金评测集 eval/dataset.json（含问题 + 黄金答案 + 来源文档）
# 运行评估（检索 → 回答 → 三项指标 → 汇总报告）
python scripts/run_eval.py

# 快速验证前 3 题 / 指定知识库 / 覆盖门禁阈值
python scripts/run_eval.py --limit 3
python scripts/run_eval.py --collection my_kb
python scripts/run_eval.py --min-faithfulness 0.6
```

- 报告输出至 `data/eval_reports/report_<时间戳>.json`，含每题得分与判定明细（便于定位失败环节）
- **质量门禁**：任一指标低于 `config.yaml` 中 `eval.min_*` 阈值时退出码为 1，可直接接入 CI
- 裁判输出要求 JSON，解析失败自动降级文本规则解析，仍无法判定时跳过该条（不计入分母），评估流程不中断
- **数值字面预检**：陈述含数值事实（阈值/区间/功率等）时先做确定性字面校验（自动统一 "大于85℃"/">85C" 等表述），命中即判"是"——比 7B 裁判对多行表格的判定更稳定；语义事实仍由 LLM 裁判
- 已知局限：7B 裁判对个别语义事实（如表格"级别"列）仍有漏判，报告含逐条判定明细，可人工审计

## 项目结构

```
qiye-zhiku/
├── main.py                 # 应用入口
├── config.yaml             # 运行配置
├── config.example.yaml     # 配置模板
├── requirements.txt        # Python 依赖
├── ROADMAP.md              # 迭代路线图
├── app/
│   ├── core/               # 核心模块
│   │   ├── config.py       # 配置管理
│   │   ├── conversation.py # 对话历史管理
│   │   ├── document.py     # 文档处理
│   │   ├── embeddings.py   # 向量嵌入
│   │   ├── evaluator.py    # RAGAS 评估（忠实度/相关性/召回率，本地裁判）
│   │   ├── llm.py          # LLM 调用
│   │   ├── reranker.py     # 检索结果重排序（cross-encoder / 启发式）
│   │   ├── retriever.py    # 检索引擎
│   │   └── vectorstore.py  # 向量存储
│   ├── routers/            # API 路由
│   │   ├── chat.py         # 对话接口（含多轮对话）
│   │   ├── documents.py    # 文档管理
│   │   └── knowledge.py    # 知识库管理
│   └── static/             # 前端界面
│       ├── index.html
│       ├── css/
│       └── js/
├── data/                   # 数据存储（gitignore）
├── tests/                  # 测试
└── docs/                   # 文档
```

## 每日迭代日志

| 日期 | 版本 | 内容 |
|------|------|------|
| 2026-08-01 | v0.1 | 项目骨架：FastAPI + ChromaDB + 基础 RAG + Web UI |
| 2026-08-02 | v0.2 | 多轮对话：对话历史管理、会话持久化、上下文窗口、对话列表 UI |
| 2026-08-03 | v0.3 | Query 改写（追问补全）+ BM25 中文分词 + 相似度阈值过滤 + 单元测试 |
| 2026-08-03 | v0.3.1 | 本地全链路验证：qwen2.5:7b 导入 Ollama，文档上传→混合检索→LLM 回答→多轮追问改写全流程实测通过 |
| 2026-08-04 | v0.4 | 检索重排：bge-reranker-base cross-encoder 语义精排 + 启发式降级 + Reranker 单元测试 |
| 2026-08-04 | v0.5 | 文档增强：PDF 版面分析（标题/正文按字号识别+bbox）+ 表格转 Markdown + 图片 OCR（RapidOCR，带章节上下文），解析器单元测试 13 项 |
| 2026-08-05 | v0.6 | 评估体系：RAGAS 方法论三大指标（忠实度/答案相关性/上下文召回率），本地裁判（qwen2.5:7b，温度 0）全离线评估 + 黄金评测集 + 质量门禁（可接 CI）+ 数值字面预检（归一化+确定性校验，忠实度 0.81→1.00、召回率 0.85→0.96），评估器单元测试 29 项 |
| ... | ... | 持续迭代中，详见 [ROADMAP.md](ROADMAP.md) |

## 适用场景

- 央企/国企内部知识库建设
- 政策法规智能问答
- 技术文档检索与问答
- 项目经验知识沉淀
- 培训考核辅助系统

## 技术栈

- **后端**: FastAPI + ChromaDB
- **LLM**: Ollama (本地) / OpenAI API (云端)
- **嵌入**: nomic-embed-text / text2vec-chinese
- **前端**: 原生 HTML + TailwindCSS
- **部署**: Docker / 直接部署

## License

MIT
