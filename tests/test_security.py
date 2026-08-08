"""安全模块单元测试 (v0.7) - 密码哈希/登录锁定/令牌/知识库隔离/审计"""
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.security import (
    User,
    _DB,
    AuditLogger,
    KBRegistry,
    UserManager,
    get_kb_registry,
    hash_password,
    require_admin,
    require_kb_access,
    verify_password,
)


# ---------------- 工具 ----------------

@pytest.fixture()
def db(tmp_path: Path) -> _DB:
    """独立临时数据库"""
    return _DB(str(tmp_path / "test_security.db"))


@pytest.fixture()
def um(db: _DB) -> UserManager:
    return UserManager(db)


@pytest.fixture()
def registry(db: _DB) -> KBRegistry:
    return KBRegistry(db)


@pytest.fixture()
def audit(db: _DB) -> AuditLogger:
    return AuditLogger(db)


def _mkuser(role: str = "user", username: str = "u") -> User:
    return User(username=username, role=role, display_name=username)


# ---------------- 密码哈希 ----------------

class TestPasswordHash:
    def test_roundtrip(self):
        hashed = hash_password("secret123")
        assert hashed.startswith("pbkdf2$")
        assert verify_password("secret123", hashed)
        assert not verify_password("wrong", hashed)

    def test_random_salt_unique(self):
        assert hash_password("a") != hash_password("a")

    def test_bad_format_rejected(self):
        assert not verify_password("x", "garbage")
        assert not verify_password("x", "md5$salt$digest")
        assert not verify_password("x", "pbkdf2$short$digest")


# ---------------- 用户管理 ----------------

class TestUserManager:
    def test_register_login_me_logout(self, um: UserManager):
        user = um.register("alice", "Abc@12345", "爱丽丝")
        assert user.username == "alice"
        assert user.role == "user"

        ok_user, token = um.login("alice", "Abc@12345")
        assert ok_user is not None and token

        # 令牌有效
        me = um.get_user_by_token(token)
        assert me is not None and me.username == "alice"

        # 登出后令牌失效
        um.logout(token)
        assert um.get_user_by_token(token) is None

    def test_register_validation(self, um: UserManager):
        with pytest.raises(ValueError):
            um.register("a", "Abc@12345")              # 太短
        with pytest.raises(ValueError):
            um.register("bad name!", "Abc@12345")      # 非法字符
        with pytest.raises(ValueError):
            um.register("alice", "123")              # 密码过短
        with pytest.raises(ValueError):
            um.register("admin", "Abc@12345")          # 保留名（默认 admin）
        um.register("bob", "Abc@12345")
        with pytest.raises(ValueError):
            um.register("bob", "Abc@12345")            # 重复

    def test_wrong_password(self, um: UserManager):
        um.register("carol", "Abc@12345")
        user, err = um.login("carol", "wrong")
        assert user is None and err

    def test_lockout_after_max_attempts(self, um: UserManager):
        um.register("dave", "Abc@12345")
        # 前 max_attempts-1 次失败
        for _ in range(um.max_attempts - 1):
            um.login("dave", "bad")
        # 第 max_attempts 次失败 → 锁定
        user, err = um.login("dave", "bad")
        assert user is None and "锁定" in err
        # 锁定期间即使密码正确也拒绝
        user, err = um.login("dave", "Abc@12345")
        assert user is None and "锁定" in err

    def test_no_lockout_when_disabled(self, db: _DB, monkeypatch):
        monkeypatch.setattr("app.core.security.settings.security.max_login_attempts", 0)
        um = UserManager(db)
        um.register("erin", "Abc@12345")
        for _ in range(10):
            um.login("erin", "bad")   # 不锁定
        user, token = um.login("erin", "Abc@12345")
        assert user is not None and token

    def test_token_expiry(self, um: UserManager, monkeypatch):
        um.register("frank", "Abc@12345")
        _, token = um.login("frank", "Abc@12345")
        # 模拟过期
        future_ts = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - 3600 * 48))  # 令牌已"过期"
        um.db.execute("UPDATE users SET token_expires=? WHERE username=?", (future_ts, "frank"))
        assert um.get_user_by_token(token) is None

    def test_change_password(self, um: UserManager):
        um.register("grace", "Old@12345")
        ok, msg = um.change_password("grace", "Old@12345", "New@12345")
        assert ok and not msg
        user, token = um.login("grace", "New@12345")
        assert user is not None
        # 原密码错误
        ok, msg = um.change_password("grace", "wrong", "Xx@12345")
        assert not ok and "原密码" in msg

    def test_bootstrap_admin(self, um: UserManager):
        pwd = um.bootstrap_admin()
        assert pwd  # 生成了随机密码
        user, token = um.login(um.admin_username, pwd)
        assert user is not None and user.is_admin
        # 幂等：再次调用不重复创建
        um.bootstrap_admin()
        assert len(um.db.query("SELECT * FROM users WHERE username=?", (um.admin_username,))) == 1


# ---------------- 知识库隔离 ----------------

