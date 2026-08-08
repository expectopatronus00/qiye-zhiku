"""文档管理 API 路由 (v0.7 接入知识库权限)"""
import uuid
from pathlib import Path
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

router = APIRouter()


class DocumentUploadResponse(BaseModel):
    """文档上传响应"""
    filename: str
    chunks_count: int
    collection_name: str
    status: str


class DocumentListResponse(BaseModel):
    """文档列表响应"""
    collection_name: str
    total_chunks: int
    documents: list[str]


@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    collection_name: str = Form("default"),
    user: User = Depends(get_current_user),
):
    """上传并解析文档，生成向量存入知识库（需知识库访问权限）"""
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

    try:
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

        # v1.1 配额校验（块数 + 文档数，-1 表示不限制）
        registry = get_kb_registry()
        kb = registry.get(collection_name)
        vectorstore = get_vector_store(collection_name=collection_name)
        existing_chunks = vectorstore.count()
        doc_count = 0
        if kb is not None and (kb.quota_chunks >= 0 or kb.quota_documents >= 0):
            try:
                metas = vectorstore.collection.get(
                    include=["metadatas"]).get("metadatas") or []
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

        get_audit_logger().log(user.username, "document.upload", collection_name,
                               f"上传 {file.filename}，共 {len(sub_chunks)} 块")

        return DocumentUploadResponse(
            filename=file.filename,
            chunks_count=len(sub_chunks),
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
    all_docs = vectorstore.collection.get(include=["metadatas"])
    filenames = list(set(
        meta.get("filename", "unknown")
        for meta in (all_docs["metadatas"] or [])
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
        result = vectorstore.collection.get(
            where={"filename": filename},
            include=["documents", "metadatas"],
        )
    except Exception:
        raise HTTPException(status_code=404, detail=f"文档 '{filename}' 不存在")

    ids = result.get("ids") or []
    docs = result.get("documents") or []
    metas = result.get("metadatas") or []
    if not ids:
        raise HTTPException(status_code=404, detail=f"文档 '{filename}' 不存在")

    # 按 id 中的序号排序（{file_id}_{index}），保证原文顺序
    def _sort_key(id_: str) -> int:
        try:
            return int(id_.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return 0

    pairs = sorted(zip(ids, docs, metas), key=lambda x: _sort_key(x[0]))
    chunks = [
        {
            "type": (meta or {}).get("block_type", "text"),
            "content": doc[:2000],  # 单块截断，防止超长
        }
        for _, doc, meta in pairs
    ]
    return {"filename": filename, "chunks_count": len(chunks), "chunks": chunks}
