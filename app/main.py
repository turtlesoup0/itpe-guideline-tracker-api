"""
FastAPI 엔트리포인트.

uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """앱 시작/종료 시 리소스 관리."""
    # TODO: DB 커넥션 풀 초기화
    # TODO: Celery worker health check
    yield
    # TODO: 리소스 정리


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="IT 보안·개인정보·SW 가이드라인 개정 추적 API",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health check ─────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    """서버 상태 확인."""
    return {"status": "ok"}


# ── Routers ──────────────────────────────────────────────
from app.routers import agencies, crawl, dashboard, guidelines

app.include_router(agencies.router)
app.include_router(guidelines.router)
app.include_router(crawl.router)
app.include_router(dashboard.router)
