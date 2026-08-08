"""v1.5 性能与高可用测试：异步任务队列 / 大文档上传异步化 / Prometheus 指标 / 热门问题缓存 / Redis 会话共享"""
import sys
import time
from pathlib import Path

import pytest

from app.core.cache import QACache
from app.core.config import settings
from app.core.metrics import record_duration, record_request, render_metrics
from app.core.security import User, UserManager, _DB
from app.core.session import RedisSessionStore, SQLiteSessionStore, get_session_store
from app.core.tasks import TaskManager


# ---------------- 异步任务队列 ----------------

def _sync_handler(task_id: str, params: dict) -> dict:
    return {"echo": params.get("x", ""), "done": True}


async def _async_handler(task_id: str, params: dict) -> dict:
    return {"async": True, "x": params.get("x", "")}


@pytest.fixture()
def tm(tmp_path: Path) -> TaskManager:
    mgr = TaskManager(db_path=str(tmp_path / "tasks.db"), max_workers=2)
    mgr.start()
    yield mgr
    mgr.shutdown()


class TestTaskManager:
    def test_submit_and_success(self, tm: TaskManager):
        tm.register("echo", _sync_handler)
        task_id = tm.submit("echo", {"x": "hello"}, created_by="alice")
        assert task_id and len(task_id) == 12
        # 轮询等待完成
        for _ in range(50):
            t = tm.get(task_id)
            if t["status"] in ("success", "failed"):
                break
            time.sleep(0.02)
        assert t["status"] == "success"
        assert t["result"] == {"echo": "hello", "done": True}
        assert t["created_by"] == "alice"
        assert t["params"] == {"x": "hello"}
        assert t["started_at"] and t["finished_at"]

    def test_unregistered_handler_failed(self, tm: TaskManager):
        task_id = tm.submit("no_such_type", {})
        for _ in range(50):
            t = tm.get(task_id)
            if t["status"] in ("success", "failed"):
                break
            time.sleep(0.02)
        assert t["status"] == "failed"
        assert "无处理器" in t["error"]

    def test_async_handler_wrapped(self, tm: TaskManager):
        tm.register("async_job", _async_handler)
        task_id = tm.submit("async_job", {"x": 1})
        for _ in range(50):
            t = tm.get(task_id)
            if t["status"] in ("success", "failed"):
                break
            time.sleep(0.02)
        assert t["status"] == "success"
        assert t["result"] == {"async": True, "x": 1}

    def test_handler_exception_failed(self, tm: TaskManager):
        def boom(task_id, params):
            raise ValueError("boom")

        tm.register("boom", boom)
        task_id = tm.submit("boom", {})
        for _ in range(50):
            t = tm.get(task_id)
            if t["status"] in ("success", "failed"):
                break
            time.sleep(0.02)
        assert t["status"] == "failed"
        assert "ValueError" in t["error"]

    def test_list_filter_pagination(self, tm: TaskManager):
        tm.register("echo", _sync_handler)
        for i in range(3):
            tm.submit("echo", {"x": i}, created_by="alice")
        tm.submit("echo", {"x": 9}, created_by="bob")
        for _ in range(50):
            if tm.list(status="success")["total"] >= 4:
                break
            time.sleep(0.02)
        all_tasks = tm.list()["items"]
        assert len(all_tasks) == 4
        alice = tm.list(created_by="alice")
        assert alice["total"] == 3
        page = tm.list(size=2, page=1)
        assert len(page["items"]) == 2 and page["total"] == 4

    def test_get_unknown_returns_none(self, tm: TaskManager):
        assert tm.get("nonexistent") is None

    def test_submit_before_start_keeps_pending(self, tmp_path: Path):
        mgr = TaskManager(db_path=str(tmp_path / "t2.db"))
        task_id = mgr.submit("echo", {})  # 未 start：保持 pending
        assert mgr.get(task_id)["status"] == "pending"
        mgr.start()
        mgr.register("echo", _sync_handler)
        mgr.shutdown()


# ---------------- 上传异步化分支 ----------------

