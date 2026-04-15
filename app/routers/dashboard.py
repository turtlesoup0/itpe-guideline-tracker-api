"""
대시보드 요약 API 라우트.

GET /dashboard/summary — 전체 현황을 단일 호출로 반환
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.agency import Agency, CrawlConfig, CrawlRun, CrawlRunStatus
from app.models.guideline import GapAnalysis, GapStatus, Guideline, GuidelineVersion, LegalBasis, Mandate

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


class DashboardSummary(BaseModel):
    # 상단 요약 카드
    agency_count: int
    legal_basis_count: int
    guideline_count: int
    gap_missing: int
    gap_outdated: int

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
    total_lb = await db.execute(select(func.count(LegalBasis.id)))
    total_gl = await db.execute(select(func.count(Guideline.id)))

    # 유형별 법적 근거 수
    lb_type_result = await db.execute(
        select(LegalBasis.basis_type, func.count(LegalBasis.id))
        .group_by(LegalBasis.basis_type)
    )
    lb_type_map = dict(lb_type_result.all())

    # ── 갭 분석 ──
    gap_result = await db.execute(
        select(GapAnalysis.status, func.count(GapAnalysis.id))
        .group_by(GapAnalysis.status)
    )
    gap_map = dict(gap_result.all())

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

    return {
        "agency_count": len(agencies_db),
        "legal_basis_count": total_lb.scalar() or 0,
        "guideline_count": total_gl.scalar() or 0,
        "gap_missing": gap_map.get(GapStatus.MISSING, 0),
        "gap_outdated": gap_map.get(GapStatus.OUTDATED, 0),
        "gosi_count": lb_type_map.get("gosi", 0),
        "hunryeong_count": lb_type_map.get("hunryeong", 0),
        "yegyu_count": lb_type_map.get("yegyu", 0),
        "agencies": agency_summaries,
        "recent_legal_bases": recent_bases,
        "recent_guidelines": recent_guidelines,
    }
