"""对话历史管理 - 多轮对话支持"""
import uuid
import time
from typing import Optional
from dataclasses import dataclass, field, asdict
from pathlib import Path
import json

from app.core.config import settings


@dataclass
class Message:
    """单条消息"""
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float = field(default_factory=time.time)
    sources: list[dict] = field(default_factory=list)  # RAG 检索来源（仅 assistant）
    tool_steps: list[dict] = field(default_factory=list)  # Agent 工具调用步骤（v0.9）
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # 消息ID（v1.2 反馈锚点）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "sources": self.sources,
            "tool_steps": self.tool_steps,
        }

    def to_llm_dict(self) -> dict:
        """转换为 LLM API 格式（不含 timestamp/sources）"""
        return {"role": self.role, "content": self.content}


@dataclass
class Conversation:
    """对话会话"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = "新对话"
    messages: list[Message] = field(default_factory=list)
    collection_name: str = "default"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def add_message(self, role: str, content: str, sources: list[dict] = None,
                    tool_steps: list[dict] = None):
        msg = Message(role=role, content=content, sources=sources or [],
                      tool_steps=tool_steps or [])
        self.messages.append(msg)
        self.updated_at = time.time()

        # 自动设置标题（取第一条用户消息的前 20 字）
        if role == "user" and self.title == "新对话":
            self.title = content[:20] + ("..." if len(content) > 20 else "")

        return msg

    def get_history(self, max_turns: int = 10) -> list[dict]:
        """获取最近 N 轮对话历史（用于 LLM 上下文）"""
        # 取最近 max_turns 条消息（不含 system）
        history = [m for m in self.messages if m.role != "system"]
        return [m.to_llm_dict() for m in history[-max_turns * 2:]]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "collection_name": self.collection_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
        }


class ConversationManager:
    """对话管理器 - 管理多个会话"""

    def __init__(self):
        self._conversations: dict[str, Conversation] = {}
        self._data_dir = Path(settings.data.conversations)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def create(self, collection_name: str = "default") -> Conversation:
        """创建新对话"""
        conv = Conversation(collection_name=collection_name)
        self._conversations[conv.id] = conv
        self._save(conv)
        return conv

    def get(self, conversation_id: str) -> Optional[Conversation]:
        """获取对话"""
        return self._conversations.get(conversation_id)

    def list_all(self) -> list[dict]:
        """列出所有对话（不含消息详情）"""
        convs = sorted(
            self._conversations.values(),
            key=lambda c: c.updated_at,
            reverse=True,
        )
        return [
            {
                "id": c.id,
                "title": c.title,
                "collection_name": c.collection_name,
                "message_count": len(c.messages),
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in convs
        ]

    def delete(self, conversation_id: str) -> bool:
        """删除对话"""
        if conversation_id in self._conversations:
            del self._conversations[conversation_id]
            file = self._data_dir / f"{conversation_id}.json"
            if file.exists():
                file.unlink()
            return True
        return False

    def clear_messages(self, conversation_id: str) -> bool:
        """清空对话消息（保留会话）"""
        conv = self._conversations.get(conversation_id)
        if conv:
            conv.messages.clear()
            conv.title = "新对话"
            conv.updated_at = time.time()
            self._save(conv)
            return True
        return False

    def save(self, conv: Conversation) -> None:
        """持久化单个对话（新增消息后调用，防止重启丢消息）"""
        self._save(conv)

    def _save(self, conv: Conversation):
        """持久化单个对话"""
        file = self._data_dir / f"{conv.id}.json"
        with open(file, "w", encoding="utf-8") as f:
            json.dump(conv.to_dict(), f, ensure_ascii=False, indent=2)

    def _load_all(self):
        """启动时加载所有对话"""
        for file in self._data_dir.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                conv = Conversation(
                    id=data["id"],
                    title=data.get("title", "新对话"),
                    collection_name=data.get("collection_name", "default"),
                    created_at=data.get("created_at", 0),
                    updated_at=data.get("updated_at", 0),
                )
                for msg_data in data.get("messages", []):
                    conv.messages.append(
                        Message(
                            role=msg_data["role"],
                            content=msg_data["content"],
                            timestamp=msg_data.get("timestamp", 0),
                            sources=msg_data.get("sources", []),
                            tool_steps=msg_data.get("tool_steps", []),
                            id=msg_data.get("id", uuid.uuid4().hex[:12]),
                        )
                    )
                self._conversations[conv.id] = conv
            except Exception:
                continue  # 跳过损坏的文件


# 全局实例
conversation_manager = ConversationManager()
