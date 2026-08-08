"""知识图谱模块 (v1.6 进阶能力)

- 实体抽取：jieba 词性（nr 人名/ns 地名/nt 机构/nz 专名）+ 领域词典（信创 GPU/厂商词表，可配置）
- 关系构建：同一文档块内实体共现 → 无向边加权（weight 累加）
- 存储：SQLite data/graph.db（entities / relations 表，按 collection 隔离）
- 上传入库时自动建图（document._process_upload 挂载，graph.enabled 可关）

用法：
    from app.core.graph import graph_builder
    graph_builder.build(collection_name, chunks)          # 建图（幂等：按文档重建）
    graph_builder.entities(collection_name, limit=50)     # 实体列表
    graph_builder.relations(collection_name, entity)      # 实体关系
    graph_builder.extract_entities(text)                  # 文本实体抽取（问答增强用）
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 默认领域词典（信创 GPU/服务器生态，随 graph.entity_dict 可扩展）
DEFAULT_ENTITY_DICT = [
    "昇腾", "寒武纪", "摩尔线程", "海光", "飞腾", "鲲鹏", "龙芯", "兆芯", "昆仑", "麒麟",
    "统信", "Atlas", "Atlas 300I", "Atlas 800I", "Atlas 900", "华为", "英伟达", "NVIDIA",
    "AMD", "Intel", "GPU", "CPU", "NPU", "FPGA", "TDP", "PCIe", "NVLink", "显存",
    "CUDA", "ROCm", "vLLM", "Ollama", "ChromaDB", "Milvus", "RAG", "向量库", "知识库",
    "CANN", "MLU", "MUSA", "DCU", "BF3", "S3000", "S60G", "X6000", "智铠", "BM1684",
]

# 过滤单字/纯数字/停用词
_STOP_WORDS = {"的", "了", "和", "与", "是", "在", "有", "为", "等", "及", "或", "中"}
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _is_valid_entity(name: str) -> bool:
    name = name.strip()
    if len(name) < 2 or len(name) > 24:
        return False
    if name in _STOP_WORDS:
        return False
    if _WORD_RE.fullmatch(name) and len(name) < 3:  # 纯短字母数字（如 "GPU" 词典词除外）
        return False
    return True


class GraphBuilder:
    """知识图谱构建与查询（线程安全，SQLite 单连接加锁）"""

    def __init__(self, db_path: str = "./data/graph.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = None
        self._entity_dict: set[str] = set(DEFAULT_ENTITY_DICT)

    # ---------------- 生命周期 ----------------

    def _ensure_conn(self) -> None:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            with self._lock:
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS entities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        collection TEXT NOT NULL,
                        name TEXT NOT NULL,
                        etype TEXT NOT NULL DEFAULT 'term',
                        count INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(collection, name)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS relations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        collection TEXT NOT NULL,
                        source TEXT NOT NULL,
                        target TEXT NOT NULL,
                        weight INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(collection, source, target)
                    )"""
                )
                self._conn.commit()

    def configure(self, entity_dict: list[str] | None = None) -> None:
        """扩展领域词典：配置项追加到内置信创词表（默认空列表保持内置词表）"""
        if entity_dict:
            self._entity_dict = set(DEFAULT_ENTITY_DICT) | set(entity_dict)

    # ---------------- 实体抽取 ----------------

    def extract_entities(self, text: str) -> list[str]:
        """从文本抽取实体：领域词典优先 + jieba 专名词性"""
        if not text:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for word in self._entity_dict:  # 词典精确匹配
            if word and word in text and word not in seen:
                found.append(word)
                seen.add(word)
        try:
            import jieba.posseg as pseg
            for w, flag in pseg.cut(text):
                name = w.strip()
                if (flag in ("nr", "ns", "nt", "nz") and _is_valid_entity(name)
                        and name not in seen):
                    found.append(name)
                    seen.add(name)
        except Exception:  # noqa: BLE001 - jieba 异常不影响词典抽取
            logger.warning("jieba 实体抽取失败，仅用领域词典")
        return found

    # ---------------- 构建 ----------------

    def build(self, collection: str, chunks: list) -> int:
        """对一批文本块建图（幂等：先清该库旧图再重建）；返回实体数

        chunks 元素支持 str / 对象（.content）/ dict（content 键）
        """
        if not collection or not chunks:
            return 0
        texts: list[str] = []
        for c in chunks:
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, dict):
                texts.append(c.get("content") or "")
            else:
                texts.append(getattr(c, "content", None) or "")
        texts = [t for t in texts if t]
        # 实体计数
        counter: dict[str, int] = {}
        for t in texts:
            for name in self.extract_entities(t):
                counter[name] = counter.get(name, 0) + 1
        if not counter:
            return 0
        # 共现关系（块内两两共现 +1）
        pair_weight: dict[tuple[str, str], int] = {}
        for t in texts:
            ents = self.extract_entities(t)
            for i in range(len(ents)):
                for j in range(i + 1, len(ents)):
                    a, b = ents[i], ents[j]
                    if a == b:
                        continue
                    key = (a, b) if a < b else (b, a)
                    pair_weight[key] = pair_weight.get(key, 0) + 1
        self._ensure_conn()
        with self._lock:
            self._conn.execute("DELETE FROM entities WHERE collection=?", (collection,))
            self._conn.execute("DELETE FROM relations WHERE collection=?", (collection,))
            self._conn.executemany(
                "INSERT INTO entities (collection, name, etype, count) VALUES (?,?,?,?)",
                [(collection, name, "term", n) for name, n in counter.items()])
            self._conn.executemany(
                "INSERT INTO relations (collection, source, target, weight) VALUES (?,?,?,?)",
                [(collection, a, b, w) for (a, b), w in pair_weight.items()])
            self._conn.commit()
        logger.info("graph build %s: %d entities, %d relations",
                    collection, len(counter), len(pair_weight))
        return len(counter)

    def drop(self, collection: str) -> None:
        """删除知识库时清理图谱"""
        self._ensure_conn()
        with self._lock:
            self._conn.execute("DELETE FROM entities WHERE collection=?", (collection,))
            self._conn.execute("DELETE FROM relations WHERE collection=?", (collection,))
            self._conn.commit()

    # ---------------- 查询 ----------------

    def entities(self, collection: str, limit: int = 50) -> list[dict]:
        self._ensure_conn()
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, etype, count FROM entities WHERE collection=? "
                "ORDER BY count DESC LIMIT ?", (collection, limit)).fetchall()
        return [{"name": r[0], "type": r[1], "count": r[2]} for r in rows]

    def relations(self, collection: str, entity: str, limit: int = 30) -> list[dict]:
        """查询与某实体直接相连的关系（含关系权重）"""
        self._ensure_conn()
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, target, weight FROM relations WHERE collection=? "
                "AND (source=? OR target=?) ORDER BY weight DESC LIMIT ?",
                (collection, entity, entity, limit)).fetchall()
        out = []
        for s, t, w in rows:
            out.append({
                "source": s, "target": t, "weight": w,
                "direction": "out" if s == entity else "in",
            })
        return out

    def related_entities(self, collection: str, entity: str, limit: int = 10) -> list[str]:
        """与实体直接相连的邻居实体名列表（图谱问答上下文注入用）"""
        rels = self.relations(collection, entity, limit=limit)
        names = set()
        for r in rels:
            names.add(r["target"] if r["source"] == entity else r["source"])
        return list(names)[:limit]

    def stats(self, collection: str) -> dict:
        self._ensure_conn()
        with self._lock:
            e = self._conn.execute(
                "SELECT COUNT(*) FROM entities WHERE collection=?", (collection,)).fetchone()[0]
            r = self._conn.execute(
                "SELECT COUNT(*) FROM relations WHERE collection=?", (collection,)).fetchone()[0]
        return {"entities": e, "relations": r}

    def all_collections(self) -> list[str]:
        """有图谱的知识库列表（前端面板入口用）"""
        self._ensure_conn()
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT collection FROM entities").fetchall()
        return [r[0] for r in rows]


# 全局单例
graph_builder = GraphBuilder()
