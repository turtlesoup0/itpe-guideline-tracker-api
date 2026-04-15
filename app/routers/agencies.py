"""
기관(Agency) API 라우트.

GET /agencies           — 전체 기관 목록 + 크롤링 설정
GET /agencies/{code}    — 기관 상세 (크롤링 이력 포함)
POST /agencies/seed     — 레지스트리에서 DB로 시드 데이터 투입
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.agency import Agency, CrawlConfig, CrawlRun, CrawlSourceType, CrawlSchedule
from app.crawlers.registry import AGENCY_SEEDS

router = APIRouter(prefix="/agencies", tags=["agencies"])


# ── Response schemas ─────────────────────────────────────


class CrawlConfigOut(BaseModel):
    id: int
    label: str
    source_type: str
    schedule: str
    url: str
    is_active: bool

    model_config = {"from_attributes": True}


class CrawlRunOut(BaseModel):
    id: int
    config_id: int | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    items_found: int
    items_new: int
    error_message: str | None

    model_config = {"from_attributes": True}


class AgencyOut(BaseModel):
    id: int
    code: str
    name: str
    short_name: str
    homepage_url: str
    description: str | None
    crawl_configs: list[CrawlConfigOut]

    model_config = {"from_attributes": True}


class AgencyDetailOut(AgencyOut):
    recent_runs: list[CrawlRunOut]


class SeedResultOut(BaseModel):
    created: int
    skipped: int
    details: list[str]


# ── Endpoints ────────────────────────────────────────────


@router.get("", response_model=list[AgencyOut])
async def list_agencies(db: AsyncSession = Depends(get_db)) -> list[Agency]:
    """전체 기관 목록 조회."""
    result = await db.execute(
        select(Agency).options(selectinload(Agency.crawl_configs)).order_by(Agency.code)
    )
    return list(result.scalars().all())


@router.get("/{code}", response_model=AgencyDetailOut)
async def get_agency(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    """기관 상세 조회 (최근 크롤링 이력 포함)."""
    result = await db.execute(
        select(Agency)
        .options(selectinload(Agency.crawl_configs), selectinload(Agency.crawl_runs))
        .where(Agency.code == code.upper())
    )
    agency = result.scalar_one_or_none()
    if not agency:
        raise HTTPException(status_code=404, detail=f"Agency '{code}' not found")

    # 최근 10건 크롤링 이력
    recent_runs = sorted(agency.crawl_runs, key=lambda r: r.started_at, reverse=True)[:10]

    return {
        **{c.key: getattr(agency, c.key) for c in Agency.__table__.columns},
        "crawl_configs": agency.crawl_configs,
        "recent_runs": recent_runs,
    }


@router.post("/seed", response_model=SeedResultOut)
async def seed_agencies(db: AsyncSession = Depends(get_db)) -> dict:
    """레지스트리의 9개 기관 시드 데이터를 DB에 투입합니다.

    이미 존재하는 기관(code 기준)은 건너뜁니다.
    """
    created = 0
    skipped = 0
    details: list[str] = []

    for seed in AGENCY_SEEDS:
        # 중복 체크
        existing = await db.execute(select(Agency).where(Agency.code == seed.code))
        if existing.scalar_one_or_none():
            skipped += 1
            details.append(f"{seed.code}: skipped (already exists)")
            continue

        # Agency 생성
        agency = Agency(
            code=seed.code,
            name=seed.name,
            short_name=seed.short_name,
            homepage_url=seed.homepage_url,
            description=seed.description,
        )
        db.add(agency)
        await db.flush()  # agency.id 확보

        # CrawlConfig 생성
        for target in seed.targets:
            config = CrawlConfig(
                agency_id=agency.id,
                label=target.label,
                source_type=CrawlSourceType(target.source_type),
                schedule=CrawlSchedule(target.schedule),
                url=target.url,
                list_selector=target.list_selector,
                title_selector=target.title_selector,
                date_selector=target.date_selector,
                link_selector=target.link_selector,
                pagination_param=target.pagination_param,
                max_pages=target.max_pages,
                keyword_filter=",".join(target.keyword_filter) if target.keyword_filter else None,
            )
            db.add(config)

        created += 1
        details.append(f"{seed.code}: created with {len(seed.targets)} crawl configs")

    return {"created": created, "skipped": skipped, "details": details}
