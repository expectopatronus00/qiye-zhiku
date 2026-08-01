"""企业智库 RAG 问答系统 - 应用入口"""
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from app.core.config import settings
from app.routers import chat, documents, knowledge

app = FastAPI(
    title="企业智库 RAG 问答系统",
    description="面向央企 AI 场景的私有化知识库问答引擎",
    version="0.1.0",
)

# 注册路由
app.include_router(chat.router, prefix="/api/chat", tags=["对话"])
app.include_router(documents.router, prefix="/api/documents", tags=["文档"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["知识库"])

# 静态文件
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    """返回前端页面"""
    return FileResponse(str(static_dir / "index.html"))


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "version": "0.1.0"}


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
