"""配置管理模块 - 从 config.yaml 加载配置"""
from pathlib import Path
from typing import Optional
from pydantic import BaseModel
import yaml


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


class LLMConfig(BaseModel):
    provider: str = "ollama"
    model: str = "qwen2.5:7b"
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


class RetrievalConfig(BaseModel):
    top_k: int = 5
    score_threshold: float = 0.5
    hybrid_search: bool = True
    bm25_weight: float = 0.3


class DataConfig(BaseModel):
    uploads: str = "./data/uploads"
    vectorstore: str = "./data/vectorstore"
    logs: str = "./data/logs"


class Settings(BaseModel):
    server: ServerConfig = ServerConfig()
    llm: LLMConfig = LLMConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    vectorstore: VectorStoreConfig = VectorStoreConfig()
    document: DocumentConfig = DocumentConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
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
