# AGENTS.md — 企业智库 RAG 系统 上手手册

> 给 AI Agent 的项目说明书：架构地图、运行命令、已知坑、迭代工作流。
> 换任何 agent 工具，先读本文件即可快速接手。人看的文档见 README.md。

## 项目是什么

面向央企 AI 场景的私有化 RAG 知识库问答系统（FastAPI + ChromaDB/Milvus + Ollama qwen2.5:7b + nomic-embed-text + bge-reranker-base）。当前版本 v1.3（信创适配），路线见 ROADMAP.md，迭代进度按"每日一版"节奏推进（v0.1~v1.3 已完成）。

技术栈：Python 3.11 / FastAPI / ChromaDB / Milvus(可选) / SQLite / jieba / PyMuPDF / RapidOCR / pytest。

## 快速启动（本机开发环境）

```bash
# 注意：系统 PATH 的 python 是 WindowsApps stub（运行直接 exit 49），不可用！
# 必须用完整 Python：
PY="C:/Users/18821/Python312/python.exe"

# 启动服务（端口 8766）
cd qiye-zhiku && $PY -m uvicorn main:app --host 127.0.0.1 --port 8766

# 全量测试（pytest.ini 已配 asyncio_mode=auto）
$PY -m pytest tests/ -q
```

运行依赖：本机 Ollama（qwen2.5:7b + nomic-embed-text，端口 11434）、ChromaDB（本地持久化 data/vectorstore）。服务地址 http://127.0.0.1:8766。

Docker 部署：`docker compose up --build`（app + ollama 编排、非 root、健康依赖启动）；容器部署必须把 config.yaml 的 `llm.ollama_base_url` 配为 `http://ollama:11434`。

## 架构地图

```
main.py            入口：路由挂载 + 日志中间件 + 启动引导（管理员初始化/存量知识库迁移）
app/core/          核心逻辑
  config.py        config.yaml 加载（pydantic 各配置块）；v1.3 国产 provider 白名单 VALID_LLM_PROVIDERS/validate_provider + 向量库类型校验
  llm.py           LLM 统一接口（ollama / openai 兼容多 provider：openai+ascend+cambricon+mthreads 走 OPENAI_COMPAT_PROVIDERS，resolve_openai_base_url 按 provider 路由；chat_stream SSE 流式）
  embeddings.py    嵌入（ollama / openai 兼容多 provider，resolve_embedding_base_url 路由；注意 ollama 通道走 llm.ollama_base_url）
  vectorstore.py   向量库（BaseVectorStore 协议 + ChromaVectorStore + MilvusVectorStore；get_vector_store 工厂按 vectorstore.type 路由，旧 VectorStore 类名保留为 Chroma 别名）
  retriever.py     检索（向量语义 + BM25 关键词双路召回，jieba 分词；标准 RRF 融合 k=60，last_debug 诊断数据）
  reranker.py      重排（bge-reranker-base cross-encoder，模型缺失自动降级启发式）
  query_rewriter.py 多轮追问 Query 改写（补全指代）
  agent.py         Agent 循环（工具调用多步推理，失败重试上限 6 + 超限强制总结）
  tools.py         工具注册表（装饰器 + 函数签名自动生成 schema + 依赖注入不暴露给模型）
  document.py      文档解析（PDF 版面按字号分标题/正文、表格转 Markdown、RapidOCR 图片 OCR）
  conversation.py  对话历史管理（上下文窗口、持久化）
  evaluator.py     RAGAS 评估（忠实度/相关性/召回率，本地裁判）
  security.py      认证（PBKDF2 哈希、令牌 24h、失败锁定 10min）、知识库权限隔离、审计日志、用户反馈 FeedbackManager（feedback 表落库 + 回流评测集）
  logging_setup.py 日志（app.log/access.log 轮转 5MB×5 + 请求中间件带 user/duration）
app/routers/       API 层（prefix 各自带 /api）
  auth.py          认证（/api/auth）
  chat.py          对话（/api/chat，含 POST /stream SSE 流式 + Agent 模式 + POST /feedback 用户反馈）
  documents.py     文档（/api/documents，上传/列表/预览）
  knowledge.py     知识库（/api/knowledge，创建/查询/统计）
  audit.py         审计（/api/audit，仅管理员；GET /export CSV 带 BOM）
  admin.py         管理后台（/api/admin：用户管理/知识库配额/系统配置热更新/反馈列表与回流导出，仅管理员）
  health.py        健康检查（/healthz 存活 + /readyz 就绪探测：向量库/DB/LLM 三段判定；LLM 探测按 provider 分支，国产走 /models）
app/static/index.html  前端单页（原生 JS，深色设计系统 + 浅色主题，无构建步骤）
eval/run_regression.py  黄金评测集一键回归（hit@5/MRR/top1，混合 vs 纯向量，基线对比；--collect-feedback 合并回流）
scripts/build_dual_arch.sh  x86_64+arm64 双架构镜像构建推送（buildx）
docs/xinchuang-deploy.md    麒麟 V10 / 统信 UOS 信创部署手册（二进制+Docker 双形态）
tests/             pytest 测试（176 项，按模块拆分 test_*.py）
```