class TestUploadAsyncBranch:
    async def test_small_file_sync_path(self, monkeypatch):
        """小文件（≤阈值）走同步路径，返回 status=success"""
        from app.routers import documents as docs

        monkeypatch.setattr(docs, "require_kb_access", lambda *a, **k: None)  # 跳过权限
        calls = {"process": 0}
        real_process = docs._process_upload

        async def fake_process(save_path, collection_name, username, file_id, filename):
            calls["process"] += 1
            return 7

        monkeypatch.setattr(docs, "_process_upload", fake_process)
        monkeypatch.setattr(settings.document, "async_upload_threshold", 5 * 1024 * 1024)

        class FakeFile:
            filename = "small.txt"
            async def read(self):
                return b"x" * 100  # 100B < 5MB

        class FakeUser:
            username = "alice"

        resp = await docs.upload_document(file=FakeFile(), collection_name="kb1", user=FakeUser())
        assert resp.status == "success" and resp.chunks_count == 7
        assert calls["process"] == 1
        assert real_process is not None  # 原函数存在

    async def test_large_file_async_path(self, monkeypatch, tmp_path):
        """大文件（>阈值）转后台任务，立即返回 accepted + task_id"""
        from app.routers import documents as docs

        monkeypatch.setattr(docs, "require_kb_access", lambda *a, **k: None)  # 跳过权限
        monkeypatch.setattr(settings.document, "async_upload_threshold", 50)  # 50B 阈值
        monkeypatch.setattr(settings.document, "upload_directory", str(tmp_path))
        submitted = {"task_id": "abc123"}

        class FakeTM:
            def submit(self, task_type, params, created_by=""):
                submitted["params"] = params
                submitted["type"] = task_type
                return submitted["task_id"]

        monkeypatch.setattr(docs.task_manager, "submit", FakeTM().submit)

        class FakeFile:
            filename = "big.pdf"
            async def read(self):
                return b"y" * 5000  # 5KB > 50B

        class FakeUser:
            username = "alice"

        resp = await docs.upload_document(file=FakeFile(), collection_name="kb1", user=FakeUser())
        assert resp.status == "accepted"
        assert resp.task_id == "abc123"
        assert submitted["type"] == "document.upload"
        assert submitted["params"]["filename"] == "big.pdf"
        assert "save_path" in submitted["params"]

    def test_async_handler_failure_cleanup(self, tmp_path):
        """异步处理器：文件已删场景返回 failed 结果且不抛异常"""
        from app.routers import documents as docs

        result = docs._async_upload_handler("t1", {
            "save_path": str(tmp_path / "ghost.pdf"),  # 文件不存在
            "collection_name": "kb1", "username": "u",
            "file_id": "f1", "filename": "ghost.pdf",
        })
        assert result["status"] == "failed"
        assert "error" in result


# ---------------- Prometheus 指标 ----------------

class TestMetrics:
    def _reset(self):
        from app.core import metrics as m
        m._http_total.clear()
        m._http_buckets.clear()
        m._http_sum.clear()
        m._http_count.clear()
        m._dur_buckets.clear()
        m._dur_sum.clear()
        m._dur_count.clear()

    def test_record_and_render_http(self):
        self._reset()
        record_request("GET", "/api/health", 200, 0.05)
        record_request("GET", "/api/health", 200, 0.2)
        record_request("POST", "/api/chat/completions", 500, 1.5)
        out = render_metrics()
        assert 'http_requests_total{method="GET",path="/api/health",status="200"} 2' in out
        assert 'http_requests_total{method="POST",path="/api/chat/completions",status="500"} 1' in out
        assert 'http_request_duration_seconds_bucket{method="GET",path="/api/health",le="0.05"} 1' in out
        assert 'http_request_duration_seconds_bucket{method="GET",path="/api/health",le="+Inf"} 2' in out
        assert 'http_request_duration_seconds_sum{method="GET",path="/api/health"} 0.250000' in out
        assert 'http_request_duration_seconds_count{method="GET",path="/api/health"} 2' in out
        assert "qiye_zhiku_uptime_seconds" in out

    def test_record_duration(self):
        self._reset()
        record_duration("retrieval", 0.03)
        record_duration("llm", 2.0)
        out = render_metrics()
        assert 'retrieval_duration_seconds_bucket{name="retrieval",le="0.05"} 1' in out
        assert 'llm_duration_seconds_bucket{name="llm",le="2.5"} 1' in out
        assert 'retrieval_duration_seconds_count{name="retrieval"} 1' in out

    def test_empty_metrics(self):
        self._reset()
        out = render_metrics()
        assert "http_requests_total" in out  # HELP 头存在
        assert "llm_duration_seconds_bucket" not in out  # 无数据不输出


# ---------------- 热门问题缓存 ----------------

