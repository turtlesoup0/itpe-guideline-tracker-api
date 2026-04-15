"""
크롤링 API 라우트.

POST /crawl/{agency_code}     — 특정 기관 수동 크롤링 실행
POST /crawl/all               — 전체 기관 크롤링 실행
GET  /crawl/status             — 최근 크롤링 실행 현황
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crawlers.base import CrawlResult
from app.crawlers.bbs import BbsCrawler
from app.crawlers.rss import RssCrawler
from app.db.session import get_db
from app.models.agency import Agency, CrawlConfig, CrawlRun, CrawlRunStatus, CrawlSourceType

router = APIRouter(prefix="/crawl", tags=["crawl"])


# ── Response schemas ─────────────────────────────────────


class CrawlItemOut(BaseModel):
    title: str
    url: str
    published_date: str | None
    attachment_count: int


class CrawlResultOut(BaseModel):
    agency_code: str
    config_label: str
    success: bool
    items_count: int
    items: list[CrawlItemOut]
    error: str | None


class CrawlStatusOut(BaseModel):
    agency_code: str
    agency_name: str
    last_run_at: datetime | None
    last_status: str | None
    last_items_new: int | None


# ── Helpers ──────────────────────────────────────────────


async def _run_config(config: CrawlConfig, agency_code: str) -> CrawlResult:
    """CrawlConfig에 따라 적절한 크롤러를 실행합니다."""
    keyword_list = config.keyword_filter.split(",") if config.keyword_filter else []

    if config.source_type == CrawlSourceType.RSS:
        crawler = RssCrawler(
            agency_code=agency_code,
            feed_url=config.url,
            keyword_filter=keyword_list,
            config_label=config.label,
        )
    elif config.source_type == CrawlSourceType.BBS_LIST:
        crawler = BbsCrawler(
            agency_code=agency_code,
            base_url=config.url,
            list_selector=config.list_selector,
            title_selector=config.title_selector,
            date_selector=config.date_selector,
            link_selector=config.link_selector,
            pagination_param=config.pagination_param,
            max_pages=config.max_pages,
            keyword_filter=keyword_list,
            config_label=config.label,
        )
    else:
        # LAW_API는 별도 구현 예정
        return CrawlResult(
            agency_code=agency_code,
            config_label=config.label,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            error=f"Unsupported source type: {config.source_type}",
        )

    async with crawler:
        return await crawler.crawl()


def _result_to_out(result: CrawlResult) -> CrawlResultOut:
    """CrawlResult → API 응답 변환."""
    return CrawlResultOut(
        agency_code=result.agency_code,
        config_label=result.config_label,
        success=result.success,
        items_count=result.count,
        items=[
            CrawlItemOut(
                title=item.title,
                url=item.url,
                published_date=item.published_date.isoformat() if item.published_date else None,
                attachment_count=len(item.attachment_urls),
            )
            for item in result.items
        ],
        error=result.error,
    )


# ── Endpoints ────────────────────────────────────────────


@router.post("/{agency_code}", response_model=list[CrawlResultOut])
async def crawl_agency(
    agency_code: str,
    db: AsyncSession = Depends(get_db),
) -> list[CrawlResultOut]:
    """특정 기관의 활성 크롤링 설정을 모두 실행합니다."""
    result = await db.execute(
        select(Agency)
        .options(selectinload(Agency.crawl_configs))
        .where(Agency.code == agency_code.upper())
    )
    agency = result.scalar_one_or_none()
    if not agency:
        raise HTTPException(status_code=404, detail=f"Agency '{agency_code}' not found")

    active_configs = [c for c in agency.crawl_configs if c.is_active]
    if not active_configs:
        raise HTTPException(status_code=400, detail="No active crawl configs")

    results: list[CrawlResultOut] = []

    for config in active_configs:
        crawl_result = await _run_config(config, agency.code)

        # DB에 실행 이력 저장
        run = CrawlRun(
            agency_id=agency.id,
            config_id=config.id,
            status=CrawlRunStatus.SUCCESS if crawl_result.success else CrawlRunStatus.FAILED,
            started_at=crawl_result.started_at,
            finished_at=crawl_result.finished_at,
            items_found=crawl_result.count,
            items_new=crawl_result.count,  # TODO: 기존 데이터와 비교하여 실제 신규만 카운트
            error_message=crawl_result.error,
        )
        db.add(run)

        results.append(_result_to_out(crawl_result))

    return results


@router.post("/legal-bases/{agency_code}")
async def crawl_legal_bases(
    agency_code: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """법제처에서 기관의 행정규칙(고시/훈령)을 검색하여 DB에 저장합니다."""
    from app.crawlers.law_api import fetch_and_store_legal_bases
    result = await fetch_and_store_legal_bases(agency_code.upper(), db)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/status", response_model=list[CrawlStatusOut])
async def crawl_status(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """전체 기관의 최근 크롤링 실행 현황."""
    result = await db.execute(
        select(Agency).options(selectinload(Agency.crawl_runs)).order_by(Agency.code)
    )
    agencies = result.scalars().all()

    statuses: list[dict] = []
    for agency in agencies:
        latest_run = max(agency.crawl_runs, key=lambda r: r.started_at, default=None) if agency.crawl_runs else None

        statuses.append({
            "agency_code": agency.code,
            "agency_name": agency.short_name,
            "last_run_at": latest_run.started_at if latest_run else None,
            "last_status": latest_run.status.value if latest_run else None,
            "last_items_new": latest_run.items_new if latest_run else None,
        })

    return statuses