## 常用命令

```bash
# 单文件测试
$PY -m pytest tests/test_agent.py -q
# 真机验证健康端点
curl http://127.0.0.1:8766/healthz && curl http://127.0.0.1:8766/readyz
# 免登录模式（临时改 config.yaml security.auth_enabled=false 后重启，验证完必须恢复！）
```

## 已知坑（换工具必读，全是踩过的）

1. 系统 PATH 的 `python` 是 WindowsApps stub（exit 49），必须用 `C:/Users/18821/Python312/python.exe`
2. Bash 工具偶发报"output file could not be read"：脚本不要打印到 stdout，直接写文件到工作区路径再 Read
3. **Ollama 回填 assistant 消息 `tool_calls.arguments` 必须是 dict 对象**；OpenAI 要求 JSON 字符串——agent.py 按 provider 分支，改错返回 400
4. `get_current_user` 签名带 `request: Request` 参数（注入 `request.state.username` 供请求日志中间件），依赖它的测试传 SimpleNamespace mock
5. msedge headless 截图：必须全新 `--user-data-dir` + URL 带 `?theme=dark` + `--virtual-time-budget=9000` + `file:///` 绝对路径（相对路径报"拒绝访问 0x5"）；Edge 忽略 `--force-prefers-color-scheme`
6. Git push 直连报 "fetch first"/Connection reset 属正常 → 用工作区 `git_api_push_v2.py`（必须 cd 到项目目录内运行 + 完整 Python 执行）；commit message 必须纯 ASCII
7. Windows 无 /tmp；curl 输出写到工作区路径；读 JSON 用 `encoding='utf-8'`（GBK 会乱码）；git 子进程加 `encoding="utf-8",errors="replace"`
8. 上传大文档是同步阻塞的（v1.2 规划异步队列），验证上传接口注意超时
9. 管理后台 API 全部走审计日志；保护规则：不能禁用/删除 admin 账号、不能删除自己；配额校验"新值 < 已用量"返回 400
10. config.yaml 热更新仅白名单字段（config.py ADMIN_EDITABLE），API key 在接口中遮蔽为 "****"，空字符串 key 表示保留原值
11. LLMService 的 provider 相关 URL/key 属性必须在 __init__ 赋值（ollama_base_url 曾漏赋值导致真机问答才暴露）
12. 反馈表在 security.db（feedback 表），消息 ID 为 conversation JSON 中 Message.id（uuid hex[:12]），管理端反馈列表走 /api/admin/feedback
13. 回归脚本控制台中文在 GBK 终端显示乱码属正常，报告写 data/eval_reports/ 文件
14. **uvicorn 重启必查端口**：taskkill 只杀父进程，旧 worker 仍占 8766（netstat -ano | grep :8766 确认监听 PID 再杀）；否则新代码不生效且日志报 bind 10048
15. 国产 provider（ascend/cambricon/mthreads）走 OpenAI 兼容协议：base_url 未配置回退 openai_base_url；api_key 空则省略 Authorization 头（内网无鉴权服务）
16. Milvus 后端 pymilvus 延迟导入（未安装时 Chroma 路径不受影响）；metadata 用 JSON 字符串存储（Milvus 不支持嵌套 dict 字段），search 返回时解析回 dict；本机无 Docker/Milvus，后端验证靠 mock 单测
17. 嵌入 ollama 通道必须走 settings.llm.ollama_base_url（曾硬编码 localhost:11434，容器部署时嵌入连不上 Ollama）；向量库统一走 get_vector_store 工厂，禁止直接 new ChromaVectorStore/MilvusVectorStore

## 迭代工作流（每个版本必须）

1. 实施功能 → 同步补/改测试 → 全量测试全绿
2. 真机验证：启动服务 + 关键链路（认证/问答/Agent/健康端点）+ 前端截图
3. README.md / ROADMAP.md / config.example.yaml 同步更新（ROADMAP 迭代日志勾 ✅）
4. git commit（英文 ASCII 消息）→ push（直连失败走 API 脚本）
5. 保持"每天可运行"：每次提交后系统必须可启动，不引入破坏性变更
