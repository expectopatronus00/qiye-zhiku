"""会话存储抽象 (v1.5 性能与高可用)

多副本部署时登录会话需跨实例共享：
- SQLiteSessionStore：默认单机模式（token 存 users 表，现状保持）
- RedisSessionStore：security.redis_url 配置非空时启用，
  token -> username 映射 + TTL 过期；redis 客户端/连接故障自动回退 SQLite
  （登录/登出均双写，SQLite 始终是权威用户数据源）

用法：
    from app.core.security import UserManager
    # UserManager 内部自动选择 store，业务层无感知
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SessionStore:
    """会话存储接口"""

    def save(self, username: str, token: str, expire_seconds: int) -> None:
        raise NotImplementedError

    def get_username(self, token: str) -> str | None:
        """返回 token 对应的用户名；未命中返回 None"""
        raise NotImplementedError

    def delete(self, token: str) -> None:
        raise NotImplementedError


class SQLiteSessionStore(SessionStore):
    """单机模式：token 由 users 表管理（login/get_user_by_token 直查 DB）"""

    def save(self, username: str, token: str, expire_seconds: int) -> None:
        pass  # 无操作：SQLite 由 UserManager 双写保证

    def get_username(self, token: str) -> str | None:
        return None  # 未命中标记，调用方回退 SQLite 查询

    def delete(self, token: str) -> None:
        pass  # 无操作：SQLite 由 UserManager 处理


class RedisSessionStore(SessionStore):
    """Redis 共享会话：token -> username，SETEX 自动过期"""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client = None
        try:
            import redis  # 延迟导入：未安装 redis 包时优雅回退
            self._client = redis.Redis.from_url(redis_url, socket_timeout=2)
            logger.info("Redis 会话存储已启用: %s", redis_url)
        except ImportError:
            logger.warning("未安装 redis 包（pip install redis），会话存储回退 SQLite")

    @property
    def available(self) -> bool:
        return self._client is not None

    def save(self, username: str, token: str, expire_seconds: int) -> None:
        if not self.available:
            return
        try:
            self._client.setex(f"session:{token}", expire_seconds, username)
        except Exception as e:  # noqa: BLE001 - Redis 故障不影响登录（SQLite 兜底）
            logger.warning("Redis 会话写入失败（回退 SQLite）: %s", e)

    def get_username(self, token: str) -> str | None:
        if not self.available:
            return None
        try:
            val = self._client.get(f"session:{token}")
            return val.decode("utf-8") if val else None
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 会话读取失败（回退 SQLite）: %s", e)
            return None

    def delete(self, token: str) -> None:
        if not self.available:
            return
        try:
            self._client.delete(f"session:{token}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 会话删除失败（SQLite 已清空）: %s", e)


def get_session_store() -> SessionStore:
    """按配置选择会话存储（security.redis_url 非空 → Redis）"""
    from app.core.config import settings
    if settings.security.redis_url:
        return RedisSessionStore(settings.security.redis_url)
    return SQLiteSessionStore()
