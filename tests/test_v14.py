"""v1.4 数据安全与等保测试：敏感信息脱敏 / 密码强度策略 / 登录失败告警 / HTTPS"""
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.masker import count_sensitive, mask_sensitive
from app.core.security import (
    _DB,
    AuditLogger,
    UserManager,
    validate_password_strength,
)


# ---------------- 敏感信息脱敏 ----------------

class TestMasker:
    def test_phone(self):
        assert mask_sensitive("联系 13812345678 王工") == "联系 138****5678 王工"

    def test_phone_no_false_positive_short(self):
        assert mask_sensitive("编号 12345") == "编号 12345"

    def test_idcard(self):
        assert mask_sensitive("身份证 110101199001011234") == "身份证 110101********1234"

    def test_bankcard(self):
        # 19 位：保留前 6 后 4，中间 9 星
        assert mask_sensitive("卡号 6222020200112233445") == "卡号 622202*********3445"

    def test_api_key_sk(self):
        # 22 字符：保留前 6 后 4，中间 12 星
        assert mask_sensitive("key=sk-abcdef1234567890xyz") == "key=sk-abc************0xyz"

    def test_token_long(self):
        # 32 字符：保留前 4 后 4，中间 24 星
        out = mask_sensitive("token: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        assert out == "token: a1b2************************c5d6"

    def test_email(self):
        assert mask_sensitive("邮箱 zhangsan@example.com") == "邮箱 zh*****n@example.com"

    def test_multiple_rules_in_text(self):
        text = "手机13812345678，身份证110101199001011234，邮箱 abcdef@b.cn"
        out = mask_sensitive(text)
        assert "138****5678" in out and "110101********1234" in out
        assert "ab***f@b.cn" in out  # 用户名保留前 2 后 1

    def test_plain_text_untouched(self):
        text = "昆仑服务器 GPU 监控指南，第 3 章"
        assert mask_sensitive(text) == text

    def test_count_sensitive(self):
        assert count_sensitive("手机13812345678 与 13900001111") == 2

    def test_empty_input(self):
        assert mask_sensitive("") == ""
        assert mask_sensitive(None) == ""


# ---------------- 密码强度策略 ----------------

class TestPasswordStrength:
    def test_ok_password(self):
        assert validate_password_strength("Abc@12345", "alice") is None

    def test_too_short(self):
        err = validate_password_strength("Ab1@345", "alice")
        assert err and "长度" in err

    def test_weak_blacklist(self):
        for weak in ("12345678", "admin123", "password", "qwerty123"):
            assert validate_password_strength(weak, "alice")

    def test_low_complexity(self):
        # 仅小写+数字 2 类
        err = validate_password_strength("abcdefg123", "alice")
        assert err and "复杂度" in err

    def test_same_as_username(self):
        assert validate_password_strength("Alice123!", "Alice123!") is not None

    def test_contains_username(self):
        err = validate_password_strength("alice@2024", "alice")
        assert err and "用户名" in err

    def test_same_as_old(self):
        err = validate_password_strength("Abc@12345", "alice", "Abc@12345")
        assert err and "原密码" in err


class TestPasswordPolicyIntegration:
    @pytest.fixture()
    def db(self, tmp_path: Path) -> _DB:
        return _DB(str(tmp_path / "test_v14.db"))

    @pytest.fixture()
    def um(self, db: _DB) -> UserManager:
        return UserManager(db)

    def test_register_weak_rejected(self, um):
        with pytest.raises(ValueError):
            um.register("alice", "12345678")

    def test_register_ok(self, um):
        um.register("alice", "Abc@12345")
        user, token = um.login("alice", "Abc@12345")
        assert user is not None

    def test_change_password_weak_rejected(self, um):
        um.register("alice", "Abc@12345")
        ok, msg = um.change_password("alice", "Abc@12345", "12345678")
        assert not ok and "密码" in msg
        # 原密码仍可登录
        user, _ = um.login("alice", "Abc@12345")
        assert user is not None

    def test_reset_password_weak_rejected(self, um):
        um.register("alice", "Abc@12345")
        with pytest.raises(ValueError):
            um.reset_password("alice", "password")


# ---------------- 登录失败告警 ----------------

class TestLoginAlert:
    @pytest.fixture()
    def db(self, tmp_path: Path) -> _DB:
        return _DB(str(tmp_path / "test_v14_alert.db"))

    @pytest.fixture()
    def um(self, db: _DB) -> UserManager:
        return UserManager(db)

    @pytest.fixture()
    def audit(self, db: _DB) -> AuditLogger:
        return AuditLogger(db)

    def test_alert_at_threshold(self, um, audit):
        um.register("alice", "Abc@12345")
        # 前 2 次失败不告警
        um.login("alice", "bad1")
        rows = audit.query(action_filter="security.alert")["items"]
        assert len(rows) == 0
        um.login("alice", "bad2")
        # 第 3 次失败触发告警（阈值 3）
        um.login("alice", "bad3")
        rows = audit.query(action_filter="security.alert")["items"]
        assert len(rows) == 1
        assert rows[0]["action"] == "security.alert"
        assert rows[0]["user"] == "alice"
        # 继续失败不重复告警（防刷屏）
        um.login("alice", "bad4")
        assert len(audit.query(action_filter="security.alert")["items"]) == 1

    def test_alert_on_lockout(self, um, audit):
        um.register("bob", "Abc@12345")
        for _ in range(um.max_attempts):  # 5 次触发锁定
            um.login("bob", "bad")
        rows = audit.query(action_filter="security.alert")["items"]
        assert any("锁定" in r["detail"] for r in rows)

    def test_alert_threshold_zero_disabled(self, um, audit, monkeypatch):
        monkeypatch.setattr(settings.security, "login_alert_threshold", 0)
        um.register("carol", "Abc@12345")
        for _ in range(3):
            um.login("carol", "bad")
        assert len(audit.query(action_filter="security.alert")["items"]) == 0

    def test_success_login_no_alert(self, um, audit):
        um.register("dave", "Abc@12345")
        um.login("dave", "Abc@12345")
        assert len(audit.query(action_filter="security.alert")["items"]) == 0


# ---------------- HTTPS ----------------

class TestHTTPS:
    def test_server_config_ssl_default_empty(self):
        assert settings.server.ssl_certfile == ""
        assert settings.server.ssl_keyfile == ""

    def test_main_passes_ssl_to_uvicorn(self, monkeypatch, tmp_path):
        import main
        calls = {}

        def fake_run(*args, **kwargs):
            calls["kwargs"] = kwargs

        monkeypatch.setattr("main.uvicorn.run", fake_run)
        monkeypatch.setattr(settings.server, "ssl_certfile", "certs/server.crt")
        monkeypatch.setattr(settings.server, "ssl_keyfile", "certs/server.key")
        monkeypatch.setattr(settings.server, "debug", False)
        main.main()
        assert calls["kwargs"]["ssl_certfile"] == "certs/server.crt"
        assert calls["kwargs"]["ssl_keyfile"] == "certs/server.key"

    def test_main_http_when_ssl_empty(self, monkeypatch):
        import main
        calls = {}

        def fake_run(*args, **kwargs):
            calls["kwargs"] = kwargs

        monkeypatch.setattr("main.uvicorn.run", fake_run)
        monkeypatch.setattr(settings.server, "ssl_certfile", "")
        monkeypatch.setattr(settings.server, "ssl_keyfile", "")
        monkeypatch.setattr(settings.server, "debug", False)
        main.main()
        assert "ssl_certfile" not in calls["kwargs"]

    def test_gen_cert_script_runs(self, tmp_path):
        """自签证书脚本：cryptography 或系统 openssl 任一可用即应生成证书文件"""
        import subprocess
        import sys
        out_dir = tmp_path / "certs"
        cmd = [sys.executable, "scripts/gen_self_signed_cert.py",
               "--dir", str(out_dir), "--cn", "127.0.0.1"]
        try:
            subprocess.run(cmd, cwd=str(Path(__file__).parent.parent),
                           check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, FileNotFoundError, TimeoutExpired) as e:
            pytest.skip(f"证书生成环境不可用: {e}")
        assert (out_dir / "server.crt").exists()
        assert (out_dir / "server.key").exists()
        assert (out_dir / "config-add.txt").exists()
