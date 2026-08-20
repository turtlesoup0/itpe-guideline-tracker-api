"""제외 분석 주기 태스크.

주 1회, 사람이 수동 제외한 항목을 모아 필터 규칙 후보를 갱신한다.
후보는 저장만 하고 필터에 반영하지 않는다 — 승인은 사람이 한다.
"""

import logging

from app.db.session import async_session_factory
from app.services.exclusion_analysis import analyze_exclusions, store_candidates
from app.tasks.celery_app import celery
from app.tasks.crawl_tasks import _run_async

logger = logging.getLogger(__name__)


async def _refresh() -> dict:
    async with async_session_factory() as session:
        candidates = await analyze_exclusions(session)
        stats = await store_candidates(session, candidates)
        await session.commit()
    stats["candidates"] = len(candidates)
    return stats


@celery.task(name="app.tasks.exclusion_tasks.refresh_exclusion_candidates")
def refresh_exclusion_candidates() -> dict:
    """제외 규칙 후보 갱신."""
    stats = _run_async(_refresh())
    logger.info("제외 규칙 후보 갱신: %s", stats)
    return stats
