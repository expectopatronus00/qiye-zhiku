# 企业智库 RAG 问答系统 (Qiye Zhiku)

> 面向央企 AI 场景的私有化知识库问答引擎 —— 数据不出域，知识可追溯

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## 项目背景

国资委已发布 60+ 个央企 AI 高价值场景，覆盖工业制造、智慧能源、医药医疗、交通物流等十大行业。每个场景的核心需求之一：**让海量文档和数据变成可问答的知识**。

本项目是一个轻量级、可私有化部署的 RAG（检索增强生成）知识库系统，专为央企/国企的数据安全需求设计。

## 核心特性

- **Agent 模式（v0.9）**：Function Calling 工具调用 + 多步推理（检索知识库/精读文档全文/知识库统计/当前时间），输入区一键切换，回答下方展示可折叠的"推理过程"（每步工具名/参数/耗时/结果），模型不支持工具调用时自动降级普通 RAG
- **全面 UI 重构（v0.8）**：现代深色科技感设计系统（蓝紫渐变 + 玻璃拟态 + 柔和发光），一键切换浅色主题（记忆偏好 + 跟随系统 + URL 强制）
- **Mini-Markdown 安全渲染（v0.8）**：回答富文本渲染（列表/表格/代码/引用/标题），纯 DOM 构建零注入风险
- **引用溯源交互（v0.8）**：回答中 [n] 引用角标可点击高亮对应来源卡片；来源卡片显示相关度并可展开片段、查看全文
- **文档预览面板（v0.8）**：点击来源卡片"查看原文"按原文顺序预览整份文档，区分块类型（标题/正文/表格/OCR）
- **对话导出（v0.8）**：一键导出当前对话为 Markdown（含问题/回答/来源），UTF-8 直接可读
- **对话深链恢复（v0.8）**：`?conv=<id>` 刷新/分享后直接恢复指定对话
- **内网免登录模式（v0.8）**：`security.auth_enabled: false` 时前端自动跳过登录页直入（公开 `/api/auth/status` 端点自适应）
- **多用户认证（v0.7）**：PBKDF2-HMAC-SHA256 密码哈希（600k 迭代 + 随机盐），不透明令牌登录（24h 有效），连续失败自动锁定 10 分钟
- **知识库权限隔离（v0.7）**：知识库按属主隔离，普通用户仅可见/可用自己创建的库，管理员可见全部；存量知识库启动时自动迁移归属管理员
- **操作审计日志（v0.7）**：登录/登出/上传/删除/问答等关键操作全留痕，仅管理员可查询（支持按用户/动作过滤 + 分页）
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

### 权限管理（v0.7）

系统默认开启认证（`security.auth_enabled: true`）。首次启动自动创建管理员账号，随机初始密码写入 `data/admin_credentials.txt`，请尽快登录并修改：

```bash
# 首次登录流程
1. 打开 http://localhost:8000 → 输入 admin + 文件中的初始密码
2. 登录后点击侧边栏"退出"旁的管理入口，可查看审计日志
3. 管理员在审计面板可见全部用户操作；注册新用户走 API（仅管理员）：
curl -X POST http://localhost:8000/api/auth/register \
  -H "Authorization: Bearer <admin令牌>" \
  -H "Content-Type: application/json" \
  -d '{"username":"zhangsan","password":"pass123","display_name":"张三"}'
```

- **认证**：PBKDF2-HMAC-SHA256（600k 迭代 + 随机盐），令牌 24h 有效，连续 5 次失败锁定 10 分钟（`max_login_attempts: 5`，0 关闭锁定）
- **权限隔离**：知识库登记属主（SQLite），普通用户仅见/可用自己的库；`default` 及历史知识库启动时自动迁移归属管理员；对话/上传/检索全部按库鉴权（403 拒绝越权）
- **审计日志**：登录/登出/注册/改密/建删库/上传/问答全部留痕，`GET /api/audit` 仅管理员可查（按用户/动作过滤 + 分页），前端侧边栏"审计日志"面板可视化
- **降级模式**：内网直连场景可设 `auth_enabled: false`，所有接口免登录（内置 system 管理员）
- 安全说明：密码仅存哈希不存明文；令牌不透明且可登出吊销；DB 为单文件 SQLite（`data/security.db`），备份即含全部用户与审计数据

### 高级 UI（v0.8）

前端全面重构为现代设计系统，无需任何前端构建工具（原生 HTML/CSS/JS）：

- **设计系统**：CSS 变量驱动的深浅双主题（深色默认，`?theme=light` / 右上角 ☀/☾ 一键切换，记忆在 localStorage 并跟随系统偏好）；蓝紫渐变主色、玻璃拟态、柔和发光、自定义滚动条
- **安全 Markdown 渲染**：回答按段落解析（代码块/表格/引用/列表/标题/分隔线），行内支持粗体/斜体/链接/行内代码，全程纯 DOM 构建（无 innerHTML 注入，杜绝 XSS）
- **引用溯源**：回答中的 `[n]` 渲染为可点击角标，点击高亮对应的来源卡片 2 秒；来源卡片显示文档名 + 相关度（重排后分数），点击展开 600 字片段，"查看全文"打开预览面板
- **文档预览**：`GET /api/documents/preview/{collection}/{filename}` 按原文顺序（块序号排序）返回整份文档，标注块类型徽章（标题/正文/表格/OCR），单块超长自动截断
- **对话导出**：侧边栏"导出"按钮把当前对话转成 Markdown 文件下载（含用户问题、助手回答、来源列表），BOM + UTF-8 编码可直接阅读
- **对话深链**：`http://host:8000/?conv=<对话id>` 打开即恢复该对话（含消息渲染），适合刷新/分享场景
- **免登录直入**：`security.auth_enabled: false` 时前端通过公开 `/api/auth/status` 自动跳过登录页，以"系统(免认证)"管理员身份直入——内网部署无需任何前端配置

