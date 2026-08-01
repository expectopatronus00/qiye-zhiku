"""对话 API 路由 - v0.2 支持多轮对话"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from fastapi.responses import StreamingResponse

from app.core.llm import LLMService
from app.core.retriever import Retriever
from app.core.conversation import conversation_manager

router = APIRouter()

# RAG 系统提示词模板
RAG_SYSTEM_PROMPT = """你是一个专业的企业知识库问答助手。请根据以下检索到的参考资料回答用户问题。

规则：
1. 只基于提供的参考资料回答，不要编造信息
2. 如果参考资料中没有相关信息，请明确告知"根据现有知识库资料，未找到相关答案"
3. 回答时请标注信息来源（引用哪个文档）
4. 回答要专业、准确、简洁
5. 结合对话历史理解用户意图，保持上下文连贯

参考资料：
{context}"""


class ChatRequest(BaseModel):
    """对话请求"""
    message: str
    collection_name: str = "default"
    use_rag: bool = True
    stream: bool = False
    conversation_id: Optional[str] = None  # 对话 ID，为空则创建新对话


class ChatResponse(BaseModel):
    """对话响应"""
    answer: str
    sources: list[dict] = []
    model: str = ""
    conversation_id: str = ""


class ConversationInfo(BaseModel):
    """对话信息"""
    id: str
    title: str
    collection_name: str
    message_count: int
    created_at: float
    updated_at: float


class CreateConversationRequest(BaseModel):
    """创建对话请求"""
    collection_name: str = "default"


# ========== 对话会话管理 ==========

@router.get("/conversations")
async def list_conversations():
    """列出所有对话"""
    return conversation_manager.list_all()


@router.post("/conversations")
async def create_conversation(req: CreateConversationRequest):
    """创建新对话"""
    conv = conversation_manager.create(collection_name=req.collection_name)
    return {"id": conv.id, "title": conv.title}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """获取对话详情（含消息历史）"""
    conv = conversation_manager.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conv.to_dict()


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """删除对话"""
    if conversation_manager.delete(conversation_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="对话不存在")


@router.post("/conversations/{conversation_id}/clear")
async def clear_conversation(conversation_id: str):
    """清空对话消息"""
    if conversation_manager.clear_messages(conversation_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="对话不存在")


# ========== 对话接口 ==========

@router.post("/completions", response_model=ChatResponse)
async def chat_completion(request: ChatRequest):
    """非流式对话接口（支持多轮）"""
    llm = LLMService()

    # 获取或创建对话
    if request.conversation_id:
        conv = conversation_manager.get(request.conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")
    else:
        conv = conversation_manager.create(collection_name=request.collection_name)

    # 记录用户消息
    conv.add_message("user", request.message)

    sources = []
    if request.use_rag:
        # RAG 模式：先检索再生成
        retriever = Retriever(collection_name=conv.collection_name)
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

        # 构建消息列表：system + 历史对话 + 当前用户消息
        messages = [
            {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
        ]
        # 插入历史对话（不含刚添加的当前消息）
        history = conv.get_history(max_turns=5)[:-1]  # 去掉最后一条（当前用户消息）
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})
    else:
        # 直接对话模式
        messages = [
            {"role": "system", "content": "你是一个专业的企业知识库问答助手。"},
        ]
        history = conv.get_history(max_turns=5)[:-1]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})

    answer = await llm.chat(messages)

    # 记录助手回复
    conv.add_message("assistant", answer, sources=sources)

    return ChatResponse(
        answer=answer,
        sources=sources,
        model=llm.model,
        conversation_id=conv.id,
    )


@router.post("/stream")
async def chat_stream(request: ChatRequest):
    """流式对话接口（支持多轮）"""
    llm = LLMService()

    # 获取或创建对话
    if request.conversation_id:
        conv = conversation_manager.get(request.conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")
    else:
        conv = conversation_manager.create(collection_name=request.collection_name)

    # 记录用户消息
    conv.add_message("user", request.message)

    if request.use_rag:
        retriever = Retriever(collection_name=conv.collection_name)
        docs = await retriever.retrieve(request.message)

        context = "\n\n---\n\n".join(
            f"[来源: {doc.get('metadata', {}).get('filename', '未知')}]\n{doc['content']}"
            for doc in docs
        )

        messages = [
            {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
        ]
        history = conv.get_history(max_turns=5)[:-1]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})
    else:
        messages = [
            {"role": "system", "content": "你是一个专业的企业知识库问答助手。"},
        ]
        history = conv.get_history(max_turns=5)[:-1]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})

    full_response = []

    async def generate():
        async for chunk in llm.chat_stream(messages):
            full_response.append(chunk)
            yield f"data: {chunk}\n\n"

        # 流结束后保存助手回复
        conv.add_message("assistant", "".join(full_response))
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Conversation-Id": conv.id},
    )
