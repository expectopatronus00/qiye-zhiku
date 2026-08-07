"""Agent 工具模块 (v0.9) - 工具注册表 + 央企知识库场景内置工具集

工具以装饰器注册，自动生成 OpenAI/Ollama 兼容的 function schema；
执行时按工具声明注入依赖（当前用户/知识库集合等），权限在工具内部校验。
"""
from __future__ import annotations

import inspect
import time
from typing import Any, Awaitable, Callable, Optional

from app.core.config import settings


# ---------------- 工具注册表 ----------------

class Tool:
    """单个工具：schema + 执行函数"""

    def __init__(self, name: str, description: str, func: Callable,
                 parameters: dict, dependencies: tuple[str, ...]):
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters
        self.dependencies = dependencies  # 需要框架注入的参数名

    def to_schema(self) -> dict:
        """OpenAI/Ollama 兼容的 function schema"""
        props = {k: v for k, v in self.parameters.get("properties", {}).items()
                 if k not in self.dependencies}
        required = [k for k in self.parameters.get("required", [])
                    if k not in self.dependencies]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }

    async def execute(self, arguments: dict, deps: dict) -> Any:
        """执行工具；deps 为框架注入的依赖（用户/知识库等）"""
        kwargs = {k: v for k, v in arguments.items() if k not in self.dependencies}
        kwargs.update({k: deps[k] for k in self.dependencies if k in deps})
        result = self.func(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


class ToolRegistry:
    """工具注册表：装饰器注册 + 按名执行"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, name: str = None, description: str = None,
                 parameters: dict = None, dependencies: tuple[str, ...] = ()):
        """装饰器：从函数签名自动生成 schema

        用法：
            @tools.register(description="...")
            async def search_kb(query: str, collection: str = "default", user=Injected):
        """
        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            sig = inspect.signature(func)
            props, required = {}, []
            for pname, param in sig.parameters.items():
                if pname in dependencies:
                    continue
                default = param.default
                required_flag = default is inspect.Parameter.empty
                if pname == "query":
                    ptype, pdesc = "string", "检索查询语句"
                elif pname == "collection" or pname == "collection_name":
                    ptype, pdesc = "string", "知识库名称，缺省为 default"
                elif pname == "filename":
                    ptype, pdesc = "string", "文档文件名"
                elif pname == "top_k":
                    ptype, pdesc = "integer", "返回条数（1-10）"
                else:
                    ptype, pdesc = "string", ""
                props[pname] = {"type": ptype, "description": pdesc}
                if required_flag:
                    required.append(pname)
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=description or (func.__doc__ or "").strip().splitlines()[0],
                func=func,
                parameters={"type": "object", "properties": props, "required": required},
                dependencies=dependencies,
            )
            return func
        return decorator

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.to_schema() for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict, deps: dict) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具: {name}")
        return await tool.execute(arguments, deps)


# ---------------- 全局注册表 ----------------

tools = ToolRegistry()


# ---------------- 内置工具集（央企知识库场景） ----------------

@tools.register(
    description="在指定知识库中检索与查询最相关的文档片段，返回带来源与相关度的结果",
    dependencies=("user",),
)
async def search_knowledge_base(query: str, collection: str = "default",
                                top_k: int = 5, user=None) -> dict:
    """检索知识库（Agent 核心工具：查文档、找答案、做总结前的资料收集）"""
    from app.core.security import require_kb_access
    from app.core.retriever import Retriever

    require_kb_access(collection, user)
    top_k = max(1, min(int(top_k), 10))
    retriever = Retriever(collection_name=collection)
    docs = await retriever.retrieve(query)
    hits = []
    for i, doc in enumerate(docs[:top_k], 1):
        hits.append({
            "index": i,
            "filename": doc.get("metadata", {}).get("filename", "未知"),
            "score": round(float(doc.get("score", 0)), 3),
            "content": doc["content"][:500],
        })
    return {
        "collection": collection,
        "total": len(hits),
        "hits": hits,
        "note": "以上片段来自检索结果，引用时标注文件名与序号",
    }


@tools.register(
    description="获取知识库中某文档的完整内容（按原文顺序分块），用于精读全文、提取细节或总结",
    dependencies=("user",),
)
async def preview_document(filename: str, collection: str = "default", user=None) -> dict:
    """读取文档全文（按原文顺序返回所有块）"""
    from app.core.security import require_kb_access
    from app.core.vectorstore import VectorStore

    require_kb_access(collection, user)
    vectorstore = VectorStore(collection_name=collection)
    try:
        result = vectorstore.collection.get(where={"filename": filename},
                                            include=["documents", "metadatas"])
    except Exception:
        return {"error": f"文档 '{filename}' 不存在于知识库 '{collection}'"}

    ids, docs = result.get("ids", []), result.get("documents", []) or []
    metas = result.get("metadatas", []) or []
    if not docs:
        return {"error": f"文档 '{filename}' 不存在于知识库 '{collection}'"}

    def _sort_key(id_: str) -> int:
        try:
            return int(id_.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return 0

    ordered = sorted(zip(ids, docs, metas), key=lambda x: _sort_key(x[0]))
    return {
        "filename": filename,
        "collection": collection,
        "chunks_count": len(ordered),
        "chunks": [
            {"block_type": (m or {}).get("block_type", "text"), "content": d[:2000]}
            for _, d, m in ordered
        ],
    }


@tools.register(
    description="列出当前用户可见的全部知识库及其文档规模（块数），用于确认可查询的范围",
    dependencies=("user",),
)
async def list_knowledge_bases(user=None) -> dict:
    """列出可见知识库及规模"""
    from app.core.security import get_kb_registry
    from app.core.vectorstore import VectorStore

    kbs = get_kb_registry().list_for(user)
    result = []
    for kb in kbs:
        try:
            count = VectorStore(collection_name=kb.name).count()
        except Exception:
            count = 0
        result.append({"name": kb.name, "owner": kb.owner, "chunk_count": count})
    return {"total": len(result), "knowledge_bases": result}


@tools.register(
    description="获取知识库的统计信息：文档数、文档块数、文档列表，用于回答'知识库里有什么'类问题",
    dependencies=("user",),
)
async def knowledge_base_stats(collection: str = "default", user=None) -> dict:
    """知识库统计"""
    from app.core.security import require_kb_access
    from app.core.vectorstore import VectorStore

    require_kb_access(collection, user)
    vectorstore = VectorStore(collection_name=collection)
    try:
        data = vectorstore.collection.get(include=["metadatas"])
    except Exception:
        return {"error": f"知识库 '{collection}' 不存在或无数据"}

    metas = data.get("metadatas", []) or []
    filenames: dict[str, int] = {}
    for m in metas:
        fname = (m or {}).get("filename", "未知")
        filenames[fname] = filenames.get(fname, 0) + 1
    return {
        "collection": collection,
        "chunk_count": len(metas),
        "document_count": len(filenames),
        "documents": [
            {"filename": f, "chunks": c} for f, c in sorted(filenames.items())
        ],
    }


@tools.register(description="获取当前日期时间，用于回答时间相关的问题")
async def get_current_time() -> dict:
    """当前时间"""
    return {"datetime": time.strftime("%Y-%m-%d %H:%M:%S")}
