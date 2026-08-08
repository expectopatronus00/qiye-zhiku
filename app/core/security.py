"""安全模块 - 多用户认证 + 知识库权限隔离 + 操作审计 (Day 7, v0.7)

- 用户认证: PBKDF2-HMAC-SHA256 密码哈希（600k 迭代 + 随机盐）、
  不透明会话令牌（secrets.token_urlsafe），支持连续失败锁定
- 知识库隔离: SQLite 登记每个知识库的属主，普通用户仅可见/可用
  自己创建的知识库，管理员可见全部
- 审计日志: 登录/登出/上传/删除/问答等关键操作全部留痕，
  仅管理员可查询
- 降级模式: security.auth_enabled=false 时返回内置 system 管理员，
  兼容内网直连场景
- v1.4: 密码强度策略（等保 2.0 三级）+ 登录失败告警（security.alert）

所有数据库操作使用单连接 + 线程锁（写操作毫秒级，无性能压力）。
"""
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 600_000


# ---------------- 数据结构 ----------------

@dataclass
class User:
    """当前登录用户"""
    username: str
    role: str = "user"          # user | admin
    display_name: str = ""
    enabled: bool = True

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class KnowledgeBase:
    """知识库归属记录（v1.1 含配额，-1 表示不限制）"""
    name: str
    owner: str
    display_name: str = ""
    quota_chunks: int = -1
    quota_documents: int = -1
    created_at: str = ""


# ---------------- 密码哈希 ----------------

def hash_password(password: str, salt: Optional[str] = None) -> str:
    """PBKDF2-HMAC-SHA256 哈希，格式: pbkdf2$salt$digest"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码（常数时间比较防时序攻击）"""
    try:
        prefix, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if prefix != "pbkdf2" or len(salt) != 32 or len(digest) != 64:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


# ---------------- 单连接 SQLite ----------------

