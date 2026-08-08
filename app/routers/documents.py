"""文档管理 API 路由 (v0.7 接入知识库权限)"""
import uuid
from pathlib import Path
from typing import Union
from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Depends
from pydantic import BaseModel

from app.core.config import settings
from app.core.document import DocumentParser, TextSplitter
from app.core.embeddings import EmbeddingService
from app.core.vectorstore import get_vector_store
from app.core.security import (
    User,
    get_audit_logger,
    get_current_user,
    get_kb_registry,
    require_kb_access,
)
from app.core.tasks import task_manager

router = APIRouter()


class DocumentUploadResponse(BaseModel):
    """文档上传响应"""
    filename: str
    chunks_count: int
    collection_name: str
    status: str


class TaskUploadResponse(BaseModel):
    """大文档异步上传响应（v1.5：立即返回，后台处理）"""
    filename: str
    collection_name: str
    status: str
    task_id: str
    message: str


class DocumentListResponse(BaseModel):
    """文档列表响应"""
    collection_name: str
    total_chunks: int
    documents: list[str]


async def _process_upload(save_path: Path, collection_name: str,
                          username: str, file_id: str, filename: str) -> int:
    """解析 + 分块 + 脱敏 + 配额校验 + 嵌入 + 入库；返回块数（上传处理公共逻辑）"""
    # 解析文档
    parser = DocumentParser()
    chunks = parser.parse(str(save_path))

    if not chunks:
        raise HTTPException(status_code=400, detail="文档解析失败：未提取到有效内容")

    # 分块
    splitter = TextSplitter(
        chunk_size=settings.document.chunk_size,
        chunk_overlap=settings.document.chunk_overlap,
    )
    sub_chunks = splitter.split(chunks)

    # v1.4 上传链路脱敏：向量库内不落明文（手机号/身份证/银行卡/密钥等）
    if settings.security.mask_sensitive:
        from app.core.masker import mask_sensitive
        for c in sub_chunks:
            c.content = mask_sensitive(c.content)

    # v1.1 配额校验（块数 + 文档数，-1 表示不限制）
    registry = get_kb_registry()
    kb = registry.get(collection_name)
    vectorstore = get_vector_store(collection_name=collection_name)
    existing_chunks = vectorstore.count()
    doc_count = 0
    if kb is not None and (kb.quota_chunks >= 0 or kb.quota_documents >= 0):
        try:
            metas = vectorstore.get_metadatas()
            doc_count = len({m.get("filename", "unknown") for m in metas})
        except Exception:
            pass
    if kb is not None and kb.quota_chunks >= 0 and \
            existing_chunks + len(sub_chunks) > kb.quota_chunks:
        raise HTTPException(
            status_code=403,
            detail=f"知识库 '{collection_name}' 块数配额 {kb.quota_chunks} 已满"
                   f"（当前 {existing_chunks}，本次新增 {len(sub_chunks)}），请联系管理员调整配额")
    if kb is not None and kb.quota_documents >= 0 and doc_count + 1 > kb.quota_documents:
        raise HTTPException(
            status_code=403,
            detail=f"知识库 '{collection_name}' 文档数配额 {kb.quota_documents} 已满"
                   f"（当前 {doc_count} 份），请联系管理员调整配额")

    # 生成嵌入向量
    embedding_service = EmbeddingService()
    texts = [chunk.content for chunk in sub_chunks]
    embeddings = await embedding_service.embed_text(texts)

    # 存入向量数据库
    ids = [f"{file_id}_{i}" for i in range(len(sub_chunks))]
    metadatas = [chunk.metadata for chunk in sub_chunks]

    vectorstore.add_documents(
        ids=ids,
        documents=texts,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    get_audit_logger().log(username, "document.upload", collection_name,
                           f"上传 {filename}，共 {len(sub_chunks)} 块")
    # v1.5 知识库内容变更 → 热门问题缓存按库失效
    try:
        from app.core.cache import qa_cache
        qa_cache.invalidate(collection_name)
    except Exception:
        pass
    # v1.6 知识图谱：上传后自动建图（幂等重建该库图谱）
    if settings.graph.enabled:
        try:
            from app.core.graph import graph_builder
            graph_builder.configure(settings.graph.entity_dict)
            graph_builder.build(collection_name, sub_chunks)
        except Exception:  # noqa: BLE001 - 建图失败不影响入库
            logger = __import__("logging").getLogger("documents")
            logger.warning("知识图谱构建失败（不影响入库）", exc_info=True)
    # v1.6 Webhook：上传完成通知（后台线程发送，不阻塞）
    try:
        from app.core.webhook import fire_event
        fire_event("document.uploaded", "文档上传完成",
                   f"知识库「{collection_name}」新增文档 {filename}，共 {len(sub_chunks)} 块已入库")
    except Exception:
        pass
    return len(sub_chunks)


def _async_upload_handler(task_id: str, params: dict) -> dict:
    """后台任务处理器：调用公共处理逻辑（async 包装在 TaskManager 内完成）"""
    import asyncio

    save_path = Path(params["save_path"])
    try:
        chunks_count = asyncio.run(_process_upload(
            save_path, params["collection_name"],
            params["username"], params["file_id"], params["filename"],
        ))
        return {"chunks_count": chunks_count, "status": "success"}
    except HTTPException as e:
        if save_path.exists():
            save_path.unlink()
        return {"error": e.detail, "status": "failed"}
    except Exception as e:  # noqa: BLE001
        if save_path.exists():
            save_path.unlink()
        return {"error": str(e), "status": "failed"}


# 注册异步上传处理器（幂等）
if not task_manager.has_handler("document.upload"):
    task_manager.register("document.upload", _async_upload_handler)


@router.post("/upload", response_model=Union[DocumentUploadResponse, TaskUploadResponse])
async def upload_document(
    file: UploadFile = File(...),
    collection_name: str = Form("default"),
    user: User = Depends(get_current_user),
):
    """上传并解析文档，生成向量存入知识库（需知识库访问权限）

    v1.5: 超过 async_upload_threshold（默认 5MB）的大文档自动转后台任务，
    立即返回 202 语义（status=accepted + task_id），可轮询 /api/tasks/{id}。
    """
    # 权限校验
    require_kb_access(collection_name, user)

    # 检查文件类型
    ext = Path(file.filename).suffix.lower()
    if ext not in settings.document.allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持: {settings.document.allowed_extensions}",
        )

    # 保存上传文件
    upload_dir = Path(settings.document.upload_directory)
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_id = str(uuid.uuid4())[:8]
    save_path = upload_dir / f"{file_id}_{file.filename}"

    content = await file.read()
    save_path.write_bytes(content)

    # v1.5 大文档异步化：超过阈值不阻塞请求，后台处理
    if len(content) > settings.document.async_upload_threshold:
        task_id = task_manager.submit("document.upload", {
            "save_path": str(save_path),
            "collection_name": collection_name,
            "username": user.username,
            "file_id": file_id,
            "filename": file.filename,
        }, created_by=user.username)
        return TaskUploadResponse(
            filename=file.filename,
            collection_name=collection_name,
            status="accepted",
            task_id=task_id,
            message=f"文档 {len(content) // 1024 // 1024}MB 超过异步阈值"
                    f"（{settings.document.async_upload_threshold // 1024 // 1024}MB），"
                    f"已进入后台处理，任务 ID: {task_id}",
        )

    try:
        chunks_count = await _process_upload(
            save_path, collection_name, user.username, file_id, file.filename)
        return DocumentUploadResponse(
            filename=file.filename,
            chunks_count=chunks_count,
            collection_name=collection_name,
            status="success",
        )

    except HTTPException:
        # 业务异常（配额超限/格式不支持等）原样透传，不包 500
        if save_path.exists():
            save_path.unlink()
        raise
    except Exception as e:
        # 清理失败的文件
        if save_path.exists():
            save_path.unlink()
        raise HTTPException(status_code=500, detail=f"文档处理失败: {str(e)}")


