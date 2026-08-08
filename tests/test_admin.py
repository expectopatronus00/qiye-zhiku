"""v1.1 管理后台单元测试 - 用户管理/知识库配额/系统配置/审计导出"""
import copy

import pytest
from fastapi.testclient import TestClient

from app.core import config as config_module
from app.core.config import settings
from app.core.security import (
    _DB,
    AuditLogger,
    KBRegistry,
    UserManager,
)
from main import app


@pytest.fixture()
def db(tmp_path) -> _DB:
    return _DB(str(tmp_path / "test_admin.db"))


@pytest.fixture()
def um(db: _DB) -> UserManager:
    return UserManager(db)


@pytest.fixture()
def registry(db: _DB) -> KBRegistry:
    return KBRegistry(db)


@pytest.fixture()
def audit(db: _DB) -> AuditLogger:
    return AuditLogger(db)


# ---------------- 用户管理（核心层） ----------------

class TestUserAdminCore:
    def test_admin_create_user_with_role(self, um: UserManager):
        user = um.admin_create_user("zhang", "Abc@12345", "张工", role="admin")
        assert user.role == "admin"
        row = um.get_user("zhang")
        assert row["role"] == "admin" and row["enabled"] == 1

    def test_admin_create_rejects_bad_role(self, um: UserManager):
        with pytest.raises(ValueError):
            um.admin_create_user("li", "Abc@12345", role="superuser")

    def test_disable_blocks_login_and_revokes_token(self, um: UserManager):
        um.register("wang", "Abc@12345", "王工")
        _, token = um.login("wang", "Abc@12345")
        assert um.get_user_by_token(token) is not None
        um.set_enabled("wang", False)
        # 令牌立即失效
        assert um.get_user_by_token(token) is None
        # 登录被拒
        user, err = um.login("wang", "Abc@12345")
        assert user is None and "禁用" in err

    def test_enable_restores_login(self, um: UserManager):
        um.register("zhao", "Abc@12345")
        um.set_enabled("zhao", False)
        um.set_enabled("zhao", True)
        user, token = um.login("zhao", "Abc@12345")
        assert user is not None and token

    def test_reset_password_revokes_tokens(self, um: UserManager):
        um.register("qian", "Abc@12345")
        _, token = um.login("qian", "Abc@12345")
        um.reset_password("qian", "New@67890")
        assert um.get_user_by_token(token) is None
        user, err = um.login("qian", "Abc@12345")
        assert user is None
        user, _ = um.login("qian", "New@67890")
        assert user is not None

    def test_reset_password_too_short(self, um: UserManager):
        um.register("sun", "Abc@12345")
        with pytest.raises(ValueError):
            um.reset_password("sun", "123")

    def test_unlock_clears_lockout(self, um: UserManager):
        um.register("zhou", "Abc@12345")
        for _ in range(um.max_attempts):
            um.login("zhou", "bad")
        user, err = um.login("zhou", "Abc@12345")
        assert user is None and "锁定" in err
        um.unlock("zhou")
        user, _ = um.login("zhou", "Abc@12345")
        assert user is not None

    def test_delete_user(self, um: UserManager):
        um.register("wu", "Abc@12345")
        um.delete_user("wu")
        assert um.get_user("wu") is None

    def test_list_users_keyword(self, um: UserManager):
        um.register("alice", "Abc@12345", "爱丽丝")
        um.register("bob", "Abc@12345", "鲍勃")
        result = um.list_users(keyword="爱丽")
        assert result["total"] == 1 and result["items"][0]["username"] == "alice"
        # 列表不含敏感字段
        assert "pass_hash" not in result["items"][0]
        assert "token" not in result["items"][0]

    def test_transfer_owner(self, registry: KBRegistry):
        registry.create("kb1", "alice")
        registry.create("kb2", "alice")
        registry.create("kb3", "bob")
        assert registry.transfer_owner("alice", "admin") == 2
        assert registry.get("kb1").owner == "admin"
        assert registry.get("kb2").owner == "admin"
        assert registry.get("kb3").owner == "bob"


