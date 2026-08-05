"""对话 API 路由 - v0.3 支持 Query 改写, v0.7 接入认证/权限/审计"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from fastapi.responses import StreamingResponse

from app.core.llm import LLMService
from app.core.retriever import Retriever
from app.core.conversation import conversation_manager
from app.core.query_rewriter import query_rewriter
from app.core.security import (
    User,
    get_audit_logger,
    get_current_user,
    get_kb_registry,
    require_kb_access,
)

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
    rewrite_query: bool = True  # 是否启用 Query 改写


class ChatResponse(BaseModel):
    """对话响应"""
    answer: str
    sources: list[dict] = []
    model: str = ""
    conversation_id: str = ""
    rewritten_query: Optional[str] = None  # 改写后的查询（仅发生改写时返回）


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

def _visible_collections(user: User) -> set[str]:
    """当前用户可见的知识库集合"""
    return {kb.name for kb in get_kb_registry().list_for(user)}


@router.get("/conversations")
async def list_conversations(user: User = Depends(get_current_user)):
    """列出当前用户可见知识库下的所有对话"""
    visible = _visible_collections(user)
    return [
        c for c in conversation_manager.list_all()
        if c["collection_name"] in visible
    ]


@router.post("/conversations")
async def create_conversation(req: CreateConversationRequest,
                              user: User = Depends(get_current_user)):
    """创建新对话（需知识库访问权限）"""
    require_kb_access(req.collection_name, user)
    conv = conversation_manager.create(collection_name=req.collection_name)
    return {"id": conv.id, "title": conv.title}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str,
                           user: User = Depends(get_current_user)):
    """获取对话详情（含消息历史）"""
    conv = conversation_manager.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    require_kb_access(conv.collection_name, user)
    return conv.to_dict()


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str,
                              user: User = Depends(get_current_user)):
    """删除对话"""
    conv = conversation_manager.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    require_kb_access(conv.collection_name, user)
    if conversation_manager.delete(conversation_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="对话不存在")


@router.post("/conversations/{conversation_id}/clear")
async def clear_conversation(conversation_id: str,
                             user: User = Depends(get_current_user)):
    """清空对话消息"""
    conv = conversation_manager.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    require_kb_access(conv.collection_name, user)
    if conversation_manager.clear_messages(conversation_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="对话不存在")


# ========== RAG 核心逻辑（共享） ==========

async def _prepare_rag_messages(
    llm: LLMService,
    conv,
    request: ChatRequest,
) -> tuple[list[dict], list[dict], Optional[str]]:
    """构建 RAG 消息列表 + 来源 + 改写后查询

    返回: (messages, sources, rewritten_query)
    """
    # 记录用户消息（先存历史，供 Query 改写参考）
    conv.add_message("user", request.message)

    # 获取历史（不含刚添加的当前消息）
    history = conv.get_history(max_turns=5)[:-1]

    # Query 改写：把追问补全为独立查询
    rewritten_query = request.message
    query_changed = False
    if request.use_rag and request.rewrite_query:
        rewritten_query, query_changed = await query_rewriter.process(
            request.message, history
        )

    if request.use_rag:
        # 检索（用改写后的查询）
        retriever = Retriever(collection_name=conv.collection_name)
        docs = await retriever.retrieve(rewritten_query)

        sources = [
            {
                "content": doc["content"][:200] + "...",
                "metadata": doc.get("metadata", {}),
                "filename": doc.get("metadata", {}).get("filename", "未知"),
                "score": doc.get("score", 0),
                "original_score": doc.get("original_score", 0),
                "reranker": doc.get("reranker", None),
            }
            for doc in docs
        ]

        context = "\n\n---\n\n".join(
            f"[来源: {doc.get('metadata', {}).get('filename', '未知')}]\n{doc['content']}"
            for doc in docs
        )

        messages = [
            {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})

        return messages, sources, (rewritten_query if query_changed else None)
    else:
        # 直接对话模式
        messages = [
            {"role": "system", "content": "你是一个专业的企业知识库问答助手。"},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})

        return messages, [], None


def _get_or_create_conv(request: ChatRequest):
    """获取或创建对话"""
    if request.conversation_id:
        conv = conversation_manager.get(request.conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")
    else:
        conv = conversation_manager.create(collection_name=request.collection_name)
    return conv


# ========== 对话接口 ==========

@router.post("/completions", response_model=ChatResponse)
async def chat_completion(request: ChatRequest, user: User = Depends(get_current_user)):
    """非流式对话接口（多轮 + Query 改写）"""
    require_kb_access(request.collection_name, user)
    llm = LLMService()
    conv = _get_or_create_conv(request)

    messages, sources, rewritten_query = await _prepare_rag_messages(llm, conv, request)

    answer = await llm.chat(messages)

    # 记录助手回复
    conv.add_message("assistant", answer, sources=sources)

    get_audit_logger().log(user.username, "chat.completion", conv.collection_name,
                           f"对话 {conv.id}，问题: {request.message[:80]}")

    return ChatResponse(
        answer=answer,
        sources=sources,
        model=llm.model,
        conversation_id=conv.id,
        rewritten_query=rewritten_query,
    )


@router.post("/stream")
async def chat_stream(request: ChatRequest, user: User = Depends(get_current_user)):
    """流式对话接口（多轮 + Query 改写）"""
    require_kb_access(request.collection_name, user)
    llm = LLMService()
    conv = _get_or_create_conv(request)

    messages, sources, rewritten_query = await _prepare_rag_messages(llm, conv, request)

    full_response = []

    async def generate():
        async for chunk in llm.chat_stream(messages):
            full_response.append(chunk)
            yield f"data: {chunk}\n\n"

        # 流结束后保存助手回复
        conv.add_message("assistant", "".join(full_response), sources=sources)
        get_audit_logger().log(user.username, "chat.stream", conv.collection_name,
                               f"对话 {conv.id}，问题: {request.message[:80]}")
        yield "data: [DONE]\n\n"

    headers = {"X-Conversation-Id": conv.id}
    if rewritten_query:
        headers["X-Rewritten-Query"] = rewritten_query

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=headers,
    )
