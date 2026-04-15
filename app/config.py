"""
환경 설정 — pydantic-settings 기반.

.env 파일 또는 환경변수에서 읽습니다.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """애플리케이션 전역 설정."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ──────────────────────────────────────────────
    app_name: str = "itpe-guideline-tracker-api"
    debug: bool = False

    # ── Database (PostgreSQL) ────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/guideline_tracker"

    # ── Redis (Celery broker + result backend) ───────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Crawling ─────────────────────────────────────────
    crawl_user_agent: str = (
        "Mozilla/5.0 (compatible; GuidelineTracker/0.1; +https://github.com/turtlesoup0)"
    )
    crawl_request_delay_sec: float = 1.0  # 기관 서버 부하 방지용 요청 간 딜레이

    # ── 법제처 API ───────────────────────────────────────
    law_api_base: str = "https://www.law.go.kr"

    # ── Claude API (가이드라인 변경 의의 요약용) ──────────
    anthropic_api_key: str = ""

    # ── CORS (프론트엔드 연동) ───────────────────────────
    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    """싱글톤 설정 인스턴스."""
    return Settings()
