"""文档管理 API 路由"""
import uuid
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from pydantic import BaseModel

from app.core.config import settings
from app.core.document import DocumentParser, TextSplitter
from app.core.embeddings import EmbeddingService
from app.core.vectorstore import VectorStore

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
):
    """上传并解析文档，生成向量存入知识库"""
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

        # 生成嵌入向量
        embedding_service = EmbeddingService()
        texts = [chunk.content for chunk in sub_chunks]
        embeddings = await embedding_service.embed_text(texts)

        # 存入向量数据库
        vectorstore = VectorStore(collection_name=collection_name)
        ids = [f"{file_id}_{i}" for i in range(len(sub_chunks))]
        metadatas = [chunk.metadata for chunk in sub_chunks]

        vectorstore.add_documents(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        return DocumentUploadResponse(
            filename=file.filename,
            chunks_count=len(sub_chunks),
            collection_name=collection_name,
            status="success",
        )

    except Exception as e:
        # 清理失败的文件
        if save_path.exists():
            save_path.unlink()
        raise HTTPException(status_code=500, detail=f"文档处理失败: {str(e)}")


@router.get("/list/{collection_name}", response_model=DocumentListResponse)
async def list_documents(collection_name: str):
    """列出知识库中的文档信息"""
    vectorstore = VectorStore(collection_name=collection_name)
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


@router.delete("/collection/{collection_name}")
async def delete_collection(collection_name: str):
    """删除整个知识库"""
    try:
        vectorstore = VectorStore(collection_name=collection_name)
        vectorstore.delete_collection()
        return {"status": "success", "message": f"知识库 '{collection_name}' 已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")