对话消息新增后实时落盘（`data/conversations/*.json`），服务重启后历史对话与标题完整保留。

### Agent 模式（v0.9）

输入区右下角点击 **⚡ Agent 模式** 开关，进入工具调用模式：模型自主决定调用哪些工具、按什么顺序调用，多步推理后给出最终回答。

```text
用户: 请检索 default 知识库并总结 3b8f99e6_layout_test.pdf 的主要内容
        ↓ Agent 决策
工具: preview_document(filename="3b8f99e6_layout_test.pdf", collection="default")  5ms
        ↓ 结果回填，模型综合推理
回答: 文档总结（监控指标/告警阈值表/通知流程）+ 依据文件名
```

- **内置工具集**：`search_knowledge_base`（检索知识库）、`preview_document`（精读文档全文）、`list_knowledge_bases`（可见库列表）、`knowledge_base_stats`（库统计/文档清单）、`get_current_time`（当前时间）
- **工具注册表**：装饰器注册，从函数签名自动生成 OpenAI 兼容 function schema（依赖注入参数如当前用户不暴露给模型，权限在工具内部二次校验）
- **Agent 循环**：LLM 决策 → 执行工具 → 结果以 `role=tool` 回填 → 再决策，直到给出答案或达到迭代上限（`agent.max_iterations`，默认 6）；工具失败不中断，错误结果回填让模型换参数重试
- **双通道兼容**：Ollama `/api/chat` tools 与 OpenAI `chat/completions` tools 均支持（自动适配 arguments 对象/JSON 字符串格式差异）
- **自动降级**：模型不支持工具调用（或临时故障）时自动降级为普通 RAG 流程，回答附降级提示条，服务不中断
- **推理过程可视化**：回答下方展示可折叠"推理过程"卡片（工具名/参数摘要/耗时/展开查看结果），来源文档以徽章列出；对话落盘含工具步骤，刷新/深链恢复后完整还原
- **审计**：`chat.agent` 动作留痕（含工具调用次数与来源数）

API：`POST /api/chat/agent`（body 同普通对话：`{message, collection_name, conversation_id}`）。

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
│   │   ├── agent.py        # Agent 执行引擎（工具调用循环 + 多步推理，v0.9）
│   │   ├── config.py       # 配置管理
│   │   ├── conversation.py # 对话历史管理
│   │   ├── document.py     # 文档处理
│   │   ├── embeddings.py   # 向量嵌入
│   │   ├── evaluator.py    # RAGAS 评估（忠实度/相关性/召回率，本地裁判）
│   │   ├── llm.py          # LLM 调用（含 Function Calling / Tools API）
│   │   ├── reranker.py     # 检索结果重排序（cross-encoder / 启发式）
│   │   ├── retriever.py    # 检索引擎
│   │   ├── tools.py        # Agent 工具注册表 + 内置工具集（v0.9）
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
| 2026-08-05 | v0.7 | 权限管理：多用户认证（PBKDF2 哈希 + 令牌 + 失败锁定）、知识库属主隔离（存量库自动迁移）、操作审计日志（admin 专属可视化面板）、登录页/权限前端，安全模块单元测试 24 项，e2e 权限链路 16 项全过 |
| 2026-08-05 | v0.8 | 高级 UI：全面重构设计系统（深色默认 + 浅色切换 + 渐变/玻璃拟态/发光）、Mini-Markdown 安全渲染、来源卡片 + 引用 [n] 点击高亮、文档预览面板（按原文顺序 + 块类型徽章）、对话导出 Markdown、`?conv=` 深链恢复、内网免登录直入（`/api/auth/status` 自适应）、对话消息实时落盘（重启不丢），全量单元测试 84 项 + e2e 16 项全过，截图视觉验证（深浅主题/登录页/欢迎页/对话视图） |
| 2026-08-07 | v0.9 | Agent 模式：Function Calling 双通道（Ollama tools / OpenAI tools 自动适配 arguments 格式差异）、工具注册表（装饰器 + 签名自动生成 schema，依赖注入不暴露给模型）、内置 5 工具（检索/文档精读/库列表/库统计/时间）、Agent 多步推理循环（失败重试 + 迭代上限 + 上限强制总结）、前端 ⚡ Agent 模式开关 + 可折叠推理过程可视化（工具名/参数/耗时/结果 + 来源文件徽章）、模型不支持工具调用自动降级普通 RAG、对话落盘含工具步骤（深链恢复还原），修复 Ollama 回填消息 400 与 `can_access(None)` 崩溃，全量单元测试 102 项全过，真机验证工具调用全链路 + 截图验证折叠/展开态 |
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
- **前端**: 原生 HTML + CSS 变量设计系统（深/浅双主题）+ 原生 JS（零构建依赖）
- **部署**: Docker / 直接部署

## License

MIT
