"""企业智库 RAG 问答系统 - 应用入口 (v1.0 生产加固)"""
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from app.core.config import settings
from app.core.logging_setup import get_logger, request_log_middleware, setup_logging
from app.core.vectorstore import VectorStore
from app.core.security import get_audit_logger, get_kb_registry, get_user_manager
from app.routers import admin, auth, audit, chat, documents, health, knowledge

APP_VERSION = "1.1.0"
logger = get_logger("main")

# 统一日志（文件轮转 + 控制台），启动即生效
setup_logging(settings.data.logs)

app = FastAPI(
    title="企业智库 RAG 问答系统",
    description="面向央企 AI 场景的私有化知识库问答引擎",
    version=APP_VERSION,
)

# 请求日志中间件（方法/路径/状态/耗时/用户）
app.middleware("http")(request_log_middleware)

# 注册路由
app.include_router(health.router, tags=["健康检查"])  # /healthz /readyz /health
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(audit.router, prefix="/api/audit", tags=["审计"])
app.include_router(chat.router, prefix="/api/chat", tags=["对话"])
app.include_router(documents.router, prefix="/api/documents", tags=["文档"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["知识库"])
app.include_router(admin.router, prefix="/api/admin", tags=["管理后台"])

# 静态文件
static_dir = Path(__file__).parent / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def startup():
    """启动引导: 管理员初始化 + 存量知识库迁移归属管理员"""
    if settings.security.auth_enabled:
        get_user_manager()  # 触发 bootstrap_admin
        registry = get_kb_registry()
        try:
            existing = VectorStore().list_collections()
            registry.migrate_existing(existing, settings.security.admin_username)
            get_audit_logger().log(settings.security.admin_username,
                                   "system.startup", "",
                                   f"启动引导完成，迁移 {len(existing)} 个存量知识库")
        except Exception:
            pass  # Chroma 不可用时忽略，不影响启动


@app.get("/")
async def index():
    """返回前端页面"""
    return FileResponse(str(static_dir / "index.html"))


def main():
    """启动服务"""
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.debug,
    )


if __name__ == "__main__":
    main()