class TestQACache:
    def test_set_get_hit(self):
        c = QACache(maxsize=10, ttl_seconds=3600, enabled=True)
        c.set("kb1", "什么是GPU监控？", {"answer": "A"})
        assert c.get("kb1", "什么是GPU监控？") == {"answer": "A"}

    def test_normalized_key(self):
        """空白/大小写标准化后命中"""
        c = QACache(maxsize=10, ttl_seconds=3600, enabled=True)
        c.set("kb1", "  什么是  GPU  监控？  ", {"answer": "A"})
        assert c.get("kb1", "什么是 gpu 监控？") == {"answer": "A"}

    def test_miss_on_different_kb(self):
        c = QACache(maxsize=10, ttl_seconds=3600, enabled=True)
        c.set("kb1", "问题", {"answer": "A"})
        assert c.get("kb2", "问题") is None

    def test_ttl_expired(self):
        c = QACache(maxsize=10, ttl_seconds=3600, enabled=True)
        c.set("kb1", "问题", {"answer": "A"})
        with c._lock:
            key = ("kb1", "问题")
            ts, data = c._data[key]
            c._data[key] = (ts - 7200, data)  # 伪造过期时间
        assert c.get("kb1", "问题") is None

    def test_lru_eviction(self):
        c = QACache(maxsize=2, ttl_seconds=3600, enabled=True)
        c.set("kb1", "q1", {"a": 1})
        c.set("kb1", "q2", {"a": 2})
        c.get("kb1", "q1")  # 刷新 q1 为最近使用
        c.set("kb1", "q3", {"a": 3})  # 淘汰 q2
        assert c.get("kb1", "q2") is None
        assert c.get("kb1", "q1") == {"a": 1}
        assert c.get("kb1", "q3") == {"a": 3}

    def test_invalidate_by_collection(self):
        c = QACache(maxsize=10, ttl_seconds=3600, enabled=True)
        c.set("kb1", "q1", {"a": 1})
        c.set("kb2", "q1", {"a": 2})
        c.invalidate("kb1")
        assert c.get("kb1", "q1") is None
        assert c.get("kb2", "q1") == {"a": 2}

    def test_disabled(self):
        c = QACache(maxsize=10, ttl_seconds=3600, enabled=False)
        c.set("kb1", "q1", {"a": 1})
        assert c.get("kb1", "q1") is None


# ---------------- Redis 会话共享 ----------------

class _FakeRedis:
    """内存版 redis 客户端 mock"""

    def __init__(self):
        self.data = {}

    def setex(self, key, seconds, value):
        self.data[key] = (seconds, value)

    def get(self, key):
        item = self.data.get(key)
        if not item:
            return None
        return item[1].encode("utf-8")

    def delete(self, key):
        self.data.pop(key, None)


class TestRedisSession:
    def test_redis_store_save_get_delete(self):
        store = RedisSessionStore("redis://localhost:6379/0")
        store._client = _FakeRedis()  # 注入 fake 客户端
        store.save("alice", "tok123", 3600)
        assert store.get_username("tok123") == "alice"
        assert store.get_username("nope") is None
        store.delete("tok123")
        assert store.get_username("tok123") is None

    def test_sqlite_store_noop(self):
        store = SQLiteSessionStore()
        store.save("alice", "t", 3600)
        assert store.get_username("t") is None  # 回退标记
        store.delete("t")  # 不抛异常

    def test_get_session_store_sqlite_default(self, monkeypatch):
        monkeypatch.setattr(settings.security, "redis_url", "")
        assert isinstance(get_session_store(), SQLiteSessionStore)

    def test_redis_import_missing_fallback(self, monkeypatch):
        """未安装 redis 包 → 回退 SQLite 语义（可用性为 False）"""
        monkeypatch.setitem(sys.modules, "redis", None)  # import redis 抛 ImportError
        store = RedisSessionStore("redis://localhost:6379/0")
        assert store.available is False
        store.save("a", "t", 3600)  # 不抛异常
        assert store.get_username("t") is None

    def test_user_manager_redis_auth_flow(self, tmp_path, monkeypatch):
        """登录后经 Redis 会话可恢复用户；登出后失效（SQLite 双写仍保留）"""
        monkeypatch.setattr(settings.security, "redis_url", "redis://fake:6379/0")
        db = _DB(str(tmp_path / "sec.db"))
        fake = _FakeRedis()
        store = RedisSessionStore("redis://fake:6379/0")
        store._client = fake
        um = UserManager(db, session_store=store)
        um.register("alice", "Abc@12345")
        user, token = um.login("alice", "Abc@12345")
        assert user is not None and token
        assert fake.data.get(f"session:{token}") is not None

        # Redis 命中路径（不依赖 SQLite token 列）
        got = um.get_user_by_token(token)
        assert got is not None and got.username == "alice"

        um.logout(token)
        assert fake.data.get(f"session:{token}") is None

    def test_redis_failure_fallback_sqlite(self, tmp_path, monkeypatch):
        """Redis 读取故障（抛异常）→ 回退 SQLite 查询仍可认证"""
        db = _DB(str(tmp_path / "sec.db"))
        um = UserManager(db, session_store=SQLiteSessionStore())
        um.register("bob", "Abc@12345")
        user, token = um.login("bob", "Abc@12345")
        assert um.get_user_by_token(token) is not None  # SQLite 原路径
