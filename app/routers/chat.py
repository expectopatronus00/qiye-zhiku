"""对话 API 路由"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, AsyncGenerator
from fastapi.responses import StreamingResponse

from app.core.llm import LLMService
from app.core.retriever import Retriever

router = APIRouter()

# RAG 系统提示词模板
RAG_SYSTEM_PROMPT = """你是一个专业的企业知识库问答助手。请根据以下检索到的参考资料回答用户问题。

规则：
1. 只基于提供的参考资料回答，不要编造信息
2. 如果参考资料中没有相关信息，请明确告知"根据现有知识库资料，未找到相关答案"
3. 回答时请标注信息来源（引用哪个文档）
4. 回答要专业、准确、简洁

参考资料：
{context}"""


class ChatRequest(BaseModel):
    """对话请求"""
    message: str
    collection_name: str = "default"
    use_rag: bool = True
    stream: bool = False


class ChatResponse(BaseModel):
    """对话响应"""
    answer: str
    sources: list[dict] = []
    model: str = ""


@router.post("/completions", response_model=ChatResponse)
async def chat_completion(request: ChatRequest):
    """非流式对话接口"""
    llm = LLMService()
    sources = []

    if request.use_rag:
        # RAG 模式：先检索再生成
        retriever = Retriever(collection_name=request.collection_name)
        docs = await retriever.retrieve(request.message)
        sources = [
            {
                "content": doc["content"][:200] + "...",
                "metadata": doc.get("metadata", {}),
                "score": doc.get("score", 0),
            }
            for doc in docs
        ]

        context = "\n\n---\n\n".join(
            f"[来源: {doc.get('metadata', {}).get('filename', '未知')}]\n{doc['content']}"
            for doc in docs
        )

        messages = [
            {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
            {"role": "user", "content": request.message},
        ]
    else:
        # 直接对话模式
        messages = [
            {"role": "system", "content": "你是一个专业的企业知识库问答助手。"},
            {"role": "user", "content": request.message},
        ]

    answer = await llm.chat(messages)
    return ChatResponse(answer=answer, sources=sources, model=llm.model)


@router.post("/stream")
async def chat_stream(request: ChatRequest):
    """流式对话接口"""
    llm = LLMService()

    if request.use_rag:
        retriever = Retriever(collection_name=request.collection_name)
        docs = await retriever.retrieve(request.message)

        context = "\n\n---\n\n".join(
            f"[来源: {doc.get('metadata', {}).get('filename', '未知')}]\n{doc['content']}"
            for doc in docs
        )

        messages = [
            {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
            {"role": "user", "content": request.message},
        ]
    else:
        messages = [
            {"role": "system", "content": "你是一个专业的企业知识库问答助手。"},
            {"role": "user", "content": request.message},
        ]

    async def generate():
        async for chunk in llm.chat_stream(messages):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
