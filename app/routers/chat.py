"""对话 API 路由 - v0.3 支持 Query 改写, v0.7 接入认证/权限/审计"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.llm import LLMService
from app.core.retriever import Retriever
from app.core.conversation import conversation_manager
from app.core.query_rewriter import query_rewriter
from app.core.security import (
    User,
    get_audit_logger,
    get_current_user,
    get_feedback_manager,
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
    message_id: str = ""  # 助手消息 ID（v1.2 反馈锚点）
    retrieval_debug: Optional[dict] = None  # 检索诊断（v1.2 详情面板）
    cached: bool = False  # v1.5 是否命中热门问题缓存
    entity_hits: list[str] = []  # v1.6 图谱问答：命中的知识图谱实体


class FeedbackRequest(BaseModel):
    """用户反馈（v1.2）"""
    message_id: str
    rating: str  # up | down
    reason: str = ""  # 点踩原因
    expected_answer: str = ""  # 期望回答（点踩时建议填写，回流评测集）


class AgentRequest(BaseModel):
    """Agent 模式对话请求 (v0.9)"""
    message: str
    collection_name: str = "default"
    conversation_id: Optional[str] = None  # 对话 ID，为空则创建新对话


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

async def _graph_enhance(retriever, collection: str, question: str) -> tuple[list[str], str]:
    """v1.6 图谱问答增强：抽取问题实体 → 命中库内实体 → 用实体名向量检索补充上下文

    返回 (entity_hits, extra_context)
    """
    try:
        from app.core.graph import graph_builder
        entities = graph_builder.extract_entities(question)
        if not entities or not settings.graph.enabled:
            return [], ""
        known = {e["name"] for e in graph_builder.entities(collection, limit=500)}
        hits = [e for e in entities if e in known][:3]
        if not hits:
            return [], ""
        # 每个命中实体做一次向量检索，取补充块
        embedder = retriever.embedding_service
        extra_blocks: list[str] = []
        for ent in hits[:2]:
            vec = await embedder.embed_query(ent)
            docs = retriever.vectorstore.search(query_embedding=vec,
                                                top_k=settings.graph.qa_context_topk)
            for d in docs[:settings.graph.qa_context_topk]:
                content = d.get("content", "").strip()
                if content and content not in extra_blocks:
                    extra_blocks.append(content[:400])
        extra = ("\n\n---\n\n【知识图谱补充知识（实体: " + "、".join(hits) + "）】\n"
                 + "\n\n".join(extra_blocks)) if extra_blocks else ""
        return hits, extra
    except Exception:  # noqa: BLE001 - 图谱增强失败不影响主流程
        return [], ""


async def _prepare_rag_messages(
    llm: LLMService,
    conv,
    request: ChatRequest,
) -> tuple[list[dict], list[dict], Optional[str], Optional[dict], list[str]]:
    """构建 RAG 消息列表 + 来源 + 改写后查询 + 检索诊断 + v1.6 图谱命中实体

    返回: (messages, sources, rewritten_query, retrieval_debug, entity_hits)
    """
    # 记录用户消息（先存历史，供 Query 改写参考）
    conv.add_message("user", request.message)
    conversation_manager.save(conv)

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
        # 检索（用改写后的查询），retriever.last_debug 带诊断数据 (v1.2)
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

        # v1.6 图谱问答增强：实体命中注入补充上下文
        entity_hits, graph_extra = await _graph_enhance(
            retriever, conv.collection_name, request.message)
        if graph_extra:
            context += graph_extra

        messages = [
            {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})

        return messages, sources, (rewritten_query if query_changed else None), retriever.last_debug, entity_hits
    else:
        # 直接对话模式
        messages = [
            {"role": "system", "content": "你是一个专业的企业知识库问答助手。"},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": request.message})

        return messages, [], None, None, []


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
    """非流式对话接口（多轮 + Query 改写 + 检索诊断 v1.2 + v1.5 热门问题缓存）"""
    require_kb_access(request.collection_name, user)
    llm = LLMService()
    conv = _get_or_create_conv(request)

    # v1.5 热门问题缓存：仅新对话（无 conversation_id）的纯 RAG 请求可命中
    cacheable = (
        request.use_rag and not request.stream
        and not request.conversation_id and settings.cache.enabled
    )
    if cacheable:
        from app.core.cache import qa_cache
        cached = qa_cache.get(request.collection_name, request.message)
        if cached is not None:
            # 命中：仍落对话记录（保证反馈锚点与历史一致），跳过 LLM/检索
            conv.add_message("user", request.message)
            msg = conv.add_message("assistant", cached["answer"], sources=cached["sources"],
                                   entity_hits=cached.get("entity_hits", []))
            conversation_manager.save(conv)
            get_audit_logger().log(user.username, "chat.completion", conv.collection_name,
                                   f"对话 {conv.id}，问题: {request.message[:80]}（缓存命中）")
            return ChatResponse(
                answer=cached["answer"],
                sources=cached["sources"],
                model=llm.model,
                conversation_id=conv.id,
                message_id=msg.id,
                cached=True,
                entity_hits=cached.get("entity_hits", []),
            )

    messages, sources, rewritten_query, retrieval_debug, entity_hits = await _prepare_rag_messages(llm, conv, request)

    answer = await llm.chat(messages)
    # v1.4 输出链路脱敏（防 LLM 幻觉生成敏感信息）
    if settings.security.mask_sensitive:
        from app.core.masker import mask_sensitive
        answer = mask_sensitive(answer)

    # 记录助手回复
    msg = conv.add_message("assistant", answer, sources=sources, entity_hits=entity_hits)
    conversation_manager.save(conv)

    if cacheable:
        from app.core.cache import qa_cache
        qa_cache.set(request.collection_name, request.message,
                     {"answer": answer, "sources": sources, "entity_hits": entity_hits})

    get_audit_logger().log(user.username, "chat.completion", conv.collection_name,
                           f"对话 {conv.id}，问题: {request.message[:80]}")

    return ChatResponse(
        answer=answer,
        sources=sources,
        model=llm.model,
        conversation_id=conv.id,
        rewritten_query=rewritten_query,
        message_id=msg.id,
        retrieval_debug=retrieval_debug,
        entity_hits=entity_hits,
    )


@router.post("/stream")
async def chat_stream(request: ChatRequest, user: User = Depends(get_current_user)):
    """流式对话接口（多轮 + Query 改写）"""
    require_kb_access(request.collection_name, user)
    llm = LLMService()
    conv = _get_or_create_conv(request)

    messages, sources, rewritten_query, _debug, _hits = await _prepare_rag_messages(llm, conv, request)

    full_response = []

    async def generate():
        async for chunk in llm.chat_stream(messages):
            # v1.4 输出链路脱敏（块级兜底；跨块号码由落库时二次脱敏保证）
            if settings.security.mask_sensitive:
                from app.core.masker import mask_sensitive
                chunk = mask_sensitive(chunk)
            full_response.append(chunk)
            yield f"data: {chunk}\n\n"

        # 流结束后保存助手回复（全量二次脱敏，覆盖跨块号码）
        final_answer = "".join(full_response)
        if settings.security.mask_sensitive:
            from app.core.masker import mask_sensitive
            final_answer = mask_sensitive(final_answer)
        conv.add_message("assistant", final_answer, sources=sources, entity_hits=_hits)
        conversation_manager.save(conv)
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


@router.post("/agent")
async def chat_agent(request: AgentRequest, user: User = Depends(get_current_user)):
    """Agent 模式：Function Calling + 工具调用 + 多步推理 (v0.9)

    模型不支持工具调用时自动降级为普通 RAG 回答（fallback 标记返回）。
    """
    from app.core.agent import run_agent

    require_kb_access(request.collection_name, user)
    conv = _get_or_create_conv(request)

    # 记录用户消息
    conv.add_message("user", request.message)
    conversation_manager.save(conv)

    history = conv.get_history(max_turns=5)[:-1]  # 不含刚添加的当前消息

    try:
        result = await run_agent(
            request.message, user,
            collection=request.collection_name,
            history=history,
        )
    except Exception as exc:
        # LLM 不支持工具调用（或临时故障）→ 降级普通 RAG
        llm = LLMService()
        try:
            rag_request = ChatRequest(
                message=request.message,
                collection_name=request.collection_name,
                use_rag=True,
                stream=False,
            )
            # _prepare_rag_messages 会重新记录用户消息，先移除刚加的
            if conv.messages and conv.messages[-1].role == "user":
                conv.messages.pop()
            messages, sources, rewritten, _debug, _hits = await _prepare_rag_messages(llm, conv, rag_request)
            answer = await llm.chat(messages)
            if settings.security.mask_sensitive:
                from app.core.masker import mask_sensitive
                answer = mask_sensitive(answer)
            result = {"answer": answer, "steps": [], "source_files": [],
                      "fallback": True, "reason": str(exc)}
        except Exception:
            raise HTTPException(status_code=500, detail=f"Agent 调用失败: {exc}")

    # 持久化助手回复（含工具步骤）；fallback 路径带图谱命中实体
    conv.add_message("assistant", result["answer"], sources=[],
                     tool_steps=result["steps"],
                     entity_hits=_hits if result.get("fallback") else [])
    conversation_manager.save(conv)

    get_audit_logger().log(
        user.username, "chat.agent", conv.collection_name,
        f"对话 {conv.id}，问题: {request.message[:80]}，"
        f"工具调用 {len(result['steps'])} 次，来源 {len(result.get('source_files', []))} 个",
    )

    return {
        "answer": result["answer"],
        "tool_steps": result["steps"],
        "source_files": result.get("source_files", []),
        "model": LLMService().model,
        "conversation_id": conv.id,
        "iterations": result.get("iterations", 0),
        "fallback": result.get("fallback", False),
        "fallback_reason": result.get("reason", ""),
        "entity_hits": _hits if result.get("fallback") else [],
    }


# ========== 用户反馈闭环 (v1.2) ==========

def _find_message(message_id: str):
    """全库查找消息（message_id 全局唯一），返回 (conv, msg) 或 None"""
    for conv in conversation_manager.list_all():
        c = conversation_manager.get(conv["id"])
        if not c:
            continue
        for m in c.messages:
            if m.id == message_id:
                return c, m
    return None, None


@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest,
                          user: User = Depends(get_current_user)):
    """提交对某条回答的反馈（点赞/点踩 + 原因 + 期望回答）"""
    req.rating = (req.rating or "").strip().lower()
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating 必须为 up 或 down")
    if not req.message_id:
        raise HTTPException(status_code=400, detail="缺少 message_id")

    conv, msg = _find_message(req.message_id)
    if msg is None or msg.role != "assistant":
        raise HTTPException(status_code=404, detail="消息不存在")

    # 取该回答对应的用户问题（向前找最近一条 user 消息）
    question = ""
    for m in conv.messages:
        if m.id == msg.id:
            break
        if m.role == "user":
            question = m.content

    fb = get_feedback_manager().add(
        message_id=msg.id,
        username=user.username,
        rating=req.rating,
        question=question,
        answer=msg.content[:2000],
        conversation_id=conv.id,
        collection_name=conv.collection_name,
        reason=req.reason,
        expected_answer=req.expected_answer,
    )
    get_audit_logger().log(
        user.username, "chat.feedback", conv.collection_name,
        f"消息 {msg.id} {req.rating}，原因: {req.reason[:60]}",
    )
    # v1.6 Webhook：用户反馈通知（后台线程，不阻塞）
    try:
        from app.core.webhook import fire_event
        rating_cn = "点赞" if req.rating == "up" else "点踩"
        fire_event("feedback.submitted", "收到用户反馈",
                   f"用户 {user.username} 对回答{rating_cn}：{(req.reason or req.expected_answer or '')[:100]}")
    except Exception:
        pass
    return {"ok": True, "id": fb}