class _DB:
    """线程安全的 SQLite 封装"""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username    TEXT PRIMARY KEY,
                    role        TEXT NOT NULL DEFAULT 'user',
                    display_name TEXT NOT NULL DEFAULT '',
                    pass_hash   TEXT NOT NULL,
                    token       TEXT,
                    token_expires TEXT,
                    fail_count  INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    name         TEXT PRIMARY KEY,
                    owner        TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    quota_chunks INTEGER NOT NULL DEFAULT -1,
                    quota_documents INTEGER NOT NULL DEFAULT -1,
                    created_at   TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    user     TEXT NOT NULL,
                    action   TEXT NOT NULL,
                    target   TEXT NOT NULL DEFAULT '',
                    detail   TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);
                CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user);
                CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
                CREATE TABLE IF NOT EXISTS feedback (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id      TEXT NOT NULL,
                    conversation_id TEXT NOT NULL DEFAULT '',
                    username        TEXT NOT NULL DEFAULT '',
                    collection_name TEXT NOT NULL DEFAULT '',
                    question        TEXT NOT NULL DEFAULT '',
                    answer          TEXT NOT NULL DEFAULT '',
                    rating          TEXT NOT NULL CHECK(rating IN ('up','down')),
                    reason          TEXT NOT NULL DEFAULT '',
                    expected_answer TEXT NOT NULL DEFAULT '',
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_message ON feedback(message_id);
                CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(rating);
                """
            )
            # 存量库兼容迁移 (v1.1): users.enabled / knowledge_bases.quota_*
            user_cols = [r["name"] for r in
                         self._conn.execute("PRAGMA table_info(users)").fetchall()]
            if "enabled" not in user_cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
            kb_cols = [r["name"] for r in
                       self._conn.execute("PRAGMA table_info(knowledge_bases)").fetchall()]
            if "quota_chunks" not in kb_cols:
                self._conn.execute("ALTER TABLE knowledge_bases "
                                   "ADD COLUMN quota_chunks INTEGER NOT NULL DEFAULT -1")
            if "quota_documents" not in kb_cols:
                self._conn.execute("ALTER TABLE knowledge_bases "
                                   "ADD COLUMN quota_documents INTEGER NOT NULL DEFAULT -1")
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None


# ---------------- 用户管理 ----------------

class UserManager:
    """用户注册/登录/令牌管理"""

    def __init__(self, db: _DB, session_store=None):
        self.db = db
        self.admin_username = settings.security.admin_username
        self.token_expire_hours = settings.security.token_expire_hours
        self.max_attempts = settings.security.max_login_attempts
        # v1.5 会话存储：redis_url 非空走 Redis 共享（可注入覆盖便于测试）
        if session_store is None:
            from app.core.session import get_session_store
            session_store = get_session_store()
        self.session_store = session_store

    def bootstrap_admin(self) -> str:
        """确保管理员存在；返回管理员初始密码

        管理员已存在时直接返回；库为空且已有凭据文件时沿用文件密码，
        保证文件与 DB 永远一致（避免环境重置后密码失配）。
        """
        existing = self.db.query_one("SELECT pass_hash FROM users WHERE username=?", (self.admin_username,))
        if existing:
            return settings.security.admin_password or ""
        password = settings.security.admin_password
        cred_path = Path("./data/admin_credentials.txt")
        if not password and cred_path.exists():
            # 复用已有凭据文件中的密码（迁移/重置场景保持连续性）
            for line in cred_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("初始密码"):
                    password = line.split(":", 1)[1].strip()
                    break
        if not password:
            password = secrets.token_urlsafe(9)  # 12 位随机密码
            cred_path.parent.mkdir(parents=True, exist_ok=True)
            cred_path.write_text(
                f"管理员账号: {self.admin_username}\n初始密码: {password}\n"
                f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n请尽快修改密码！\n",
                encoding="utf-8",
            )
        self.db.execute(
            "INSERT INTO users (username, role, display_name, pass_hash, created_at) VALUES (?,?,?,?,?)",
            (self.admin_username, "admin", "系统管理员",
             hash_password(password), time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return password

    def register(self, username: str, password: str, display_name: str = "") -> User:
        """注册普通用户（admin 为保留名）"""
        username = username.strip()
        if not username or len(username) < 2 or len(username) > 32:
            raise ValueError("用户名长度需为 2-32 个字符")
        if username.lower() == self.admin_username.lower():
            raise ValueError("该用户名不可注册")
        if not re_valid_username(username):
            raise ValueError("用户名仅支持字母、数字、下划线、中文")
        # v1.4 等保：密码强度校验（长度/复杂度/弱口令/不得含用户名）
        err = validate_password_strength(password, username)
        if err:
            raise ValueError(err)
        if self.db.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError("用户名已存在")
        self.db.execute(
            "INSERT INTO users (username, role, display_name, pass_hash, created_at) VALUES (?,?,?,?,?)",
            (username, "user", display_name or username,
             hash_password(password), time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return User(username=username, role="user", display_name=display_name or username)

    def login(self, username: str, password: str) -> tuple[Optional[User], Optional[str]]:
        """登录，成功返回 (User, token)，失败返回 (None, 错误信息)"""
        row = self.db.query_one("SELECT * FROM users WHERE username=?", (username,))
        if not row:
            return None, "用户名或密码错误"
        if not row["enabled"]:
            return None, "账号已被禁用，请联系管理员"
        # 锁定检查
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.max_attempts > 0 and row["locked_until"] and row["locked_until"] > now:
            return None, "失败次数过多，账号已临时锁定，请稍后再试"
        if not verify_password(password, row["pass_hash"]):
            # 连续失败计数
            attempt = row["fail_count"] + 1
            locked_until = ""
            if self.max_attempts > 0 and attempt >= self.max_attempts:
                locked_until = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 600))
                self.db.execute(
                    "UPDATE users SET fail_count=0, locked_until=? WHERE username=?",
                    (locked_until, username))
                # v1.4 登录失败告警：触发锁定
                self._alert_security(
                    username, f"连续登录失败 {self.max_attempts} 次，账号已锁定 10 分钟")
                return None, "失败次数过多，账号已临时锁定，请稍后再试"
            self.db.execute(
                "UPDATE users SET fail_count=?, locked_until=? WHERE username=?",
                (attempt, locked_until, username))
            # v1.4 登录失败告警：达到告警阈值（仅触发一次，防刷屏）
            threshold = settings.security.login_alert_threshold
            if threshold > 0 and attempt == threshold:
                self._alert_security(
                    username, f"连续登录失败 {attempt} 次，疑似暴力破解")
            return None, "用户名或密码错误"
        # 成功：重置失败计数，签发令牌
        token = secrets.token_urlsafe(32)
        expires = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + self.token_expire_hours * 3600))
        self.db.execute(
            "UPDATE users SET token=?, token_expires=?, fail_count=0, locked_until='' WHERE username=?",
            (token, expires, username))
        # v1.5 会话共享：Redis 双写（故障自动回退，SQLite 为权威）
        try:
            self.session_store.save(username, token, self.token_expire_hours * 3600)
        except Exception:  # noqa: BLE001
            logger.warning("会话存储写入异常，已回退 SQLite", exc_info=True)
        return User(username=row["username"], role=row["role"],
                    display_name=row["display_name"] or row["username"]), token

    def _alert_security(self, username: str, detail: str) -> None:
        """v1.4 登录失败告警：security.alert 审计 + WARNING 日志"""
        try:
            AuditLogger(self.db).log(username, "security.alert", username, detail)
        except Exception:  # 告警写入失败不影响登录流程
            pass
        logger.warning("[SECURITY] %s: %s", username, detail)
        # v1.6 Webhook：安全告警通知（后台线程，不阻塞登录）
        try:
            from app.core.webhook import fire_event
            fire_event("security.alert", "安全告警", f"用户 {username}：{detail}")
        except Exception:
            pass

    def get_user_by_token(self, token: str) -> Optional[User]:
        """令牌校验（含过期检查）；v1.5 优先查共享会话存储，未命中回退 SQLite"""
        if not token:
            return None
        # v1.5 共享会话：Redis 命中直接定位用户（SQLite 模式返回 None 走原逻辑）
        try:
            username = self.session_store.get_username(token)
        except Exception:  # noqa: BLE001 - Redis 故障回退 SQLite
            username = None
        if username is not None:
            row = self.db.query_one("SELECT * FROM users WHERE username=?", (username,))
        else:
            row = self.db.query_one("SELECT * FROM users WHERE token=?", (token,))
        if not row:
            return None
        if not row["enabled"]:
            return None  # 禁用后令牌立即失效
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if row["token_expires"] and row["token_expires"] < now:
            return None
        return User(username=row["username"], role=row["role"],
                    display_name=row["display_name"] or row["username"],
                    enabled=bool(row["enabled"]))

    def logout(self, token: str):
        if token:
            self.db.execute("UPDATE users SET token='' WHERE token=?", (token,))
            # v1.5 共享会话同步清除
            try:
                self.session_store.delete(token)
            except Exception:  # noqa: BLE001
                pass

    def change_password(self, username: str, old_password: str, new_password: str) -> tuple[bool, str]:
        """修改密码，成功返回 (True, "")；v1.4 接入等保强度校验"""
        row = self.db.query_one("SELECT pass_hash FROM users WHERE username=?", (username,))
        if not row or not verify_password(old_password, row["pass_hash"]):
            return False, "原密码不正确"
        err = validate_password_strength(new_password, username, old_password)
        if err:
            return False, err
        self.db.execute(
            "UPDATE users SET pass_hash=? WHERE username=?",
            (hash_password(new_password), username))
        return True, ""

    # ---- v1.1 管理后台方法（仅管理员调用） ----

    def list_users(self, keyword: str = "", page: int = 1, size: int = 20) -> dict:
        """用户列表（不含密码哈希/令牌），支持关键字模糊搜索"""
        where, params = [], []
        if keyword:
            where.append("(username LIKE ? OR display_name LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) AS c FROM users {cond}",
                                  tuple(params))["c"]
        rows = self.db.query(
            f"SELECT username, role, display_name, enabled, fail_count, locked_until, "
            f"created_at FROM users {cond} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params + [size, (page - 1) * size]),
        )
        return {"total": total, "page": page, "size": size, "items": rows}

    def admin_create_user(self, username: str, password: str,
                          display_name: str = "", role: str = "user") -> User:
        """管理员代建用户（支持指定角色）"""
        if role not in ("user", "admin"):
            raise ValueError("角色仅支持 user / admin")
        user = self.register(username, password, display_name)
        if role == "admin":
            self.db.execute("UPDATE users SET role='admin' WHERE username=?",
                            (username,))
        return User(username=user.username, role=role, display_name=user.display_name)

    def set_enabled(self, username: str, enabled: bool) -> None:
        """启用/禁用账号；禁用时立即吊销令牌"""
        if not self.db.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError(f"用户 '{username}' 不存在")
        if enabled:
            self.db.execute("UPDATE users SET enabled=1 WHERE username=?", (username,))
        else:
            self.db.execute(
                "UPDATE users SET enabled=0, token='', token_expires='' WHERE username=?",
                (username,))

    def set_display_name(self, username: str, display_name: str) -> None:
        if not self.db.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError(f"用户 '{username}' 不存在")
        self.db.execute("UPDATE users SET display_name=? WHERE username=?",
                        (display_name, username))

    def set_role(self, username: str, role: str) -> None:
        if role not in ("user", "admin"):
            raise ValueError("角色仅支持 user / admin")
        if not self.db.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError(f"用户 '{username}' 不存在")
        self.db.execute("UPDATE users SET role=? WHERE username=?", (role, username))

    def reset_password(self, username: str, new_password: str) -> None:
        """管理员重置密码（同时吊销该用户全部令牌）；v1.4 接入等保强度校验"""
        err = validate_password_strength(new_password, username)
        if err:
            raise ValueError(err)
        if not self.db.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError(f"用户 '{username}' 不存在")
        self.db.execute(
            "UPDATE users SET pass_hash=?, token='', token_expires='' WHERE username=?",
            (hash_password(new_password), username))

    def delete_user(self, username: str) -> None:
        """删除用户（管理路由层需校验：不可删 admin、不可删自己）"""
        self.db.execute("DELETE FROM users WHERE username=?", (username,))

    def unlock(self, username: str) -> None:
        """清除锁定状态"""
        if not self.db.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError(f"用户 '{username}' 不存在")
        self.db.execute(
            "UPDATE users SET fail_count=0, locked_until='' WHERE username=?", (username,))

    def get_user(self, username: str) -> Optional[dict]:
        """查询用户公开信息（管理台详情用）"""
        return self.db.query_one(
            "SELECT username, role, display_name, enabled, fail_count, locked_until, "
            "created_at FROM users WHERE username=?", (username,))


# ---------------- 知识库权限 ----------------

class KBRegistry:
    """知识库归属登记与权限判定"""

    def __init__(self, db: _DB):
        self.db = db

    def create(self, name: str, owner: str, display_name: str = "") -> KnowledgeBase:
        if self.db.query_one("SELECT 1 FROM knowledge_bases WHERE name=?", (name,)):
            raise ValueError(f"知识库 '{name}' 已存在")
        kb = KnowledgeBase(name=name, owner=owner, display_name=display_name,
                           created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.db.execute(
            "INSERT INTO knowledge_bases (name, owner, display_name, created_at) VALUES (?,?,?,?)",
            (name, owner, display_name, kb.created_at))
        return kb

    def get(self, name: str) -> Optional[KnowledgeBase]:
        row = self.db.query_one("SELECT * FROM knowledge_bases WHERE name=?", (name,))
        if not row:
            return None
        return KnowledgeBase(
            name=row["name"], owner=row["owner"],
            display_name=row["display_name"],
            quota_chunks=row["quota_chunks"], quota_documents=row["quota_documents"],
            created_at=row["created_at"])

    def set_quota(self, name: str, quota_chunks: int = -1,
                  quota_documents: int = -1) -> None:
        """设置知识库配额（-1 不限制；不能小于当前用量由路由层校验）"""
        if not self.db.query_one("SELECT 1 FROM knowledge_bases WHERE name=?", (name,)):
            raise ValueError(f"知识库 '{name}' 不存在")
        if quota_chunks != -1 and quota_chunks < 0:
            raise ValueError("块数配额需为 -1（不限）或非负整数")
        if quota_documents != -1 and quota_documents < 0:
            raise ValueError("文档数配额需为 -1（不限）或非负整数")
        self.db.execute(
            "UPDATE knowledge_bases SET quota_chunks=?, quota_documents=? WHERE name=?",
            (quota_chunks, quota_documents, name))

    def transfer_owner(self, old_owner: str, new_owner: str) -> int:
        """批量转移知识库属主（删除用户时保留数据），返回转移数"""
        count = self.db.query_one(
            "SELECT COUNT(*) AS c FROM knowledge_bases WHERE owner=?",
            (old_owner,))["c"]
        self.db.execute(
            "UPDATE knowledge_bases SET owner=? WHERE owner=?", (new_owner, old_owner))
        return count

    def delete(self, name: str):
        self.db.execute("DELETE FROM knowledge_bases WHERE name=?", (name,))

    def list_all(self) -> list[KnowledgeBase]:
        return [
            KnowledgeBase(name=r["name"], owner=r["owner"],
                          display_name=r["display_name"],
                          quota_chunks=r["quota_chunks"],
                          quota_documents=r["quota_documents"],
                          created_at=r["created_at"])
            for r in self.db.query("SELECT * FROM knowledge_bases ORDER BY created_at DESC")
        ]

    def list_for(self, user: User) -> list[KnowledgeBase]:
        """普通用户仅见自己的知识库，管理员可见全部"""
        if user.is_admin:
            return self.list_all()
        return [
            KnowledgeBase(name=r["name"], owner=r["owner"],
                          display_name=r["display_name"],
                          quota_chunks=r["quota_chunks"],
                          quota_documents=r["quota_documents"],
                          created_at=r["created_at"])
            for r in self.db.query(
                "SELECT * FROM knowledge_bases WHERE owner=? ORDER BY created_at DESC",
                (user.username,))
        ]

    def can_access(self, name: str, user: User) -> bool:
        """用户是否有权使用该知识库（管理员或属主，库不存在则无权）

        user 为 None（内部调用/未认证上下文）时视为无权。
        """
        if user is None:
            return False
        kb = self.get(name)
        if kb is None:
            return False
        return user.is_admin or kb.owner == user.username

    def migrate_existing(self, collection_names: list[str], admin_username: str):
        """迁移: 存量 Chroma 知识库（无登记记录）归属管理员"""
        for name in collection_names:
            if not self.get(name):
                try:
                    self.create(name, admin_username, display_name=name)
                except ValueError:
                    pass  # 并发下已存在


# ---------------- 审计日志 ----------------

class AuditLogger:
    """操作审计日志"""

    def __init__(self, db: _DB):
        self.db = db

    def log(self, username: str, action: str, target: str = "", detail: str = ""):
        try:
            self.db.execute(
                "INSERT INTO audit_logs (ts, user, action, target, detail) VALUES (?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), username or "anonymous",
                 action, target or "", (detail or "")[:500]),
            )
        except Exception:
            pass  # 审计失败不影响业务

    def query(self, user_filter: str = "", action_filter: str = "",
              page: int = 1, size: int = 50) -> dict:
        where, params = [], []
        if user_filter:
            where.append("user=?")
            params.append(user_filter)
        if action_filter:
            where.append("action=?")
            params.append(action_filter)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(
            f"SELECT COUNT(*) AS c FROM audit_logs {cond}", tuple(params))["c"]
        rows = self.db.query(
            f"SELECT * FROM audit_logs {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params + [size, (page - 1) * size]),
        )
        return {"total": total, "page": page, "size": size, "items": rows}


# ---------------- 用户反馈 (v1.2) ----------------

class FeedbackManager:
    """用户反馈闭环：点赞/点踩落库 + 回流黄金评测集 (v1.2)

    回流：点踩且填写了"期望回答"的反馈可导出为评测集条目，
    用于迭代回归（eval/run_regression.py --collect-feedback）。
    """

    def __init__(self, db: _DB):
        self.db = db

    def add(self, message_id: str, username: str, rating: str,
            question: str = "", answer: str = "",
            conversation_id: str = "", collection_name: str = "",
            reason: str = "", expected_answer: str = "") -> int:
        """新增反馈；同一消息重复提交直接覆盖（保持最新意图）"""
        if rating not in ("up", "down"):
            raise ValueError("rating 必须为 up 或 down")
        existing = self.db.query_one(
            "SELECT id FROM feedback WHERE message_id=?", (message_id,))
        if existing:
            self.db.execute(
                """UPDATE feedback SET username=?, rating=?, question=?, answer=?,
                   conversation_id=?, collection_name=?, reason=?, expected_answer=?,
                   created_at=? WHERE message_id=?""",
                (username, rating, question[:500], answer[:2000],
                 conversation_id, collection_name,
                 reason[:500], expected_answer[:1000],
                 time.strftime("%Y-%m-%d %H:%M:%S"), message_id))
            return existing["id"]
        return self.db.execute(
            """INSERT INTO feedback (message_id, conversation_id, username,
               collection_name, question, answer, rating, reason, expected_answer, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (message_id, conversation_id, username, collection_name,
             question[:500], answer[:2000], rating, reason[:500],
             expected_answer[:1000], time.strftime("%Y-%m-%d %H:%M:%S")))

    def list(self, rating: Optional[str] = None, limit: int = 200,
             offset: int = 0) -> list[dict]:
        """反馈列表（管理端），按时间倒序"""
        where, params = "", ()
        if rating in ("up", "down"):
            where, params = " WHERE rating=?", (rating,)
        rows = self.db.query(
            f"SELECT * FROM feedback{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + (limit, offset))
        for r in rows:
            r["rating"] = r.get("rating", "")
            r["_date"] = r.get("created_at", "")[:16]
        return rows

    def export_dataset(self) -> dict:
        """回流为黄金评测集（点踩 + 期望回答的反馈）"""
        rows = self.db.query(
            """SELECT question, expected_answer FROM feedback
               WHERE rating='down' AND expected_answer != ''
               ORDER BY id DESC LIMIT 500""")
        items = [{
            "question": r["question"],
            "golden_answer": r["expected_answer"],
            "source_doc": "(feedback)",
            "origin": "user_feedback",
        } for r in rows if r["question"]]
        return {
            "name": "用户反馈回流评测集",
            "description": "由用户点踩反馈 + 期望回答回流生成，追加到黄金评测集回归",
            "collection": "",
            "items": items,
        }

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM feedback")
        return row["c"] if row else 0


# ---------------- 全局单例与 FastAPI 依赖 ----------------

def re_valid_username(username: str) -> bool:
    import re
    return bool(re.fullmatch(r"[\w\u4e00-\u9fff_-]{2,32}", username))


# 常见弱口令黑名单（等保 2.0 三级：禁用弱口令）
WEAK_PASSWORDS = {
    "123456", "12345678", "123456789", "1234567890", "000000", "111111",
    "666666", "888888", "88888888", "password", "password123", "p@ssw0rd",
    "admin", "admin123", "admin888", "root", "root123", "qwerty", "qwerty123",
    "abc123", "abc123456", "a123456", "1qaz2wsx", "iloveyou", "welcome",
    "sunshine", "monkey", "football", "11111111", "123123", "1234567",
}


def validate_password_strength(password: str, username: str = "",
                               old_password: str = "") -> Optional[str]:
    """密码强度校验（v1.4 等保 2.0 三级），返回错误信息或 None

    规则：长度 ≥ password_min_length；至少包含小写/大写/数字/特殊字符中 3 类；
    不在弱口令黑名单；不得与用户名相同或包含用户名（长度 ≥4 时）；不得与原密码相同。
    """
    min_len = max(settings.security.password_min_length, 6)
    if not password or len(password) < min_len:
        return f"密码长度至少 {min_len} 位"
    if password.lower() in WEAK_PASSWORDS:
        return "密码过于简单，请更换（弱口令黑名单）"
    categories = 0
    if re.search(r"[a-z]", password):
        categories += 1
    if re.search(r"[A-Z]", password):
        categories += 1
    if re.search(r"\d", password):
        categories += 1
    if re.search(r"[^A-Za-z0-9]", password):
        categories += 1
    if categories < 3:
        return "密码复杂度不足：需包含大写字母/小写字母/数字/特殊字符中的至少 3 类"
    if username and len(username) >= 2:
        if password.lower() == username.lower():
            return "密码不能与用户名相同"
        if len(username) >= 4 and username.lower() in password.lower():
            return "密码不能包含用户名"
    if old_password and password == old_password:
        return "新密码不能与原密码相同"
    return None


_security_db: Optional[_DB] = None
_user_manager: Optional[UserManager] = None
_kb_registry: Optional[KBRegistry] = None
_audit_logger: Optional[AuditLogger] = None
_feedback_manager: Optional[FeedbackManager] = None


def _init_security() -> None:
    """延迟初始化（首次访问时），并引导管理员"""
    global _security_db, _user_manager, _kb_registry, _audit_logger, _feedback_manager
    if _user_manager is not None:
        return
    _security_db = _DB(settings.security.db_path)
    _user_manager = UserManager(_security_db)
    _kb_registry = KBRegistry(_security_db)
    _audit_logger = AuditLogger(_security_db)
    _feedback_manager = FeedbackManager(_security_db)
    _user_manager.bootstrap_admin()


def get_user_manager() -> UserManager:
    _init_security()
    return _user_manager


def get_kb_registry() -> KBRegistry:
    _init_security()
    return _kb_registry


def get_audit_logger() -> AuditLogger:
    _init_security()
    return _audit_logger


def get_feedback_manager() -> FeedbackManager:
    _init_security()
    return _feedback_manager


# FastAPI 安全依赖
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    """当前登录用户；auth_enabled=false 时返回内置 system 管理员"""
    if not settings.security.auth_enabled:
        user = User(username="system", role="admin", display_name="系统(免认证)")
    elif credentials is None:
        raise HTTPException(status_code=401, detail="未登录或令牌缺失")
    else:
        user = get_user_manager().get_user_by_token(credentials.credentials)
        if user is None:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    request.state.username = user.username  # 供请求日志中间件记录
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """仅管理员可访问"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_kb_access(name: str, user: User) -> None:
    """校验知识库访问权限，无权抛 403"""
    if not get_kb_registry().can_access(name, user):
        raise HTTPException(
            status_code=403,
            detail=f"无权访问知识库 '{name}'（仅属主或管理员可用）",
        )


