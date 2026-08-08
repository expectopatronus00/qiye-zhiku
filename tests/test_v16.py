"""v1.6 进阶能力测试：知识图谱(构建/查询/问答增强) / 多模态 VLM(降级与调用) / Webhook(飞书钉钉通知)"""
import asyncio
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.graph import GraphBuilder
from app.core.vision import VLMCaptioner
from app.core.webhook import WebhookManager, fire_event

# ---------------- 知识图谱构建 ----------------

CHUNK_TEXTS = [
    "华为昆仑服务器搭载昇腾Atlas 300I加速卡，支持GPU监控与显存管理。",
    "寒武纪MLU370与昇腾910芯片均支持国产AI推理，昆仑G8600可搭配BF3加速卡。",
    "摩尔线程S3000与智铠100在训练场景表现良好，紫金山产线已完成适配。",
    "海光DCU与飞腾处理器组合用于昆仑Pod for AI集群，麒麟操作系统提供支持。",
]


@pytest.fixture()
def gb(tmp_path: Path) -> GraphBuilder:
    return GraphBuilder(str(tmp_path / "graph.db"))


class TestGraphBuild:
    def test_build_str_chunks(self, gb: GraphBuilder):
        n = gb.build("kb1", CHUNK_TEXTS)
        assert n > 5
        ents = gb.entities("kb1", limit=100)
        names = {e["name"] for e in ents}
        assert "昇腾" in names and "昆仑" in names and "寒武纪" in names
        assert gb.stats("kb1")["entities"] == n
        assert gb.stats("kb1")["relations"] > 0

    def test_build_dict_and_object_chunks(self, gb: GraphBuilder):
        class _Chunk:
            def __init__(self, c):
                self.content = c

        chunks = [
            {"content": "昆仑G8600服务器支持Atlas 800I加速卡，用于推理场景。"},
            {"content": "智铠100加速卡搭配飞腾处理器，适配麒麟系统。"},
            _Chunk("摩尔线程MUSA生态支持CUDA迁移，S3000显卡适合训练。"),
            _Chunk("昇腾CANN提供统一编程接口，支持vLLM推理框架。"),
        ]
        n = gb.build("kb2", chunks)
        assert n > 0
        names = {e["name"] for e in gb.entities("kb2", limit=100)}
        assert "昆仑" in names and "麒麟" in names and "昇腾" in names

    def test_build_empty_returns_0(self, gb: GraphBuilder):
        assert gb.build("kb", []) == 0
        assert gb.build("", ["文本"]) == 0
        assert gb.build("kb", [""]) == 0

    def test_build_idempotent_rebuild(self, gb: GraphBuilder):
        gb.build("kb", CHUNK_TEXTS)
        n1 = gb.stats("kb")["entities"]
        gb.build("kb", CHUNK_TEXTS)  # 重建不叠加
        n2 = gb.stats("kb")["entities"]
        assert n1 == n2
        # 库隔离：另一库不受影响
        gb.build("kb_other", CHUNK_TEXTS)
        assert gb.stats("kb")["entities"] == n1

    def test_extract_entities_dict_and_jieba(self, gb: GraphBuilder):
        ents = gb.extract_entities("寒武纪MLU370与昇腾910用于昆仑服务器")
        assert "寒武纪" in ents and "昇腾" in ents and "昆仑" in ents

    def test_configure_appends_to_default(self, gb: GraphBuilder):
        gb.configure(["星河二号"])  # 追加自定义词
        ents = gb.extract_entities("星河二号超级计算机采用国产芯片")
        assert "星河二号" in ents
        assert "昇腾" in gb.extract_entities("昇腾910训练芯片")  # 内置词表仍在
        gb.configure([])  # 空配置保持默认
        assert "昇腾" in gb.extract_entities("昇腾910")

    def test_relations_and_related(self, gb: GraphBuilder):
        gb.build("kb", CHUNK_TEXTS)
        rels = gb.relations("kb", "昆仑", limit=50)
        assert rels, "昆仑应存在共现关系"
        others = {r["target"] if r["direction"] == "out" else r["source"] for r in rels}
        assert "昇腾" in others or "寒武纪" in others or "海光" in others
        assert gb.related_entities("kb", "昆仑")  # 邻居实体非空
        assert all(0 < r["weight"] <= 4 for r in rels)

    def test_drop_and_collections(self, gb: GraphBuilder):
        gb.build("kbA", CHUNK_TEXTS)
        gb.build("kbB", CHUNK_TEXTS[:2])
        assert sorted(gb.all_collections()) == ["kbA", "kbB"]
        gb.drop("kbA")
        assert gb.all_collections() == ["kbB"]
        assert gb.stats("kbA") == {"entities": 0, "relations": 0}


