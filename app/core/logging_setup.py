"""统一日志体系 (v1.0) - 双通道输出 + 文件轮转 + 请求日志中间件

- 控制台：简洁文本（容器/开发友好）
- 文件：data/logs/app.log，RotatingFileHandler 5MB × 5 轮转
- 访问日志：data/logs/access.log（uvicorn access 重定向到文件）
- 请求日志中间件：method/path/status/duration/用户 统一留痕
"""
from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from fastapi import Request

_APP_LOGGER_NAME = "qiye_zhiku"
_ACCESS_LOGGER_NAME = "uvicorn.access"

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


class JsonFormatter(logging.Formatter):
    """结构化 JSON 行格式（机器可读，便于接入日志采集）"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("method", "path", "status", "duration_ms", "username", "client_ip"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(log_dir: str | Path, json_lines: bool = False) -> None:
    """初始化日志：应用日志文件轮转 + 控制台输出；uvicorn access 转文件"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 应用 logger（含子模块）
    app_logger = logging.getLogger(_APP_LOGGER_NAME)
    app_logger.setLevel(logging.INFO)
    app_logger.handlers.clear()
    app_logger.propagate = False

    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter() if json_lines
                              else logging.Formatter(_FORMAT))
    app_logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FORMAT))
    app_logger.addHandler(console)

    # uvicorn access 日志 → 文件轮转（同时保留控制台）
    access = logging.getLogger(_ACCESS_LOGGER_NAME)
    access.setLevel(logging.INFO)
    access.handlers.clear()
    access_file = RotatingFileHandler(
        log_dir / "access.log", maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    access_file.setFormatter(JsonFormatter() if json_lines
                             else logging.Formatter(_FORMAT))
    access.addHandler(access_file)
    access.propagate = True  # 控制台仍可见


def get_logger(name: str) -> logging.Logger:
    """获取应用日志器（继承统一配置）"""
    return logging.getLogger(f"{_APP_LOGGER_NAME}.{name}")


async def request_log_middleware(request: Request, call_next):
    """请求日志：方法/路径/状态码/耗时/用户 + v1.5 Prometheus 指标埋点"""
    start = time.time()
    response = await call_next(request)
    duration_ms = round((time.time() - start) * 1000)
    from app.core.metrics import record_request  # 延迟导入避免循环依赖
    record_request(request.method, request.url.path, response.status_code,
                   duration_ms / 1000)
    logger = get_logger("access")
    username = getattr(request.state, "username", None)
    logger.info(
        "%s %s -> %s (%dms)%s",
        request.method, request.url.path, response.status_code, duration_ms,
        f" user={username}" if username else "",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "username": username or "",
            "client_ip": request.client.host if request.client else "",
        },
    )
    return response
