"""
Celery 앱 인스턴스 + Beat 스케줄 설정.

실행:
  celery -A app.tasks.celery_app worker --loglevel=info
  celery -A app.tasks.celery_app beat --loglevel=info
"""

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery = Celery(
    "guideline-tracker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.crawl_tasks"],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Seoul",
    enable_utc=True,
    # 워커가 한 번에 하나의 크롤링만 실행 (정부 사이트 부하 방지)
    worker_concurrency=1,
    worker_prefetch_multiplier=1,
    # 작업 타임아웃
    task_soft_time_limit=300,   # 5분 소프트 리밋
    task_time_limit=600,        # 10분 하드 리밋
)

# ── Beat 스케줄 ──────────────────────────────────────────
# 기관별 크롤링 주기를 Celery Beat으로 관리합니다.
# 실제 기관별 주기(daily/weekly/monthly)는 DB CrawlConfig에서 읽어
# crawl_all 태스크가 필터링합니다.

celery.conf.beat_schedule = {
    # ── 매일 09:00 KST: RSS 피드 크롤링 (행안부, 금융위, KISA) ──
    "crawl-rss-daily": {
        "task": "app.tasks.crawl_tasks.crawl_by_schedule",
        "schedule": crontab(hour=0, minute=0),  # 00:00 UTC = 09:00 KST
        "args": ("daily",),
    },
    # ── 매주 월요일 09:30 KST: 게시판 크롤링 ──
    "crawl-bbs-weekly": {
        "task": "app.tasks.crawl_tasks.crawl_by_schedule",
        "schedule": crontab(hour=0, minute=30, day_of_week=1),  # Mon 00:30 UTC
        "args": ("weekly",),
    },
    # ── 매월 1일 10:00 KST: 월간 크롤링 (NIS, NIA, SPRi) ──
    "crawl-monthly": {
        "task": "app.tasks.crawl_tasks.crawl_by_schedule",
        "schedule": crontab(hour=1, minute=0, day_of_month=1),  # 1st 01:00 UTC
        "args": ("monthly",),
    },
    # ── 매주 수요일 10:00 KST: 법제처 행정규칙 변경 감지 ──
    "check-legal-bases": {
        "task": "app.tasks.crawl_tasks.check_legal_basis_updates",
        "schedule": crontab(hour=1, minute=0, day_of_week=3),  # Wed 01:00 UTC
    },
}
