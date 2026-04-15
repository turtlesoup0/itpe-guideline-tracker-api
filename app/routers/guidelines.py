"""
가이드라인 + 법적 근거 + 갭 분석 API 라우트.

GET  /guidelines                — 가이드라인 목록 (필터: agency, category)
GET  /guidelines/{id}           — 가이드라인 상세 + 버전 이력
GET  /legal-bases               — 법적 근거(고시/훈령) 목록
GET  /legal-bases/{id}/mandates — 위임 항목 목록
GET  /gaps                      — 갭 분석 결과 (미발행/미갱신 가이드라인)
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.guideline import (
    GapAnalysis,
    GapStatus,
    Guideline,
    GuidelineCategory,
    GuidelineVersion,
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
    description: str | None
    source_url: str | None
    pdf_url: str | None

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


class GapOut(BaseModel):
    id: int
    mandate_id: int
    guideline_id: int | None
    status: str
    basis_last_amended: date | None
    guideline_last_updated: date | None
    days_gap: int | None
    note: str | None
    # 조인 필드
    mandate_description: str | None = None
    legal_basis_title: str | None = None

    model_config = {"from_attributes": True}


class GapSummaryOut(BaseModel):
    total_mandates: int
    missing: int
    outdated: int
    resolved: int
    gaps: list[GapOut]


# ── Guidelines ───────────────────────────────────────────


@router.get("/guidelines", response_model=list[GuidelineOut])
async def list_guidelines(
    agency_code: str | None = Query(None, description="기관 코드로 필터"),
    category: GuidelineCategory | None = Query(None, description="분야 필터"),
    db: AsyncSession = Depends(get_db),
) -> list[Guideline]:
    """가이드라인 목록 조회."""
    stmt = select(Guideline)

    if agency_code:
        from app.models.agency import Agency
        stmt = stmt.join(Agency, Guideline.agency_id == Agency.id).where(
            Agency.code == agency_code.upper()
        )

    if category:
        stmt = stmt.where(Guideline.category == category)

    stmt = stmt.order_by(Guideline.title)
    result = await db.execute(stmt)
    return list(result.scalars().all())


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


# ── Gap Analysis ─────────────────────────────────────────


@router.get("/gaps", response_model=GapSummaryOut)
async def get_gaps(
    status: GapStatus | None = Query(None, description="상태 필터"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """갭 분석 — 법적 근거 대비 가이드라인 누락/미갱신 현황."""
    stmt = (
        select(GapAnalysis, Mandate.description, LegalBasis.title)
        .join(Mandate, GapAnalysis.mandate_id == Mandate.id)
        .join(LegalBasis, Mandate.legal_basis_id == LegalBasis.id)
    )

    if status:
        stmt = stmt.where(GapAnalysis.status == status)

    result = await db.execute(stmt)
    rows = result.all()

    gaps = [
        {
            **{c.key: getattr(row[0], c.key) for c in GapAnalysis.__table__.columns},
            "mandate_description": row[1],
            "legal_basis_title": row[2],
        }
        for row in rows
    ]

    # 전체 mandate 수
    total_result = await db.execute(select(func.count(Mandate.id)))
    total_mandates = total_result.scalar() or 0

    return {
        "total_mandates": total_mandates,
        "missing": sum(1 for g in gaps if g["status"] == GapStatus.MISSING),
        "outdated": sum(1 for g in gaps if g["status"] == GapStatus.OUTDATED),
        "resolved": sum(1 for g in gaps if g["status"] == GapStatus.RESOLVED),
        "gaps": gaps,
    }
