"""
대시보드 요약 API 라우트.

GET /dashboard/summary — 전체 현황을 단일 호출로 반환
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.agency import Agency, CrawlConfig, CrawlRun, CrawlRunStatus
from app.models.guideline import Guideline, GuidelineCategory, GuidelineVersion, LegalBasis, Mandate

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ── Response schemas ─────────────────────────────────────


class AgencySummary(BaseModel):
    code: str
    short_name: str
    name: str
    homepage_url: str
    crawl_target_count: int
    legal_basis_count: int
    guideline_count: int
    last_crawl_at: datetime | None
    last_crawl_status: str | None
    last_crawl_items: int | None

    model_config = {"from_attributes": True}


class CrawlHealthItem(BaseModel):
    agency_code: str
    agency_name: str
    issue: str  # "never_crawled" | "all_failed" | "zero_keyword_match" | "stale"
    detail: str | None = None  # 사람이 읽을 수 있는 경고 상세 (예: "키워드 매칭 0")
    latest_run_at: datetime | None = None
    items_collected: int = 0  # 최근 run에서 수집(크롤 결과) 건수 합계


class DashboardSummary(BaseModel):
    # 상단 요약 카드
    agency_count: int
    legal_basis_count: int
    guideline_count: int          # item_type=guideline 만
    announcement_count: int       # item_type=announcement 만
    recently_updated_count: int  # 최근 30일 변경 가이드라인 수
    gap_missing: int  # 레거시 (항상 0)
    gap_outdated: int  # 레거시 (항상 0)

    # 최종 갱신 정보
    last_global_crawl_at: datetime | None  # 전체 기관 중 가장 최근 크롤 시각
    crawl_health: list[CrawlHealthItem]  # 크롤 건전성 경고

    # 카테고리별 가이드라인 분포
    category_stats: dict[str, int]

    # 유형별 법적 근거
    gosi_count: int
    hunryeong_count: int
    yegyu_count: int

    # 기관별 상세
    agencies: list[AgencySummary]

    # 최근 수집된 법적 근거 (최신 10건)
    recent_legal_bases: list[dict]

    # 최근 수집된 가이드라인 (최신 10건)
    recent_guidelines: list[dict]


# ── Endpoint ─────────────────────────────────────────────


@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """대시보드 전체 현황을 단일 호출로 반환합니다."""

    # ── 기관 + 크롤링 설정 + 크롤링 이력 ──
    agency_result = await db.execute(
        select(Agency)
        .options(selectinload(Agency.crawl_configs), selectinload(Agency.crawl_runs))
        .order_by(Agency.code)
    )
    agencies_db = list(agency_result.scalars().all())

    # ── 기관별 법적 근거 수 ──
    lb_counts_result = await db.execute(
        select(LegalBasis.agency_id, func.count(LegalBasis.id))
        .group_by(LegalBasis.agency_id)
    )
    lb_count_map: dict[int, int] = dict(lb_counts_result.all())

    # ── 기관별 가이드라인 수 ──
    gl_counts_result = await db.execute(
        select(Guideline.agency_id, func.count(Guideline.id))
        .group_by(Guideline.agency_id)
    )
    gl_count_map: dict[int, int] = dict(gl_counts_result.all())

    # ── 전체 카운트 ──
    from app.models.guideline import ItemType
    total_lb = await db.execute(select(func.count(LegalBasis.id)))
    total_gl = await db.execute(
        select(func.count(Guideline.id)).where(Guideline.item_type == ItemType.GUIDELINE)
    )
    total_ann = await db.execute(
        select(func.count(Guideline.id)).where(Guideline.item_type == ItemType.ANNOUNCEMENT)
    )

    # 유형별 법적 근거 수
    lb_type_result = await db.execute(
        select(LegalBasis.basis_type, func.count(LegalBasis.id))
        .group_by(LegalBasis.basis_type)
    )
    lb_type_map = dict(lb_type_result.all())

    # ── 최근 30일 변경 가이드라인 수 ──
    # 실제 발행일(published_date) 기준. 시스템 탐지 시각(detected_at)이 아님 —
    # 시스템이 과거 가이드라인을 "오늘" 처음 수집해도 "최근 발행"이 아니기 때문.
    from datetime import date as date_cls
    cutoff_30d = date_cls.today() - timedelta(days=30)
    recent_update_result = await db.execute(
        select(func.count(func.distinct(GuidelineVersion.guideline_id)))
        .where(GuidelineVersion.published_date >= cutoff_30d)
    )
    recently_updated_count = recent_update_result.scalar() or 0

    # ── 기관별 요약 ──
    agency_summaries: list[dict] = []
    for agency in agencies_db:
        latest_run = (
            max(agency.crawl_runs, key=lambda r: r.started_at)
            if agency.crawl_runs
            else None
        )
        agency_summaries.append({
            "code": agency.code,
            "short_name": agency.short_name,
            "name": agency.name,
            "homepage_url": agency.homepage_url,
            "crawl_target_count": len(agency.crawl_configs),
            "legal_basis_count": lb_count_map.get(agency.id, 0),
            "guideline_count": gl_count_map.get(agency.id, 0),
            "last_crawl_at": latest_run.started_at if latest_run else None,
            "last_crawl_status": latest_run.status.value if latest_run else None,
            "last_crawl_items": latest_run.items_new if latest_run else None,
        })

    # ── 최근 수집된 법적 근거 (최신 10건) ──
    recent_lb_result = await db.execute(
        select(LegalBasis, Agency.short_name)
        .join(Agency, LegalBasis.agency_id == Agency.id)
        .order_by(LegalBasis.created_at.desc())
        .limit(10)
    )
    recent_bases = [
        {
            "id": row[0].id,
            "title": row[0].title,
            "basis_type": row[0].basis_type.value,
            "agency_name": row[1],
            "promulgation_date": row[0].promulgation_date.isoformat() if row[0].promulgation_date else None,
            "created_at": row[0].created_at.isoformat() if row[0].created_at else None,
        }
        for row in recent_lb_result.all()
    ]

    # ── 최근 수집된 가이드라인 (최신 10건) ──
    recent_gl_result = await db.execute(
        select(GuidelineVersion, Guideline.title, Agency.short_name)
        .join(Guideline, GuidelineVersion.guideline_id == Guideline.id)
        .join(Agency, Guideline.agency_id == Agency.id)
        .order_by(GuidelineVersion.detected_at.desc())
        .limit(10)
    )
    recent_guidelines = [
        {
            "id": row[0].id,
            "guideline_id": row[0].guideline_id,
            "title": row[1],
            "agency_name": row[2],
            "version_label": row[0].version_label,
            "published_date": row[0].published_date.isoformat() if row[0].published_date else None,
            "pdf_url": row[0].pdf_url,
            "detected_at": row[0].detected_at.isoformat() if row[0].detected_at else None,
        }
        for row in recent_gl_result.all()
    ]

    # ── 전체 최종 갱신일 + 크롤 건전성 ──
    # 각 config의 "최근 실행" 기준으로 집계하여 기관 전체 상태 종합 판단.
    last_global_crawl_at = None
    crawl_health: list[dict] = []
    from datetime import timezone
    stale_threshold = datetime.now(timezone.utc) - timedelta(days=14)

    # config_id → 최신 CrawlRun 맵
    latest_per_config: dict[int, "CrawlRun"] = {}
    for agency in agencies_db:
        for run in agency.crawl_runs:
            cid = run.config_id
            if cid is None:
                continue
            prev = latest_per_config.get(cid)
            if prev is None or run.started_at > prev.started_at:
                latest_per_config[cid] = run

    for agency in agencies_db:
        # 이 기관의 config들 중 최신 run 모음
        runs = [
            latest_per_config[c.id]
            for c in agency.crawl_configs
            if c.is_active and c.id in latest_per_config
        ]

        if not runs:
            crawl_health.append({
                "agency_code": agency.code,
                "agency_name": agency.short_name,
                "issue": "never_crawled",
                "detail": "크롤 이력 없음",
                "latest_run_at": None,
                "items_collected": 0,
            })
            continue

        max_run_at = max(r.started_at for r in runs)
        if last_global_crawl_at is None or max_run_at > last_global_crawl_at:
            last_global_crawl_at = max_run_at

        total_found = sum((r.items_found or 0) for r in runs)
        succeeded = [r for r in runs if r.status == CrawlRunStatus.SUCCESS]
        failed = [r for r in runs if r.status == CrawlRunStatus.FAILED]

        # 판단 로직
        if runs and not succeeded:
            # 모든 config 실패
            err_samples = ", ".join(
                (r.error_message or "unknown")[:40] for r in failed[:2]
            )
            crawl_health.append({
                "agency_code": agency.code,
                "agency_name": agency.short_name,
                "issue": "all_failed",
                "detail": f"크롤 실패: {err_samples}",
                "latest_run_at": max_run_at,
                "items_collected": 0,
            })
        elif total_found == 0:
            # 크롤 성공했으나 수집 결과 0 (키워드 매칭 없음)
            crawl_health.append({
                "agency_code": agency.code,
                "agency_name": agency.short_name,
                "issue": "zero_keyword_match",
                "detail": f"대상 키워드 매칭 게시물 없음 (게시판 접근은 정상)",
                "latest_run_at": max_run_at,
                "items_collected": 0,
            })
        elif max_run_at < stale_threshold:
            crawl_health.append({
                "agency_code": agency.code,
                "agency_name": agency.short_name,
                "issue": "stale",
                "detail": "최근 2주+ 미실행",
                "latest_run_at": max_run_at,
                "items_collected": total_found,
            })
        # total_found > 0이면 정상 — 경고 없음 (items_new=0이어도 "기존 데이터 유지" 상태)

    # ── 카테고리별 가이드라인 분포 ──
    cat_result = await db.execute(
        select(Guideline.category, func.count(Guideline.id))
        .group_by(Guideline.category)
    )
    category_stats = {row[0].value: row[1] for row in cat_result.all()}

    return {
        "agency_count": len(agencies_db),
        "legal_basis_count": total_lb.scalar() or 0,
        "guideline_count": total_gl.scalar() or 0,
        "announcement_count": total_ann.scalar() or 0,
        "recently_updated_count": recently_updated_count,
        "gap_missing": 0,
        "gap_outdated": 0,
        "last_global_crawl_at": last_global_crawl_at,
        "crawl_health": crawl_health,
        "category_stats": category_stats,
        "gosi_count": lb_type_map.get("gosi", 0),
        "hunryeong_count": lb_type_map.get("hunryeong", 0),
        "yegyu_count": lb_type_map.get("yegyu", 0),
        "agencies": agency_summaries,
        "recent_legal_bases": recent_bases,
        "recent_guidelines": recent_guidelines,
    }