@router.get("/list/{collection_name}", response_model=DocumentListResponse)
async def list_documents(
    collection_name: str,
    user: User = Depends(get_current_user),
):
    """列出知识库中的文档信息（需知识库访问权限）"""
    require_kb_access(collection_name, user)
    vectorstore = get_vector_store(collection_name=collection_name)
    total = vectorstore.count()

    # 获取所有文档的元数据
    metas = vectorstore.get_metadatas()
    filenames = list(set(
        meta.get("filename", "unknown")
        for meta in metas
    ))

    return DocumentListResponse(
        collection_name=collection_name,
        total_chunks=total,
        documents=filenames,
    )


@router.get("/preview/{collection_name}/{filename}")
async def preview_document(
    collection_name: str,
    filename: str,
    user: User = Depends(get_current_user),
):
    """预览文档内容：返回该文档在知识库中的全部文本块（需知识库访问权限）"""
    require_kb_access(collection_name, user)
    vectorstore = get_vector_store(collection_name=collection_name)

    try:
        # 按 filename 过滤查询全部块
        result = vectorstore.get_documents_by_metadata({"filename": filename})
    except Exception:
        raise HTTPException(status_code=404, detail=f"文档 '{filename}' 不存在")

    if not result:
        raise HTTPException(status_code=404, detail=f"文档 '{filename}' 不存在")

    # 按 id 中的序号排序（{file_id}_{index}），保证原文顺序
    def _sort_key(pair: tuple) -> int:
        try:
            return int(pair[0].rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return 0

    pairs = sorted([(d["id"], d["content"], d["metadata"]) for d in result], key=_sort_key)
    chunks = [
        {
            "type": (meta or {}).get("block_type", "text"),
            "content": doc[:2000],  # 单块截断，防止超长
        }
        for _, doc, meta in pairs
    ]
    return {"filename": filename, "chunks_count": len(chunks), "chunks": chunks}