# ---------------- 图谱 API ----------------

class _FakeUser:
    username = "admin"
    role = "admin"
    is_admin = True


def test_graph_api_endpoints(monkeypatch, gb: GraphBuilder):
    """4 个图谱端点（mock 权限与全局图谱实例）"""
    gb.build("gk", CHUNK_TEXTS)
    # graph 路由从 security 模块导入（绑定引用）。全量测试时路由已被
    # 其他测试导入绑定旧函数，须先 patch 再 reload 强制重新绑定。
    import importlib
    import app.core.security as sec_mod
    import app.routers.graph as graph_mod
    monkeypatch.setattr(sec_mod, "get_current_user", lambda: _FakeUser())
    monkeypatch.setattr(sec_mod, "require_kb_access", lambda *a, **k: None)
    monkeypatch.setattr(sec_mod, "get_kb_registry", lambda: type(
        "R", (), {"list_for": staticmethod(lambda u: [type("KB", (), {"name": "gk"})()])})())
    importlib.reload(graph_mod)
    monkeypatch.setattr(graph_mod, "graph_builder", gb)

    from fastapi.testclient import TestClient
    client = TestClient(graph_mod.router)
    r = client.get("/collections")
    assert r.status_code == 200 and "gk" in r.json()
    r = client.get("/entities/gk?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["collection"] == "gk" and body["items"]
    assert "昇腾" in {e["name"] for e in body["items"]}
    r = client.get("/relations/gk?entity=%E6%98%87%E8%85%BE")
    assert r.status_code == 200 and r.json()["entity"] == "昇腾"
    r = client.get("/stats/gk")
    assert r.status_code == 200 and r.json()["entities"] > 0


# ---------------- 图谱问答增强 ----------------

async def _fake_graph_enhance(retriever, collection, question):
    from app.routers.chat import _graph_enhance
    return await _graph_enhance(retriever, collection, question)


class _FakeVecStore:
    def search(self, query_embedding, top_k):
        return [{"content": f"实体补充块{i}（来自图谱检索）"} for i in range(top_k)]


class _FakeEmbedder:
    async def embed_query(self, text):
        return [0.1, 0.2]


class _FakeRetriever:
    def __init__(self):
        self.vectorstore = _FakeVecStore()
        self.embedding_service = _FakeEmbedder()


def test_graph_enhance_hits(monkeypatch):
    """问题实体命中库内实体 → 返回 hits 与补充上下文"""
    import app.core.graph as graph_mod

    class _FakeGraph:
        def extract_entities(self, text):
            return ["昇腾", "昆仑", "不存在的实体X"]

        def entities(self, collection, limit=500):
            return [{"name": "昇腾"}, {"name": "昆仑"}]

    monkeypatch.setattr(graph_mod, "graph_builder", _FakeGraph())
    monkeypatch.setattr(settings.graph, "enabled", True)
    monkeypatch.setattr(settings.graph, "qa_context_topk", 2)

    hits, extra = asyncio.run(_fake_graph_enhance(_FakeRetriever(), "kb", "昇腾与昆仑服务器性能如何？"))
    assert hits == ["昇腾", "昆仑"]
    assert "知识图谱补充知识" in extra
    assert "实体补充块" in extra


def test_graph_enhance_disabled_or_miss(monkeypatch):
    """图谱关闭 / 实体未命中 → 空增强不干扰主流程"""
    import app.core.graph as graph_mod

    class _MissGraph:
        def extract_entities(self, text):
            return ["未知实体"]

        def entities(self, collection, limit=500):
            return [{"name": "昇腾"}]

    monkeypatch.setattr(graph_mod, "graph_builder", _MissGraph())
    monkeypatch.setattr(settings.graph, "enabled", True)
    hits, extra = asyncio.run(_fake_graph_enhance(_FakeRetriever(), "kb", "未知实体是什么？"))
    assert hits == [] and extra == ""

    monkeypatch.setattr(settings.graph, "enabled", False)
    hits, extra = asyncio.run(_fake_graph_enhance(_FakeRetriever(), "kb", "昇腾是什么？"))
    assert hits == [] and extra == ""


# ---------------- 多模态 VLM ----------------

class _FakeVLMResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "这是一张GPU利用率折线图，横轴为时间，纵轴为利用率百分比。"}}]}


class _FakeVLMClient:
    def __init__(self):
        self.calls = []

    def post(self, url, json):
        self.calls.append((url, json))
        return _FakeVLMResp()


