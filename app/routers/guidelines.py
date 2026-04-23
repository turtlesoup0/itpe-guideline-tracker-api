"""
가이드라인 + 법적 근거 API 라우트.

GET  /guidelines                — 가이드라인 목록 (필터: agency, category, q, sort_by)
GET  /guidelines/recent-changes — 최근 변경된 가이드라인 목록
GET  /guidelines/{id}           — 가이드라인 상세 + 버전 이력
GET  /legal-bases               — 법적 근거(고시/훈령) 목록
GET  /legal-bases/{id}/mandates — 위임 항목 목록
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.guideline import (
    Guideline,
    GuidelineCategory,
    GuidelineVersion,
    ItemType,
    LegalBasis,
    LegalBasisType,
    Mandate,
)

router = APIRouter(tags=["guidelines"])


# ── Response schemas ─────────────────────────────────────


class GuidelineVersionOut(BaseModel):
    id: int
    version_label: str | None
    published_date: date
    pdf_url: str | None
    page_count: int | None
    change_summary: str | None
    significance: str | None

    model_config = {"from_attributes": True}


class GuidelineOut(BaseModel):
    id: int
    agency_id: int
    mandate_id: int | None
    title: str
    category: str
    item_type: str
    description: str | None
    source_url: str | None
    pdf_url: str | None
    latest_published_date: date | None = None
    version_count: int = 0

    model_config = {"from_attributes": True}


class GuidelineDetailOut(GuidelineOut):
    versions: list[GuidelineVersionOut]


class MandateOut(BaseModel):
    id: int
    legal_basis_id: int
    article_ref: str | None
    description: str
    expected_guideline_title: str | None
    guideline_count: int = 0

    model_config = {"from_attributes": True}


class LegalBasisOut(BaseModel):
    id: int
    agency_id: int
    basis_type: str
    title: str
    promulgation_date: date | None
    enforcement_date: date | None
    parent_law_name: str | None
    category: str
    mandate_count: int = 0

    model_config = {"from_attributes": True}


class RecentChangeOut(BaseModel):
    """최근 변경된 가이드라인."""
    guideline_id: int
    title: str
    agency_code: str
    agency_name: str
    category: str
    change_type: str  # "new" | "updated"
    version_label: str | None
    published_date: date | None
    detected_at: datetime
    version_count: int

    model_config = {"from_attributes": True}


# ── Guidelines ───────────────────────────────────────────


@router.get("/guidelines", response_model=list[GuidelineOut])
async def list_guidelines(
    agency_code: str | None = Query(None, description="기관 코드로 필터"),
    category: GuidelineCategory | None = Query(None, description="분야 필터"),
    item_type: ItemType | None = Query(None, description="유형: guideline | announcement"),
    q: str | None = Query(None, description="제목 텍스트 검색"),
    sort_by: str = Query("title", description="정렬: title | latest_date | version_count"),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """가이드라인/발표 목록 조회."""
    from app.models.agency import Agency

    stmt = (
        select(Guideline)
        .join(Agency, Guideline.agency_id == Agency.id)
        .options(selectinload(Guideline.versions))
    )

    if agency_code:
        stmt = stmt.where(Agency.code == agency_code.upper())

    if category:
        stmt = stmt.where(Guideline.category == category)

    if item_type:
        stmt = stmt.where(Guideline.item_type == item_type)

    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Guideline.title.ilike(pattern),
                Agency.short_name.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Guideline.title)
    result = await db.execute(stmt)
    guidelines = list(result.scalars().all())

    items = [
        {
            **{c.key: getattr(g, c.key) for c in Guideline.__table__.columns},
            "latest_published_date": (
                max((v.published_date for v in g.versions), default=None)
                if g.versions else None
            ),
            "version_count": len(g.versions),
        }
        for g in guidelines
    ]

    # 정렬
    if sort_by == "latest_date":
        items.sort(key=lambda x: x["latest_published_date"] or date.min, reverse=True)
    elif sort_by == "version_count":
        items.sort(key=lambda x: x["version_count"], reverse=True)
    # 기본값 title은 이미 DB에서 정렬됨

    return items


@router.get("/guidelines/recent-changes", response_model=list[RecentChangeOut])
async def list_recent_changes(
    days: int = Query(30, ge=1, le=365, description="최근 N일 이내"),
    agency_code: str | None = Query(None, description="기관 코드 필터"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """최근 신규 등록 또는 버전 갱신된 가이드라인 목록."""
    from app.models.agency import Agency

    cutoff = datetime.now() - timedelta(days=days)

    stmt = (
        select(
            GuidelineVersion,
            Guideline,
            Agency.code,
            Agency.short_name,
            func.count(GuidelineVersion.id).over(
                partition_by=GuidelineVersion.guideline_id
            ).label("ver_count"),
        )
        .join(Guideline, GuidelineVersion.guideline_id == Guideline.id)
        .join(Agency, Guideline.agency_id == Agency.id)
        .where(GuidelineVersion.detected_at >= cutoff)
        .order_by(GuidelineVersion.detected_at.desc())
        .limit(limit)
    )

    if agency_code:
        stmt = stmt.where(Agency.code == agency_code.upper())

    result = await db.execute(stmt)
    rows = result.all()

    # guideline_id별 전체 버전 수를 위해 별도 쿼리
    gl_ids = list({row[1].id for row in rows})
    if gl_ids:
        ver_count_result = await db.execute(
            select(
                GuidelineVersion.guideline_id,
                func.count(GuidelineVersion.id),
            )
            .where(GuidelineVersion.guideline_id.in_(gl_ids))
            .group_by(GuidelineVersion.guideline_id)
        )
        ver_count_map = dict(ver_count_result.all())
    else:
        ver_count_map = {}

    return [
        {
            "guideline_id": gl.id,
            "title": gl.title,
            "agency_code": agency_code_val,
            "agency_name": agency_name,
            "category": gl.category.value if hasattr(gl.category, "value") else gl.category,
            "change_type": "new" if ver_count_map.get(gl.id, 1) == 1 else "updated",
            "version_label": ver.version_label,
            "published_date": ver.published_date,
            "detected_at": ver.detected_at,
            "version_count": ver_count_map.get(gl.id, 1),
        }
        for ver, gl, agency_code_val, agency_name, _ in rows
    ]


@router.get("/guidelines/{guideline_id}", response_model=GuidelineDetailOut)
async def get_guideline(
    guideline_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """가이드라인 상세 + 버전 이력."""
    result = await db.execute(
        select(Guideline)
        .options(selectinload(Guideline.versions))
        .where(Guideline.id == guideline_id)
    )
    guideline = result.scalar_one_or_none()
    if not guideline:
        raise HTTPException(status_code=404, detail="Guideline not found")

    return {
        **{c.key: getattr(guideline, c.key) for c in Guideline.__table__.columns},
        "versions": guideline.versions,
    }


# ── Legal Bases ──────────────────────────────────────────


@router.get("/legal-bases", response_model=list[LegalBasisOut])
async def list_legal_bases(
    agency_code: str | None = Query(None),
    basis_type: LegalBasisType | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """법적 근거(고시/훈령) 목록."""
    stmt = (
        select(LegalBasis, func.count(Mandate.id).label("mandate_count"))
        .outerjoin(Mandate)
        .group_by(LegalBasis.id)
    )

    if agency_code:
        from app.models.agency import Agency
        stmt = stmt.join(Agency, LegalBasis.agency_id == Agency.id).where(
            Agency.code == agency_code.upper()
        )

    if basis_type:
        stmt = stmt.where(LegalBasis.basis_type == basis_type)

    stmt = stmt.order_by(LegalBasis.title)
    result = await db.execute(stmt)

    return [
        {
            **{c.key: getattr(row[0], c.key) for c in LegalBasis.__table__.columns},
            "mandate_count": row[1],
        }
        for row in result.all()
    ]


@router.get("/legal-bases/{basis_id}/mandates", response_model=list[MandateOut])
async def list_mandates(
    basis_id: int,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """법적 근거의 위임 항목 목록."""
    stmt = (
        select(Mandate, func.count(Guideline.id).label("guideline_count"))
        .outerjoin(Guideline)
        .where(Mandate.legal_basis_id == basis_id)
        .group_by(Mandate.id)
    )
    result = await db.execute(stmt)

    return [
        {
            **{c.key: getattr(row[0], c.key) for c in Mandate.__table__.columns},
            "guideline_count": row[1],
        }
        for row in result.all()
    ]


# ── Gap Analysis (레거시 — 빈 응답 반환) ────────────────


@router.get("/gaps")
async def get_gaps() -> dict:
    """레거시 갭 분석 — 빈 응답. 프론트엔드에서 /guidelines/recent-changes로 전환 예정."""
    return {
        "total_mandates": 0,
        "missing": 0,
        "outdated": 0,
        "resolved": 0,
        "gaps": [],
    }
