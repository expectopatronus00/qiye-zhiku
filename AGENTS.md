# AGENTS.md — 企业智库 RAG 系统 上手手册

> 给 AI Agent 的项目说明书：架构地图、运行命令、已知坑、迭代工作流。
> 换任何 agent 工具，先读本文件即可快速接手。人看的文档见 README.md。

## 项目是什么

面向央企 AI 场景的私有化 RAG 知识库问答系统（FastAPI + ChromaDB/Milvus + Ollama qwen2.5:7b + nomic-embed-text + bge-reranker-base）。当前版本 v1.6（进阶能力：知识图谱 + VLM 图表理解 + Webhook 通知），路线见 ROADMAP.md，迭代进度按"每日一版"节奏推进（v0.1~v1.6 已完成）。

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
  document.py      文档解析（PDF 版面按字号分标题/正文、表格转 Markdown、RapidOCR 图片 OCR；v1.6 图片 OCR 文本不足时走 VLM 图表描述 block_type=chart）
  conversation.py  对话历史管理（上下文窗口、持久化；v1.6 Message 加 entity_hits 字段随消息落盘/恢复）
  graph.py         v1.6 知识图谱 GraphBuilder（jieba posseg nr/ns/nt/nz + 内置信创领域词典 DEFAULT_ENTITY_DICT，块内两两共现加权关系；SQLite data/graph.db entities/relations 表；build 幂等先删后建；configure 追加合并词典；_graph_enhance 图谱问答增强：问题实体抽取→库内命中→向量检索补充上下文注入"【知识图谱补充知识】"段）
  vision.py        v1.6 VLM 图表理解 VLMCaptioner（OpenAI 兼容 /v1/chat/completions 对接昇腾 CANN vLLM-Ascend/寒武纪部署的 Qwen2.5-VL；base64 图片内联；未配置 base_url 返回 None 纯降级；get_captioner 工厂按 settings.vision 重建）
  webhook.py       v1.6 Webhook 通知 WebhookManager（飞书/钉钉双平台 payload，逗号分隔多 URL；后台 daemon 线程 fire 不阻塞 + 5s 超时单次重试；fire_event 按四事件开关：document.uploaded/task.failed/security.alert/feedback.submitted；get_webhook_manager 每次调用重新 configure 热更新立即生效）
  evaluator.py     RAGAS 评估（忠实度/相关性/召回率，本地裁判）
  security.py      认证（PBKDF2 哈希、令牌 24h、失败锁定 10min）、知识库权限隔离、审计日志、用户反馈 FeedbackManager（feedback 表落库 + 回流评测集）；v1.4 密码强度 validate_password_strength（≥8 位 + 3 类复杂度 + 弱口令黑名单 + 禁含用户名）+ 登录失败告警 _alert_security（threshold 触发 security.alert，防刷屏）
  masker.py        v1.4 敏感信息脱敏（SENSITIVE_RULES 6 条正则顺序执行，mask_sensitive 主入口；上传入库前 + 输出兜底双链路）
  tasks.py         v1.5 异步任务队列 TaskManager（SQLite data/tasks.db + ThreadPoolExecutor 2 workers + 处理器注册表 register()，状态机 pending→running→success/failed，async 处理器自动 asyncio.run 包装，全局单例 task_manager；懒连接：submit 先于 start 也能落库）
  metrics.py       v1.5 Prometheus 指标（手写文本格式零依赖：http_requests_total + 请求/检索/LLM 直方图，record_request/record_duration 埋点，render_metrics 输出）
  cache.py         v1.5 热门问题缓存 QACache（LRU + TTL，key=(collection, 标准化问题)，invalidate 按库失效，全局单例 qa_cache）
  session.py       v1.5 会话存储抽象（SQLiteSessionStore 单机 / RedisSessionStore 可选 security.redis_url：token→user 映射 + TTL，故障回退 SQLite 双写）
  logging_setup.py 日志（app.log/access.log 轮转 5MB×5 + 请求中间件带 user/duration + v1.5 指标埋点）
