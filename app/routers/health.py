"""健康检查路由 (v1.0) - /healthz 存活 + /readyz 就绪（依赖探测）

- GET /healthz  存活探针：进程活着即 200（K8s/Docker liveness）
- GET /readyz   就绪探针：探测向量库/数据库/Ollama，核心依赖失败返回 503（readiness）
- GET /health   兼容旧端点（返回版本与状态）
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import settings

APP_VERSION = "1.0.0"

router = APIRouter(tags=["健康检查"])


def _check_vectorstore() -> dict:
    """向量库可用性：能列出集合即可（懒连接，Chroma 失败抛异常）"""
    t0 = time.time()
    try:
        from app.core.vectorstore import get_vector_store
        get_vector_store().list_collections()
        return {"ok": True, "ms": round((time.time() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "error": str(exc)[:120]}


def _check_database() -> dict:
    """安全数据库可用性：查询用户表"""
    t0 = time.time()
    try:
        from app.core.security import get_user_manager
        get_user_manager().db.query("SELECT COUNT(*) FROM users")
        return {"ok": True, "ms": round((time.time() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "error": str(exc)[:120]}


async def _check_llm() -> dict:
    """LLM 可用性：Ollama /api/tags 或 OpenAI 兼容 /models（国产 provider 同通道，短超时）"""
    t0 = time.time()
    if settings.llm.provider == "ollama":
        url = settings.llm.ollama_base_url.rstrip("/") + "/api/tags"
        headers = {}
    else:
        from app.core.llm import resolve_openai_base_url
        url = resolve_openai_base_url().rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {settings.llm.openai_api_key}"} if settings.llm.openai_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url, headers=headers)
            ok = resp.status_code < 500
            return {"ok": ok, "ms": round((time.time() - t0) * 1000),
                    "status": resp.status_code}
    except Exception as exc:
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "error": str(exc)[:120]}


@router.get("/healthz")
async def healthz():
    """存活探针：进程活着即通过"""
    return {"status": "ok", "ts": datetime.now().isoformat(timespec="seconds")}


@router.get("/readyz")
async def readyz():
    """就绪探针：核心依赖（向量库/数据库）可用才就绪；LLM 异常标记 degraded 不阻塞"""
    checks = {
        "vectorstore": _check_vectorstore(),
        "database": _check_database(),
        "llm": await _check_llm(),
    }
    core_ok = checks["vectorstore"]["ok"] and checks["database"]["ok"]
    llm_ok = checks["llm"]["ok"]
    if core_ok and llm_ok:
        status = "ready"
    elif core_ok:
        status = "degraded"  # 核心可用但 LLM 异常（文档管理等功能仍可用）
    else:
        status = "unavailable"
    body = {
        "status": status,
        "version": APP_VERSION,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }
    return JSONResponse(status_code=200 if core_ok else 503, content=body)


@router.get("/health")
async def health_legacy():
    """兼容旧端点"""
    llm = await _check_llm()
    return {
        "status": "ok" if llm["ok"] else "degraded",
        "version": APP_VERSION,
        "llm": "ok" if llm["ok"] else "unavailable",
    }
