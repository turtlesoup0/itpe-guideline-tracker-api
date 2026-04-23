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
from app.services.guideline_sync import sync_crawl_results
from app.services.manifest import regenerate_manifest_async

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

    # Static Publications Page — 단일 URL에 발간자료 다수 나열된 구조
    # (config.url이 프로필의 url과 일치하면 라우팅)
    from app.crawlers.static_pubs import get_profiles as get_static_pubs_profiles
    from app.crawlers.static_pubs import crawl_static_pubs
    for pub_profile in get_static_pubs_profiles(agency_code):
        if config.url == pub_profile.url:
            return await crawl_static_pubs(pub_profile, keyword_filter=keyword_list)

    # BBS Detail Scan 모듈이 지원하는 기관이면 해당 크롤러로 라우팅
    # (list는 JS 렌더링이지만 detail은 SSR인 사이트용 — 프로필 기반)
    from app.crawlers.bbs_detail_scan import get_profile, crawl_bbs_detail_scan
    profile = get_profile(agency_code)
    if profile is not None:
        return await crawl_bbs_detail_scan(
            profile=profile,
            keyword_filter=keyword_list,
            config_label=config.label,
        )

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
    elif config.source_type == CrawlSourceType.LAW_API:
        from app.crawlers.law_api import crawl_admin_rules

        return await crawl_admin_rules(agency_code)
    else:
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

        # 크롤링 성공 시 → 가이드라인 자동 저장 (Stage 3 LLM 분류 포함)
        sync_stats = {"new": 0, "updated": 0, "skipped": 0}
        if crawl_result.success and crawl_result.items:
            sync_stats = await sync_crawl_results(
                agency_id=agency.id,
                items=crawl_result.items,
                db=db,
                config_label=config.label,
                agency_name=agency.name,
            )

        # DB에 실행 이력 저장 (items_new = 실제 신규 가이드라인 수)
        run = CrawlRun(
            agency_id=agency.id,
            config_id=config.id,
            status=CrawlRunStatus.SUCCESS if crawl_result.success else CrawlRunStatus.FAILED,
            started_at=crawl_result.started_at,
            finished_at=crawl_result.finished_at,
            items_found=crawl_result.count,
            items_new=sync_stats["new"] + sync_stats["updated"],
            error_message=crawl_result.error,
        )
        db.add(run)

        results.append(_result_to_out(crawl_result))

    # 크롤링 완료 후 shared manifest 갱신
    try:
        await regenerate_manifest_async(db)
    except Exception as e:
        # manifest 갱신 실패가 크롤 응답을 블로킹하면 안 됨
        import logging
        logging.getLogger(__name__).warning("[manifest] 갱신 실패 (무시): %s", e)

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
