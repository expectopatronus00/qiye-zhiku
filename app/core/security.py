"""安全模块 - 多用户认证 + 知识库权限隔离 + 操作审计 (Day 7, v0.7)

- 用户认证: PBKDF2-HMAC-SHA256 密码哈希（600k 迭代 + 随机盐）、
  不透明会话令牌（secrets.token_urlsafe），支持连续失败锁定
- 知识库隔离: SQLite 登记每个知识库的属主，普通用户仅可见/可用
  自己创建的知识库，管理员可见全部
- 审计日志: 登录/登出/上传/删除/问答等关键操作全部留痕，
  仅管理员可查询
- 降级模式: security.auth_enabled=false 时返回内置 system 管理员，
  兼容内网直连场景

所有数据库操作使用单连接 + 线程锁（写操作毫秒级，无性能压力）。
"""
import hashlib
import hmac
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

_PBKDF2_ITERATIONS = 600_000


# ---------------- 数据结构 ----------------

@dataclass
class User:
    """当前登录用户"""
    username: str
    role: str = "user"          # user | admin
    display_name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class KnowledgeBase:
    """知识库归属记录"""
    name: str
    owner: str
    display_name: str = ""
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
                    created_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    name         TEXT PRIMARY KEY,
                    owner        TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
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
                """
            )
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

    def __init__(self, db: _DB):
        self.db = db
        self.admin_username = settings.security.admin_username
        self.token_expire_hours = settings.security.token_expire_hours
        self.max_attempts = settings.security.max_login_attempts

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
        if len(password) < 6:
            raise ValueError("密码长度至少 6 位")
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
        # 锁定检查
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.max_attempts > 0 and row["locked_until"] and row["locked_until"] > now:
            return None, "失败次数过多，账号已临时锁定，请稍后再试"
        if not verify_password(password, row["pass_hash"]):
            # 连续失败计数
            fail = row["fail_count"] + 1
            locked_until = ""
            if self.max_attempts > 0 and fail >= self.max_attempts:
                locked_until = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 600))
                fail = 0
                self.db.execute(
                    "UPDATE users SET fail_count=?, locked_until=? WHERE username=?",
                    (fail, locked_until, username))
                return None, "失败次数过多，账号已临时锁定，请稍后再试"
            self.db.execute(
                "UPDATE users SET fail_count=?, locked_until=? WHERE username=?",
                (fail, locked_until, username))
            return None, "用户名或密码错误"
        # 成功：重置失败计数，签发令牌
        token = secrets.token_urlsafe(32)
        expires = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + self.token_expire_hours * 3600))
        self.db.execute(
            "UPDATE users SET token=?, token_expires=?, fail_count=0, locked_until='' WHERE username=?",
            (token, expires, username))
        return User(username=row["username"], role=row["role"],
                    display_name=row["display_name"] or row["username"]), token

    def get_user_by_token(self, token: str) -> Optional[User]:
        """令牌校验（含过期检查）"""
        if not token:
            return None
        row = self.db.query_one("SELECT * FROM users WHERE token=?", (token,))
        if not row:
            return None
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if row["token_expires"] and row["token_expires"] < now:
            return None
        return User(username=row["username"], role=row["role"],
                    display_name=row["display_name"] or row["username"])

    def logout(self, token: str):
        if token:
            self.db.execute("UPDATE users SET token='' WHERE token=?", (token,))

    def change_password(self, username: str, old_password: str, new_password: str) -> tuple[bool, str]:
        """修改密码，成功返回 (True, "")"""
        row = self.db.query_one("SELECT pass_hash FROM users WHERE username=?", (username,))
        if not row or not verify_password(old_password, row["pass_hash"]):
            return False, "原密码不正确"
        self.db.execute(
            "UPDATE users SET pass_hash=? WHERE username=?",
            (hash_password(new_password), username))
        return True, ""


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
        return KnowledgeBase(name=row["name"], owner=row["owner"],
                             display_name=row["display_name"], created_at=row["created_at"])

    def delete(self, name: str):
        self.db.execute("DELETE FROM knowledge_bases WHERE name=?", (name,))

    def list_all(self) -> list[KnowledgeBase]:
        return [
            KnowledgeBase(name=r["name"], owner=r["owner"],
                          display_name=r["display_name"], created_at=r["created_at"])
            for r in self.db.query("SELECT * FROM knowledge_bases ORDER BY created_at DESC")
        ]

    def list_for(self, user: User) -> list[KnowledgeBase]:
        """普通用户仅见自己的知识库，管理员可见全部"""
        if user.is_admin:
            return self.list_all()
        return [
            KnowledgeBase(name=r["name"], owner=r["owner"],
                          display_name=r["display_name"], created_at=r["created_at"])
            for r in self.db.query(
                "SELECT * FROM knowledge_bases WHERE owner=? ORDER BY created_at DESC",
                (user.username,))
        ]

    def can_access(self, name: str, user: User) -> bool:
        """用户是否有权使用该知识库（管理员或属主，库不存在则无权）"""
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


# ---------------- 全局单例与 FastAPI 依赖 ----------------

def re_valid_username(username: str) -> bool:
    import re
    return bool(re.fullmatch(r"[\w\u4e00-\u9fff_-]{2,32}", username))


_security_db: Optional[_DB] = None
_user_manager: Optional[UserManager] = None
_kb_registry: Optional[KBRegistry] = None
_audit_logger: Optional[AuditLogger] = None


def _init_security() -> None:
    """延迟初始化（首次访问时），并引导管理员"""
    global _security_db, _user_manager, _kb_registry, _audit_logger
    if _user_manager is not None:
        return
    _security_db = _DB(settings.security.db_path)
    _user_manager = UserManager(_security_db)
    _kb_registry = KBRegistry(_security_db)
    _audit_logger = AuditLogger(_security_db)
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


# FastAPI 安全依赖
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    """当前登录用户；auth_enabled=false 时返回内置 system 管理员"""
    if not settings.security.auth_enabled:
        return User(username="system", role="admin", display_name="系统(免认证)")
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录或令牌缺失")
    user = get_user_manager().get_user_by_token(credentials.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
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