class TestKBRegistry:
    def test_create_and_get(self, registry: KBRegistry):
        kb = registry.create("kb_a", "alice")
        assert kb.owner == "alice"
        assert registry.get("kb_a").owner == "alice"
        assert registry.get("missing") is None

    def test_duplicate_rejected(self, registry: KBRegistry):
        registry.create("kb_x", "alice")
        with pytest.raises(ValueError):
            registry.create("kb_x", "bob")

    def test_list_for_isolation(self, registry: KBRegistry):
        registry.create("kb_alice", "alice")
        registry.create("kb_bob", "bob")
        registry.create("kb_admin", "admin")

        alice_names = {kb.name for kb in registry.list_for(_mkuser(username="alice"))}
        assert alice_names == {"kb_alice"}
        admin_names = {kb.name for kb in registry.list_for(_mkuser(role="admin"))}
        assert admin_names == {"kb_alice", "kb_bob", "kb_admin"}

    def test_can_access(self, registry: KBRegistry):
        registry.create("kb_shared", "alice")
        assert registry.can_access("kb_shared", _mkuser(username="alice"))
        assert not registry.can_access("kb_shared", _mkuser(username="bob"))
        assert registry.can_access("kb_shared", _mkuser(role="admin"))
        assert not registry.can_access("missing", _mkuser(username="alice"))
        assert not registry.can_access("missing", _mkuser(role="admin"))

    def test_migrate_existing(self, registry: KBRegistry):
        registry.create("known", "alice")
        registry.migrate_existing(["known", "legacy_a", "legacy_b"], "admin")
        assert registry.get("legacy_a").owner == "admin"
        assert registry.get("known").owner == "alice"  # 不覆盖已有登记
        # 幂等
        registry.migrate_existing(["legacy_a"], "admin")
        assert len(registry.db.query("SELECT * FROM knowledge_bases WHERE name='legacy_a'")) == 1


# ---------------- 审计日志 ----------------

class TestAuditLogger:
    def test_log_and_query(self, audit: AuditLogger):
        audit.log("alice", "auth.login", "", "登录成功")
        audit.log("alice", "document.upload", "kb1", "上传 a.pdf")
        audit.log("bob", "kb.create", "kb2", "创建知识库")

        data = audit.query()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_filters(self, audit: AuditLogger):
        audit.log("alice", "auth.login", "", "")
        audit.log("alice", "document.upload", "kb1", "")
        audit.log("bob", "auth.login", "", "")

        alice = audit.query(user_filter="alice")
        assert alice["total"] == 2
        uploads = audit.query(action_filter="document.upload")
        assert uploads["total"] == 1
        both = audit.query(user_filter="bob", action_filter="auth.login")
        assert both["total"] == 1

    def test_pagination(self, audit: AuditLogger):
        for i in range(25):
            audit.log(f"u{i}", "auth.login", "", "")
        page1 = audit.query(page=1, size=10)
        page3 = audit.query(page=3, size=10)
        assert page1["total"] == 25 and len(page1["items"]) == 10
        assert len(page3["items"]) == 5

    def test_failure_does_not_raise(self, audit: AuditLogger):
        # 超长 detail 截断，不抛异常
        audit.log("alice", "chat.completion", "kb", "x" * 5000)


# ---------------- FastAPI 依赖 ----------------

class TestDependencies:
    def test_get_current_user_no_auth(self, monkeypatch):
        from types import SimpleNamespace
        from app.core.security import get_current_user
        monkeypatch.setattr("app.core.security.settings.security.auth_enabled", False)
        user = get_current_user(SimpleNamespace(state=SimpleNamespace()))  # 无凭据
        assert user.is_admin and user.username == "system"

    def test_get_current_user_missing_credential(self, monkeypatch):
        from types import SimpleNamespace
        from app.core.security import get_current_user
        monkeypatch.setattr("app.core.security.settings.security.auth_enabled", True)
        with pytest.raises(HTTPException) as exc:
            get_current_user(SimpleNamespace(state=SimpleNamespace()), None)  # 无凭据
        assert exc.value.status_code == 401

    def test_require_admin(self):
        admin = require_admin(user=_mkuser(role="admin"))
        assert admin.is_admin
        with pytest.raises(HTTPException) as exc:
            require_admin(user=_mkuser(role="user"))
        assert exc.value.status_code == 403

    def test_require_kb_access(self, registry: KBRegistry, monkeypatch):
        registry.create("kb_p", "alice")
        monkeypatch.setattr("app.core.security.get_kb_registry", lambda: registry)

        # 属主放行
        require_kb_access("kb_p", _mkuser(username="alice"))
        # 管理员放行
        require_kb_access("kb_p", _mkuser(role="admin"))
        # 其他用户拒绝
        with pytest.raises(HTTPException) as exc:
            require_kb_access("kb_p", _mkuser(username="bob"))
        assert exc.value.status_code == 403

    def test_auth_status_endpoint(self, monkeypatch):
        """公开状态端点：前端据此决定登录页 or 免登录直入（内网模式）"""
        from app.core.config import settings
        from app.routers.auth import auth_status
        import asyncio

        monkeypatch.setattr(settings.security, "auth_enabled", True)
        assert asyncio.run(auth_status()) == {"auth_enabled": True}
        monkeypatch.setattr(settings.security, "auth_enabled", False)
        assert asyncio.run(auth_status()) == {"auth_enabled": False}

    def test_bootstrap_reuses_credentials_file(self, db: _DB, monkeypatch, tmp_path):
        """库为空但凭据文件存在时，沿用文件密码，避免文件与 DB 失配"""
        from app.core.security import UserManager, verify_password

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "admin_credentials.txt").write_text(
            "管理员账号: admin\n初始密码: KeepThisPass1\n", encoding="utf-8")

        mgr = UserManager(db)
        pw = mgr.bootstrap_admin()
        assert pw == "KeepThisPass1"
        row = db.query_one("SELECT pass_hash FROM users WHERE username='admin'")
        assert row and verify_password("KeepThisPass1", row["pass_hash"])
        # 已存在时不再重复创建
        assert mgr.bootstrap_admin() == ""