app/routers/       API 层（prefix 各自带 /api）
  auth.py          认证（/api/auth）
  chat.py          对话（/api/chat，含 POST /stream SSE 流式 + Agent 模式 + POST /feedback 用户反馈；v1.6 响应带 entity_hits 图谱命中，feedback 尾部 fire webhook）
  documents.py     文档（/api/documents，上传/列表/预览；v1.5 大文件超 async_upload_threshold 转后台任务返回 accepted+task_id；v1.6 上传建图后 fire webhook document.uploaded）
  knowledge.py     知识库（/api/knowledge，创建/查询/统计）
  graph.py         v1.6 图谱（/api/graph：GET stats 统计 / GET entities 实体列表 / GET entities/{name}/relations 关系 / GET related 邻居查询，权限同知识库隔离）
  audit.py         审计（/api/audit，仅管理员；GET /export CSV 带 BOM）
  admin.py         管理后台（/api/admin：用户管理/知识库配额/系统配置热更新/反馈列表与回流导出，仅管理员）
  tasks.py         v1.5 任务状态（/api/tasks：GET /{task_id} 单查 + GET 列表分页/状态过滤，非管理员仅本人任务）
  health.py        健康检查（/healthz 存活 + /readyz 就绪探测：向量库/DB/LLM 三段判定；LLM 探测按 provider 分支，国产走 /models）+ v1.5 /metrics Prometheus 端点（免登录）
