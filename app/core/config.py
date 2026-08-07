"""配置管理模块 - 从 config.yaml 加载配置"""
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, ConfigDict
import yaml


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


class LLMConfig(BaseModel):
    provider: str = "ollama"
    model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"  # Ollama 服务地址（容器部署时指向 ollama 服务）
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.3
    max_tokens: int = 2048


class EmbeddingConfig(BaseModel):
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    local_model_path: str = ""


class VectorStoreConfig(BaseModel):
    type: str = "chroma"
    persist_directory: str = "./data/vectorstore"
    dimension: int = 768


class DocumentConfig(BaseModel):
    chunk_size: int = 500
    chunk_overlap: int = 50
    allowed_extensions: list[str] = [".pdf", ".docx", ".xlsx", ".md", ".txt", ".csv"]
    upload_directory: str = "./data/uploads"
    # ---- 文档增强 (v0.5) ----
    heading_min_size: float = 13.0      # PDF 版面分析: 大于等于该字号判定为标题
    table_to_markdown: bool = True      # PDF 表格识别并转 Markdown
    ocr_enabled: bool = True            # PDF 内嵌图片 OCR
    ocr_max_images_per_page: int = 3    # 每页最多 OCR 的图片数
    ocr_min_area: int = 8000            # 参与 OCR 的最小图片面积(px²)，过滤小图标


class RetrievalConfig(BaseModel):
    top_k: int = 5
    score_threshold: float = 0.5
    hybrid_search: bool = True
    bm25_weight: float = 0.3


class QueryRewriteConfig(BaseModel):
    enabled: bool = True
    max_history_turns: int = 4


class RerankerConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    enabled: bool = True
    type: str = "heuristic"  # cross_encoder | heuristic | none
    model_path: str = ""
    top_n: int = 5


class EvalConfig(BaseModel):
    """评估体系配置 (v0.6)"""
    enabled: bool = True
    judge_model: str = ""            # 裁判模型，空则复用 llm.model（本地 qwen2.5:7b）
    judge_temperature: float = 0.0   # 裁判判定温度（0 保证判定一致性）
    num_generated_questions: int = 3 # 答案相关性评估时生成的问题数
    max_context_chars: int = 4000    # 每次裁判判定送入的最大上下文长度
    report_directory: str = "./data/eval_reports"
    # 质量门禁阈值（低于任一指标则评估失败，退出码 1，供 CI 使用）
    min_faithfulness: float = 0.5
    min_answer_relevancy: float = 0.4
    min_context_recall: float = 0.5


class SecurityConfig(BaseModel):
    """安全与权限配置 (v0.7)"""
    auth_enabled: bool = True        # 关闭后所有接口免登录（内网直连模式）
    token_expire_hours: int = 24     # 登录令牌有效期
    db_path: str = "./data/security.db"  # 用户/知识库归属/审计日志 SQLite
    admin_username: str = "admin"
    admin_password: str = ""         # 空则首次启动生成随机密码并写入 data/admin_credentials.txt
    max_login_attempts: int = 5      # 连续失败锁定次数（0 表示不锁定）


class AgentConfig(BaseModel):
    """Agent 模式配置 (v0.9)"""
    max_iterations: int = 6         # 单次任务最大工具调用轮数（防失控循环）
    default_collection: str = "default"  # 未指定知识库时的默认检索范围


class DataConfig(BaseModel):
    uploads: str = "./data/uploads"
    vectorstore: str = "./data/vectorstore"
    logs: str = "./data/logs"
    conversations: str = "./data/conversations"


class Settings(BaseModel):
    server: ServerConfig = ServerConfig()
    llm: LLMConfig = LLMConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    vectorstore: VectorStoreConfig = VectorStoreConfig()
    document: DocumentConfig = DocumentConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    query_rewrite: QueryRewriteConfig = QueryRewriteConfig()
    reranker: RerankerConfig = RerankerConfig()
    eval: EvalConfig = EvalConfig()
    security: SecurityConfig = SecurityConfig()
    agent: AgentConfig = AgentConfig()
    data: DataConfig = DataConfig()


def load_settings(config_path: Optional[str] = None) -> Settings:
    """从 YAML 文件加载配置，不存在则使用默认值"""
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config.yaml"
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return Settings(**data)
    else:
        # 使用默认配置
        return Settings()


# 全局配置实例
settings = load_settings()

# ---------------- 管理台热更新 (v1.1) ----------------

# 管理台可编辑的配置节与字段白名单（安全敏感项如 db_path/上传目录不回传；
# openai_api_key 可更新但查看时脱敏，留空表示保留原值）
ADMIN_EDITABLE: dict[str, set[str]] = {
    "llm": {"provider", "model", "ollama_base_url", "openai_base_url",
            "temperature", "max_tokens", "openai_api_key"},
    "embedding": {"provider", "model", "local_model_path", "openai_api_key"},
    "retrieval": {"top_k", "score_threshold", "hybrid_search", "bm25_weight"},
    "reranker": {"enabled", "type", "top_n"},
    "query_rewrite": {"enabled", "max_history_turns"},
    "agent": {"max_iterations"},
    "document": {"chunk_size", "chunk_overlap", "ocr_enabled"},
}

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


def get_config_view() -> dict:
    """返回管理台可编辑配置（密钥脱敏为 ****）"""
    data = settings.model_dump()
    for key in ("openai_api_key",):
        if data["llm"].get(key):
            data["llm"][key] = "****"
        if data["embedding"].get(key):
            data["embedding"][key] = "****"
    return {k: data[k] for k in ADMIN_EDITABLE}


def update_config(patch: dict) -> dict:
    """热更新配置并写回 config.yaml；空串密钥视为保留原值"""
    for section, fields in (patch or {}).items():
        if section not in ADMIN_EDITABLE:
            raise ValueError(f"不允许修改配置节: {section}")
        current = getattr(settings, section)
        for field, value in fields.items():
            if field not in ADMIN_EDITABLE[section]:
                raise ValueError(f"不允许修改配置项: {section}.{field}")
            if value is None:
                continue
            if field == "openai_api_key" and str(value).strip() == "":
                continue  # 空串保留原密钥
            setattr(current, field, value)
    _write_config(settings)
    return get_config_view()


def _write_config(s: Settings) -> None:
    """全量写回 config.yaml（含未修改项，保留密钥原值）"""
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("# 企业智库配置\n# 注：本文件可由管理台「系统配置」页保存时自动更新\n\n")
        yaml.safe_dump(s.model_dump(), f, allow_unicode=True, sort_keys=False)
