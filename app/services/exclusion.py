"""수집 제외 — 추적할 필요가 없다고 사람이 판단한 항목 처리.

두 가지를 함께 해야 제외가 유지된다:

1. guidelines.excluded_at 세팅 → 목록·대시보드에서 숨김
2. 같은 URL의 crawl_decisions 를 EXCLUDED(stage="manual") 로 기록
   → guideline_sync 가 다음 크롤에서 판정 캐시로 걸러낸다
     (guideline_sync.py 의 decision_cache 조회는 url_index 확인보다 앞에 있어
      제외 판정이 "이미 수집됨" 경로보다 먼저 적용된다)

행은 지우지 않는다. 왜 제외됐는지가 필터 규칙을 다듬는 근거이고,
잘못 제외했을 때 되돌릴 수 있어야 하기 때문이다.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crawl_decision import CrawlDecision, DecisionOutcome
from app.models.guideline import ExclusionCategory, Guideline
from app.services.url_key import normalize_decision_url

logger = logging.getLogger(__name__)

MANUAL_STAGE = "manual"


class ExclusionError(ValueError):
    """제외 처리 입력이 유효하지 않을 때."""


async def exclude_guideline(
    db: AsyncSession,
    guideline: Guideline,
    category: ExclusionCategory,
    note: str | None = None,
) -> Guideline:
    """가이드라인을 수집 제외 처리한다 (멱등).

    Raises:
        ExclusionError: category=other 인데 사유 메모가 없을 때.
            분류가 'other'로 몰리면 주기 분석이 아무것도 도출할 수 없으므로
            최소한 사람이 남긴 문장은 받아 둔다.
    """
    note = (note or "").strip() or None

    if category == ExclusionCategory.OTHER and not note:
        raise ExclusionError("분류가 '기타'일 때는 사유 메모가 필요합니다.")

    guideline.excluded_at = guideline.excluded_at or datetime.now(timezone.utc)
    guideline.exclusion_category = category
    guideline.exclusion_note = note

    await _mark_decision_excluded(db, guideline, category, note)

    logger.info(
        "수집 제외: [%s] %s (%s)",
        category.value, guideline.title[:60], note or "-",
    )
    return guideline


async def restore_guideline(db: AsyncSession, guideline: Guideline) -> Guideline:
    """제외를 해제한다 (멱등).

    수동 제외로 남긴 판정 기록만 지운다. 크롤러가 자동으로 내린 판정
    (it_domain, regex_exclude, llm 등)은 건드리지 않는다 — 그건 사람이
    되돌린 결정이 아니고, 지우면 다음 크롤에서 같은 판정을 다시 하게 된다.
    """
    guideline.excluded_at = None
    guideline.exclusion_category = None
    guideline.exclusion_note = None

    decision = await _find_decision(db, guideline)
    if decision is not None and decision.stage == MANUAL_STAGE:
        await db.delete(decision)

    logger.info("수집 제외 해제: %s", guideline.title[:60])
    return guideline


async def _find_decision(
    db: AsyncSession, guideline: Guideline
) -> CrawlDecision | None:
    """판정 행 조회.

    정규화 URL이 캐시 키지만, 과거에 원본 URL로 저장된 행도 남아 있어
    양쪽을 모두 찾는다(정규화 키 우선).
    """
    if not guideline.source_url:
        return None
    key = normalize_decision_url(guideline.source_url) or guideline.source_url
    candidates = {key, guideline.source_url}
    result = await db.execute(
        select(CrawlDecision).where(CrawlDecision.url.in_(candidates))
    )
    rows = list(result.scalars().all())
    if not rows:
        return None
    for row in rows:
        if row.url == key:
            return row
    return rows[0]


async def _mark_decision_excluded(
    db: AsyncSession,
    guideline: Guideline,
    category: ExclusionCategory,
    note: str | None,
) -> None:
    """재수집 차단용 판정 기록을 남긴다.

    source_url 이 없는 항목(수동 등록 등)은 판정 캐시 키가 없어 기록하지
    않는다 — excluded_at 만으로 화면에서는 숨겨진다.
    """
    if not guideline.source_url:
        logger.debug("source_url 없음 — 판정 기록 생략: %s", guideline.title[:60])
        return

    reason = f"수동 제외 [{category.value}]" + (f": {note}" if note else "")
    existing = await _find_decision(db, guideline)

    if existing is not None:
        existing.outcome = DecisionOutcome.EXCLUDED
        existing.stage = MANUAL_STAGE
        existing.reason = reason
        existing.title = guideline.title[:500]
        return

    db.add(
        CrawlDecision(
            agency_id=guideline.agency_id,
            config_label=None,
            url=(
                normalize_decision_url(guideline.source_url)
                or guideline.source_url
            )[:1000],
            title=guideline.title[:500],
            outcome=DecisionOutcome.EXCLUDED,
            stage=MANUAL_STAGE,
            reason=reason,
            keyword_matched=None,
        )
    )
