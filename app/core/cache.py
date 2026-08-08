"""热门问题缓存 (v1.5 性能与高可用)

LRU + TTL 的问答结果缓存：
- key = (collection, normalized_question)，标准化抹平空白/大小写差异
- 只缓存纯 RAG 完整响应（agent 模式不缓存）
- 知识库内容变更（上传/删除文档）时按库整体失效
- 命中响应带 cached: true 标记，前端可感知

用法：
    from app.core.cache import qa_cache
    qa_cache.get(collection, question) -> dict | None
    qa_cache.set(collection, question, data)
    qa_cache.invalidate(collection)
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict

from app.core.config import settings

_WS = re.compile(r"\s+")


def _normalize(question: str) -> str:
    """问题标准化：折叠空白 + 转小写（中文不受影响）"""
    return _WS.sub(" ", question.strip().lower())


class QACache:
    """线程安全 LRU + TTL 缓存"""

    def __init__(self, maxsize: int | None = None, ttl_seconds: int | None = None,
                 enabled: bool | None = None):
        cfg = settings.cache
        self.enabled = cfg.enabled if enabled is None else enabled
        self.maxsize = cfg.maxsize if maxsize is None else maxsize
        self.ttl = cfg.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._data: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, collection: str, question: str) -> dict | None:
        """命中返回缓存结果，未命中返回 None"""
        if not self.enabled or self.maxsize <= 0:
            return None
        key = (collection, _normalize(question))
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, data = item
            if time.time() - ts > self.ttl:
                del self._data[key]  # TTL 过期惰性淘汰
                return None
            self._data.move_to_end(key)  # LRU 刷新
            return data

    def set(self, collection: str, question: str, data: dict) -> None:
        if not self.enabled or self.maxsize <= 0:
            return
        key = (collection, _normalize(question))
        with self._lock:
            self._data[key] = (time.time(), data)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:  # 超容量淘汰最久未用
                self._data.popitem(last=False)

    def invalidate(self, collection: str) -> None:
        """知识库内容变更时按库整体失效"""
        with self._lock:
            expired = [k for k in self._data if k[0] == collection]
            for k in expired:
                del self._data[k]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._data)


# 全局单例
qa_cache = QACache()