def test_vlm_unconfigured_returns_none():
    """base_url 为空（未部署 VLM）→ 全部返回 None，纯 OCR 降级"""
    cap = VLMCaptioner(base_url="")
    assert cap.describe_image(b"fakeimg") is None
    assert cap._failed  # 惰性判定失败，后续不再尝试


def test_vlm_success(monkeypatch):
    """配置 VLM 后返回中文图表描述（OpenAI 兼容格式）"""
    cap = VLMCaptioner(base_url="http://vlm:8000/v1", model="qwen2.5-vl:7b")
    fake = _FakeVLMClient()
    monkeypatch.setattr(cap, "_client", fake)
    out = cap.describe_image(b"pngbytes", mime="image/png", context="GPU监控")
    assert out and "GPU利用率" in out
    url, payload = fake.calls[0]
    assert url == "/chat/completions"
    assert payload["model"] == "qwen2.5-vl:7b"
    assert payload["temperature"] == 0.2
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text" and "GPU监控" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vlm_server_error_returns_none(monkeypatch):
    """VLM 调用异常 → 静默降级返回 None"""

    class _ErrClient:
        def post(self, url, json):
            raise ConnectionError("vlm down")

    cap = VLMCaptioner(base_url="http://vlm:8000/v1")
    monkeypatch.setattr(cap, "_client", _ErrClient())
    assert cap.describe_image(b"x") is None


# ---------------- Webhook ----------------

class _FakeWebhookClient:
    def __init__(self):
        self.posts = []

    def post(self, url, json):
        self.posts.append((url, json))
        return _FakeVLMResp()  # code 为 None → 成功


class _FakeFailClient:
    def post(self, url, json):
        raise ConnectionError("boom")


def test_webhook_disabled_or_no_urls():
    wh = WebhookManager(enabled=False, feishu_urls="http://f")
    assert wh.fire("document.uploaded", "t", "b") is False
    wh2 = WebhookManager(enabled=True, feishu_urls="")
    assert wh2.fire("document.uploaded", "t", "b") is False


def test_webhook_dispatch_payloads(monkeypatch):
    """飞书/钉钉各自 payload 格式 + 多 URL 分发"""
    fake = _FakeWebhookClient()
    wh = WebhookManager(feishu_urls="http://feishu1, http://feishu2",
                        dingtalk_urls="http://ding1")
    monkeypatch.setattr(wh, "_get_client", lambda: fake)
    wh._dispatch("上传完成", "文档A已入库，共12块")
    assert len(fake.posts) == 3
    fs = [j for u, j in fake.posts if "feishu" in u]
    dg = [j for u, j in fake.posts if "ding" in u]
    assert fs[0]["msg_type"] == "text" and "上传完成" in fs[0]["content"]["text"]
    assert dg[0]["msgtype"] == "text" and "上传完成" in dg[0]["text"]["content"]


def test_webhook_retry_then_giveup(monkeypatch, caplog):
    """发送失败：重试 1 次后放弃，只记日志不抛出"""
    wh = WebhookManager(feishu_urls="http://f")
    monkeypatch.setattr(wh, "_get_client", lambda: _FakeFailClient())
    assert wh._post("http://f", {"a": 1}) is False
    assert "重试" in caplog.text


def test_webhook_fire_async_thread(monkeypatch):
    """fire() 立即返回 True，实际发送在后台线程"""
    fake = _FakeWebhookClient()
    wh = WebhookManager(feishu_urls="http://f", dingtalk_urls="")
    monkeypatch.setattr(wh, "_get_client", lambda: fake)
    assert wh.fire("document.uploaded", "t", "b") is True
    deadline = time.time() + 3
    while not fake.posts and time.time() < deadline:
        time.sleep(0.02)
    assert fake.posts and "t" in fake.posts[0][1]["content"]["text"]


def test_fire_event_switches(monkeypatch):
    """事件开关关闭 → 不发送"""
    monkeypatch.setattr(settings.webhook, "notify_upload", False)
    monkeypatch.setattr(settings.webhook, "feishu_urls", "http://f")
    monkeypatch.setattr(settings.webhook, "enabled", True)
    assert fire_event("document.uploaded", "t", "b") is False
    monkeypatch.setattr(settings.webhook, "notify_upload", True)
    # 无 URL → 直接 False（不发线程）
    monkeypatch.setattr(settings.webhook, "feishu_urls", "")
    monkeypatch.setattr(settings.webhook, "dingtalk_urls", "")
    assert fire_event("document.uploaded", "t", "b") is False
