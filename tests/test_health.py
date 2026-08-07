"""健康检查单元测试 (v1.0) - /healthz 存活 + /readyz 依赖探测 + 降级语义"""
import pytest
from fastapi.testclient import TestClient

from app.routers import health
from main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


class TestHealthz:
    def test_liveness_ok(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["ts"]

    def test_legacy_health(self, client, monkeypatch):
        async def fake_llm():
            return {"ok": True, "ms": 1}
        monkeypatch.setattr(health, "_check_llm", fake_llm)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["llm"] == "ok"


class TestReadyz:
    def test_all_deps_ok(self, client, monkeypatch):
        async def fake_llm():
            return {"ok": True, "ms": 5}

        monkeypatch.setattr(health, "_check_vectorstore", lambda: {"ok": True, "ms": 3})
        monkeypatch.setattr(health, "_check_database", lambda: {"ok": True, "ms": 3})
        monkeypatch.setattr(health, "_check_llm", fake_llm)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["vectorstore"]["ok"]
        assert body["checks"]["database"]["ok"]
        assert body["checks"]["llm"]["ok"]

    def test_llm_down_degrades_but_200(self, client, monkeypatch):
        """LLM 不可用 → status=degraded，但核心可用仍 200（文档管理等功能不受影响）"""
        async def fake_llm():
            return {"ok": False, "ms": 2, "error": "connect timeout"}

        monkeypatch.setattr(health, "_check_vectorstore", lambda: {"ok": True, "ms": 3})
        monkeypatch.setattr(health, "_check_database", lambda: {"ok": True, "ms": 3})
        monkeypatch.setattr(health, "_check_llm", fake_llm)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"
        assert "llm" in resp.json()["checks"]

    def test_core_down_returns_503(self, client, monkeypatch):
        """向量库不可用 → 503 不可服务"""
        async def fake_llm():
            return {"ok": True, "ms": 5}

        monkeypatch.setattr(health, "_check_vectorstore",
                            lambda: {"ok": False, "ms": 3, "error": "chroma down"})
        monkeypatch.setattr(health, "_check_database", lambda: {"ok": True, "ms": 3})
        monkeypatch.setattr(health, "_check_llm", fake_llm)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"

    def test_real_checks_no_crash(self, client):
        """真实探测不崩溃（CI 无 Ollama 时 LLM 应标记不可用但核心仍 200）"""
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert set(resp.json()["checks"].keys()) == {"vectorstore", "database", "llm"}