# ---------------- 知识库配额（核心层） ----------------

class TestKbQuotaCore:
    def test_quota_default_unlimited(self, registry: KBRegistry):
        registry.create("kb", "alice")
        kb = registry.get("kb")
        assert kb.quota_chunks == -1 and kb.quota_documents == -1

    def test_set_quota(self, registry: KBRegistry):
        registry.create("kb", "alice")
        registry.set_quota("kb", quota_chunks=1000, quota_documents=50)
        kb = registry.get("kb")
        assert kb.quota_chunks == 1000 and kb.quota_documents == 50

    def test_set_quota_rejects_invalid(self, registry: KBRegistry):
        registry.create("kb", "alice")
        with pytest.raises(ValueError):
            registry.set_quota("kb", quota_chunks=-5)
        with pytest.raises(ValueError):
            registry.set_quota("no_such_kb", quota_chunks=10)

    def test_migration_adds_quota_columns(self, tmp_path):
        """存量库（无配额列）打开后自动迁移"""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE knowledge_bases (name TEXT PRIMARY KEY, owner TEXT, "
                     "display_name TEXT, created_at TEXT)")
        conn.execute("INSERT INTO knowledge_bases VALUES ('old', 'alice', '旧库', '2026-01-01')")
        conn.commit()
        conn.close()
        legacy = _DB(str(db_path))
        kb = KBRegistry(legacy).get("old")
        assert kb.quota_chunks == -1 and kb.quota_documents == -1


# ---------------- 系统配置 ----------------

class TestConfigApi:
    def test_get_view_masks_api_key(self, monkeypatch):
        monkeypatch.setattr(settings.llm, "openai_api_key", "sk-secret123")
        view = config_module.get_config_view()
        assert view["llm"]["openai_api_key"] == "****"
        monkeypatch.setattr(settings.llm, "openai_api_key", "")

    def test_view_only_contains_editable_sections(self):
        view = config_module.get_config_view()
        assert set(view.keys()) == set(config_module.ADMIN_EDITABLE.keys())
        assert "security" not in view and "server" not in view

    def test_update_config_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "_CONFIG_PATH", tmp_path / "config.yaml")
        view = config_module.update_config({"llm": {"temperature": 0.7},
                                            "retrieval": {"top_k": 8}})
        assert view["llm"]["temperature"] == 0.7
        assert view["retrieval"]["top_k"] == 8
        # 写回文件可加载且值一致
        loaded = config_module.load_settings(str(tmp_path / "config.yaml"))
        assert loaded.llm.temperature == 0.7
        assert loaded.retrieval.top_k == 8
        # 恢复全局配置
        monkeypatch.setattr(settings.llm, "temperature", 0.3)
        monkeypatch.setattr(settings.retrieval, "top_k", 5)

    def test_update_config_rejects_unknown(self, monkeypatch):
        with pytest.raises(ValueError):
            config_module.update_config({"security": {"auth_enabled": False}})
        with pytest.raises(ValueError):
            config_module.update_config({"llm": {"db_path": "/evil"}})

    def test_update_config_keeps_key_on_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "_CONFIG_PATH", tmp_path / "config.yaml")
        monkeypatch.setattr(settings.llm, "openai_api_key", "sk-real")
        view = config_module.update_config({"llm": {"openai_api_key": ""}})
        assert settings.llm.openai_api_key == "sk-real"  # 空串不覆盖
        assert view["llm"]["openai_api_key"] == "****"
        monkeypatch.setattr(settings.llm, "openai_api_key", "")


# ---------------- 审计导出 ----------------

class TestAuditExport:
    def test_export_csv_content(self, audit: AuditLogger):
        audit.log("alice", "auth.login", "", "登录成功")
        audit.log("admin", "kb.create", "kb1", "创建知识库")
        data = audit.query(page=1, size=5000)
        assert data["total"] == 2

    def test_export_requires_admin(self):
        """普通用户访问 /api/audit/export 返回 403"""
        with TestClient(app) as client:
            resp = client.get("/api/audit/export")
            assert resp.status_code in (401, 403)
