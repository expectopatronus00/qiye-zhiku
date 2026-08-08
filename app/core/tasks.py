"""异步任务队列 (v1.5 性能与高可用)

- SQLite 持久化任务状态（data/tasks.db，独立于安全库）
- 后台 worker 线程池消费 pending 任务（ThreadPoolExecutor，防阻塞主循环）
- 处理器注册表：register_handler(type, fn)，fn(task_id, params) 由 worker 调用
- 异步处理器（async def）自动用 asyncio.run 包装（嵌入/向量库均为异步接口）

使用：
    from app.core.tasks import task_manager
    task_id = task_manager.submit("document.upload", params, created_by="admin")
    status = task_manager.get(task_id)
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 任务状态机：pending -> running -> success/failed
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class TaskManager:
    """任务持久化 + 后台执行"""

    def __init__(self, db_path: str = "./data/tasks.db", max_workers: int = 2):
        self.db_path = db_path
        self._lock = threading.Lock()  # SQLite 单连接写锁
        self._conn = None
        self._handlers: dict[str, Callable] = {}
        self._executor = None  # 延迟导入 ThreadPoolExecutor
        self._started = False

    # ---------------- 生命周期 ----------------

    def _ensure_conn(self) -> None:
        """懒建立 SQLite 连接（submit 先于 start 时也能落库）"""
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._init_schema()

    def start(self) -> None:
        """启动 worker（main.py startup 调用；重复调用幂等）"""
        if self._started:
            return
        from concurrent.futures import ThreadPoolExecutor
        self._ensure_conn()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task-worker")
        self._started = True
        logger.info("task manager started (db=%s)", self.db_path)

    def shutdown(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None
        if self._conn:
            self._conn.close()
            self._conn = None
        self._started = False

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    params TEXT NOT NULL DEFAULT '{}',
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            self._conn.commit()

    # ---------------- 处理器注册 ----------------

    def register(self, task_type: str, fn: Callable) -> None:
        """注册任务处理器（async def 或普通 def 均可）"""
        self._handlers[task_type] = fn

    def has_handler(self, task_type: str) -> bool:
        return task_type in self._handlers

    # ---------------- 提交与查询 ----------------

    def submit(self, task_type: str, params: dict, created_by: str = "") -> str:
        """提交任务，返回 task_id（写入 pending 队列即返回）"""
        self._ensure_conn()
        task_id = secrets.token_hex(6)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, task_type, status, params, created_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, task_type, STATUS_PENDING, json.dumps(params, ensure_ascii=False),
                 created_by, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._conn.commit()
        if self._started and self._executor:
            self._executor.submit(self._run_task, task_id)
        else:
            logger.warning("task manager 未启动，任务 %s 保持 pending（submit 后需 start）", task_id)
        return task_id

    def get(self, task_id: str) -> Optional[dict]:
        self._ensure_conn()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def list(self, created_by: str = "", status: str = "",
             page: int = 1, size: int = 20) -> dict:
        self._ensure_conn()
        where, params = [], []
        if created_by:
            where.append("created_by=?")
            params.append(created_by)
        if status:
            where.append("status=?")
            params.append(status)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM tasks {cond}", tuple(params)).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM tasks {cond} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params + [size, (page - 1) * size])).fetchall()
        return {"total": total, "page": page, "size": size,
                "items": [self._row_to_dict(r) for r in rows]}

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        cols = ["id", "task_type", "status", "params", "result", "error",
                "created_by", "created_at", "started_at", "finished_at"]
        d = dict(zip(cols, row))
        try:
            d["params"] = json.loads(d["params"] or "{}")
        except ValueError:
            d["params"] = {}
        try:
            d["result"] = json.loads(d["result"] or "{}")
        except ValueError:
            d["result"] = {}
        return d

    # ---------------- 执行 ----------------

    def _run_task(self, task_id: str) -> None:
        """worker 执行入口：pending -> running -> success/failed"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            row = self._conn.execute(
                "SELECT task_type, params FROM tasks WHERE id=? AND status=?",
                (task_id, STATUS_PENDING)).fetchone()
            if not row:
                return  # 已被并发 worker 抢占
            task_type, params_json = row
            self._conn.execute(
                "UPDATE tasks SET status=?, started_at=? WHERE id=?",
                (STATUS_RUNNING, now, task_id))
            self._conn.commit()

        handler = self._handlers.get(task_type)
        error = ""
        if handler is None:
            error = f"无处理器: {task_type}"
        else:
            try:
                params = json.loads(params_json or "{}")
                ret = handler(task_id, params)
                if hasattr(ret, "__await__"):  # async 处理器 → asyncio 事件循环
                    import asyncio
                    ret = asyncio.run(ret)
                result_json = json.dumps(ret or {}, ensure_ascii=False)
                with self._lock:
                    self._conn.execute(
                        "UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
                        (STATUS_SUCCESS, result_json, now, task_id))
                    self._conn.commit()
                logger.info("task %s (%s) success", task_id, task_type)
                return
            except Exception as e:  # noqa: BLE001 - 任务级兜底，任何异常都落 failed
                logger.exception("task %s (%s) failed: %s", task_id, task_type, e)
                error = f"{type(e).__name__}: {e}"
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?",
                (STATUS_FAILED, error[:500], now, task_id))
            self._conn.commit()


# 全局单例
task_manager = TaskManager()