app/static/index.html  前端单页（原生 JS，深色设计系统 + 浅色主题，无构建步骤）
eval/run_regression.py  黄金评测集一键回归（hit@5/MRR/top1，混合 vs 纯向量，基线对比；--collect-feedback 合并回流）
scripts/build_dual_arch.sh  x86_64+arm64 双架构镜像构建推送（buildx）
scripts/gen_self_signed_cert.py  v1.4 自签 TLS 证书（cryptography 优先，回退系统 openssl；输出 config-add.txt 供追加 config.yaml）
docs/xinchuang-deploy.md    麒麟 V10 / 统信 UOS 信创部署手册（二进制+Docker 双形态）
docs/dengbao-checklist.md   v1.4 等保 2.0 三级自查清单（十类控制项对照表 + 快速验证命令）
tests/             pytest 测试（251 项，按模块拆分 test_*.py，v1.6 新增 test_v16.py 19 项）
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
8. v1.5 起大文档（>5MB）上传已异步化（后台任务队列 + 前端轮询），小文档仍同步；验证异步链路把阈值临时调小即可（见坑 23）
9. 管理后台 API 全部走审计日志；保护规则：不能禁用/删除 admin 账号、不能删除自己；配额校验"新值 < 已用量"返回 400
10. config.yaml 热更新仅白名单字段（config.py ADMIN_EDITABLE），API key 在接口中遮蔽为 "****"，空字符串 key 表示保留原值
11. LLMService 的 provider 相关 URL/key 属性必须在 __init__ 赋值（ollama_base_url 曾漏赋值导致真机问答才暴露）
12. 反馈表在 security.db（feedback 表），消息 ID 为 conversation JSON 中 Message.id（uuid hex[:12]），管理端反馈列表走 /api/admin/feedback
13. 回归脚本控制台中文在 GBK 终端显示乱码属正常，报告写 data/eval_reports/ 文件
14. **uvicorn 重启必查端口**：taskkill 只杀父进程，旧 worker 仍占 8766（netstat -ano | grep :8766 确认监听 PID 再杀）；否则新代码不生效且日志报 bind 10048
15. 国产 provider（ascend/cambricon/mthreads）走 OpenAI 兼容协议：base_url 未配置回退 openai_base_url；api_key 空则省略 Authorization 头（内网无鉴权服务）
16. Milvus 后端 pymilvus 延迟导入（未安装时 Chroma 路径不受影响）；metadata 用 JSON 字符串存储（Milvus 不支持嵌套 dict 字段），search 返回时解析回 dict；本机无 Docker/Milvus，后端验证靠 mock 单测
17. 嵌入 ollama 通道必须走 settings.llm.ollama_base_url（曾硬编码 localhost:11434，容器部署时嵌入连不上 Ollama）；向量库统一走 get_vector_store 工厂，禁止直接 new ChromaVectorStore/MilvusVectorStore
18. v1.4 密码策略：测试密码必须合规（≥8 位 + 3 类字符，如 Abc@12345），旧测试的 6 位密码会报 ValueError；批量替换时注意 change_password/reset_password 测试同步改
19. 登录失败告警只在"密码错误"分支触发（fail_count 递增路径），锁定事件单独告警；threshold=0 关闭；审计查询参数是 /api/audit?action=security.alert（不是 action_filter）
20. documents.py 的 get_documents_by_metadata 返回 [{id,content,metadata}] dict 列表，preview 排序 _sort_key 收到的元素是 tuple（d[0] 取 id），不是 dict——v1.3 迁移曾写错导致 500；tools.py 同逻辑是对 dict 排序，两者别混
21. 验证上传脱敏用 multipart（collection_name 表单字段 + file），不是 JSON；preview 的 filename 带 file_id 前缀（先 /api/documents/list 拿真实文件名）；自签证书验证用 curl -sk
22. v1.5 上传接口声明 response_model 必须 Union[DocumentUploadResponse, TaskUploadResponse]——单模型校验会因异步分支缺 chunks_count 字段 500（真机验证踩过）
23. v1.5 TaskManager 是懒连接：submit/get/list 先于 start() 也能自动建库（_ensure_conn）；验证异步上传把 config.yaml async_upload_threshold 临时调小（如 100000）即可触发，验证完恢复 5242880；metrics 里 retrieval/llm 直方图只有问答发生后才出现（缓存命中不产生检索记录）
24. v1.6 Webhook 热更新即生效：get_webhook_manager() 每次调用从 settings 重新 configure，改 config.yaml webhook 段（或管理台热更新）无需重启；fire 是后台 daemon 线程，验证时给接收端留 1-2s 落盘再查
25. v1.6 entity_hits 持久化：Message 模型加了 entity_hits 字段，但聊天落盘必须显式传 add_message(..., entity_hits=xxx)——chat.py 共 5 处调用点（completions/缓存命中/stream/agent fallback/agent 响应体），新增回复路径漏传则该消息历史恢复后无图谱标签
26. v1.6 图谱 API 测试必须 patch security 模块名（如 patch("app.core.security.get_current_user")）+ importlib.reload(graph_mod) 强制重绑定——graph 路由 from app.core.security import 是绑定引用，单独跑 test_v16.py 靠 patch 先于 import 能过，全量跑会被其他测试先导入导致 401
27. admin_credentials.txt 是多行"标签: 值"格式（管理员: admin），解析须按行 split(":",1)[1].strip()；整文件 split(":",1) 会把中文标签当用户名（登录 401）
28. 免登录截图流程（v1.6 验证 UI 用）：临时改 config.yaml security.auth_enabled=false 重启 → 截图 → 必须恢复 true 重启；残留 false 会让全量测试期望 401/403 的用例全挂（认证测试返回 200），已踩两次
29. 图谱建图是幂等整库重建：build 先 DELETE 该 collection 旧 entities/relations 再插入（graph.db）；删知识库必须同时清理图谱（documents.py drop_collection 里调 graph_manager.drop）；图谱问答命中实体上限 2 个、每个实体向量检索 qa_context_topk=3 块，只注入"【知识图谱补充知识】"段不改主检索流程

## 迭代工作流（每个版本必须）

1. 实施功能 → 同步补/改测试 → 全量测试全绿
2. 真机验证：启动服务 + 关键链路（认证/问答/Agent/健康端点）+ 前端截图
3. README.md / ROADMAP.md / config.example.yaml 同步更新（ROADMAP 迭代日志勾 ✅）
4. git commit（英文 ASCII 消息）→ push（直连失败走 API 脚本）
5. 保持"每天可运行"：每次提交后系统必须可启动，不引入破坏性变更
