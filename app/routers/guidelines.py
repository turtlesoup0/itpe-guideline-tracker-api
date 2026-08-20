"""
가이드라인 + 법적 근거 API 라우트.

GET  /guidelines                — 가이드라인 목록 (필터: agency, category, q, sort_by, excluded)
POST /guidelines/{id}/exclude   — 수집 제외 처리
POST /guidelines/{id}/restore   — 제외 해제
GET  /guidelines/recent-changes — 최근 변경된 가이드라인 목록
GET  /guidelines/{id}           — 가이드라인 상세 + 버전 이력
GET  /legal-bases               — 법적 근거(고시/훈령) 목록
GET  /legal-bases/{id}/mandates — 위임 항목 목록
"""

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.guideline import (
    ExclusionCategory,
    Guideline,
    GuidelineCategory,
    GuidelineVersion,
    ItemType,
    LegalBasis,
    LegalBasisType,
    Mandate,
)
from app.models.exclusion_rule import ExclusionRuleCandidate, RuleCandidateStatus
from app.services.exclusion import (
    ExclusionError,
    exclude_guideline,
    restore_guideline,
)

router = APIRouter(tags=["guidelines"])


# ── 수집 키워드 공개 엔드포인트 (프론트에서 투명성 표시용) ──


@router.get("/meta/keywords")
async def get_collection_keywords() -> dict:
    """가이드라인/보도자료 수집에 사용되는 키워드 목록.

    프론트엔드 (?) 아이콘 등에서 노출하여 수집 기준을 공개.
    """
    from app.crawlers.registry import GUIDELINE_KEYWORDS, ANNOUNCEMENT_KEYWORDS
    return {
        "guideline": {
            "description": "가이드라인/안내서/매뉴얼 관련 게시판에서 사용하는 키워드",
            "keywords": GUIDELINE_KEYWORDS,
        },
        "announcement": {
            "description": "보도자료/공지사항 게시판에서 가이드라인·법령 발표성 글만 필터링하는 키워드",
            "keywords": ANNOUNCEMENT_KEYWORDS,
        },
    }


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
    duplicate_of_id: int | None = None
    excluded_at: datetime | None = None
    exclusion_category: str | None = None
    exclusion_note: str | None = None
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
    previous_published_date: date | None = None  # 이전 버전 발행일 (있으면)
    previous_version_label: str | None = None
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
    excluded: bool = Query(
        False, description="true면 수집 제외 처리된 항목만 조회 (기본: 제외 안 된 항목만)"
    ),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """가이드라인/발표 목록 조회."""
    from app.models.agency import Agency

    stmt = (
        select(Guideline)
        .join(Agency, Guideline.agency_id == Agency.id)
        .options(selectinload(Guideline.versions))
    )

    # 수집 제외 항목은 기본적으로 목록에서 제외한다.
    stmt = stmt.where(
        Guideline.excluded_at.is_not(None) if excluded
        else Guideline.excluded_at.is_(None)
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


class RuleCandidateOut(BaseModel):
    """제외 규칙 후보 — 근거(support)와 위험(false_positive)을 함께 노출한다."""
    id: int
    pattern: str
    category: str | None
    support_count: int
    false_positive_count: int
    sample_titles: list[str]
    status: str
    reviewed_at: datetime | None

    model_config = {"from_attributes": True}


class ExclusionAnalysisOut(BaseModel):
    excluded_count: int
    by_category: dict[str, int]
    candidates: list[RuleCandidateOut]
    note: str


@router.get("/meta/exclusion-analysis", response_model=ExclusionAnalysisOut)
async def exclusion_analysis(db: AsyncSession = Depends(get_db)) -> dict:
    """수동 제외 현황과, 분석이 도출한 필터 규칙 후보.

    후보는 제안일 뿐이며 자동으로 필터에 반영되지 않는다.
    """
    counts = (
        await db.execute(
            select(Guideline.exclusion_category, func.count(Guideline.id))
            .where(Guideline.excluded_at.is_not(None))
            .group_by(Guideline.exclusion_category)
        )
    ).all()
    by_category = {
        (row[0].value if row[0] is not None else "unspecified"): row[1]
        for row in counts
    }

    rows = (
        await db.execute(
            select(ExclusionRuleCandidate)
            .where(ExclusionRuleCandidate.status == RuleCandidateStatus.PENDING)
            .order_by(
                ExclusionRuleCandidate.support_count.desc(),
                ExclusionRuleCandidate.pattern,
            )
        )
    ).scalars().all()

    candidates = [
        {
            "id": row.id,
            "pattern": row.pattern,
            "category": row.category.value if row.category else None,
            "support_count": row.support_count,
            "false_positive_count": row.false_positive_count,
            "sample_titles": (row.sample_titles or "").splitlines(),
            "status": row.status.value,
            "reviewed_at": row.reviewed_at,
        }
        for row in rows
    ]

    return {
        "excluded_count": sum(by_category.values()),
        "by_category": by_category,
        "candidates": candidates,
        "note": (
            "후보는 제안일 뿐입니다. 승인해야 필터에 반영되며, "
            "활성 항목에 하나라도 걸리는(false_positive>0) 패턴은 후보에 오르지 않습니다."
        ),
    }


@router.post("/meta/exclusion-rules/{candidate_id}/review", response_model=RuleCandidateOut)
async def review_rule_candidate(
    candidate_id: int,
    approve: bool = Query(..., description="true=승인(필터 반영), false=반려"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """규칙 후보를 승인하거나 반려한다."""
    row = (
        await db.execute(
            select(ExclusionRuleCandidate).where(
                ExclusionRuleCandidate.id == candidate_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="규칙 후보를 찾을 수 없습니다.")

    if approve and row.false_positive_count > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"이 패턴은 활성 항목 {row.false_positive_count}건에도 걸립니다. "
                "승인하면 정상 문서가 수집에서 사라집니다."
            ),
        )

    row.status = (
        RuleCandidateStatus.APPROVED if approve else RuleCandidateStatus.REJECTED
    )
    row.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)

    return {
        "id": row.id,
        "pattern": row.pattern,
        "category": row.category.value if row.category else None,
        "support_count": row.support_count,
        "false_positive_count": row.false_positive_count,
        "sample_titles": (row.sample_titles or "").splitlines(),
        "status": row.status.value,
        "reviewed_at": row.reviewed_at,
    }


class ExcludeIn(BaseModel):
    """수집 제외 요청."""
    category: ExclusionCategory
    note: str | None = None


@router.post("/guidelines/{guideline_id}/exclude", response_model=GuidelineOut)
async def exclude(
    guideline_id: int,
    payload: ExcludeIn,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """추적이 불필요한 항목을 수집 제외 처리합니다.

    목록에서 숨기는 동시에 같은 URL의 판정 기록을 남겨 재수집을 막습니다.
    데이터는 지우지 않으므로 restore 로 되돌릴 수 있습니다.
    """
    guideline = await _get_guideline_or_404(db, guideline_id)
    try:
        await exclude_guideline(db, guideline, payload.category, payload.note)
    except ExclusionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await db.commit()
    # commit 으로 인스턴스가 만료된다. 부분 refresh 로 versions 만 되살리면
    # 나머지 컬럼이 직렬화 시점에 지연 로딩돼 async 컨텍스트 밖 IO(MissingGreenlet)가
    # 된다 — versions 까지 eager 로 다시 읽는다.
    return _guideline_dict(await _get_guideline_or_404(db, guideline_id))


@router.post("/guidelines/{guideline_id}/restore", response_model=GuidelineOut)
async def restore(guideline_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """수집 제외를 해제합니다."""
    guideline = await _get_guideline_or_404(db, guideline_id)
    await restore_guideline(db, guideline)
    await db.commit()
    # commit 으로 인스턴스가 만료된다. 부분 refresh 로 versions 만 되살리면
    # 나머지 컬럼이 직렬화 시점에 지연 로딩돼 async 컨텍스트 밖 IO(MissingGreenlet)가
    # 된다 — versions 까지 eager 로 다시 읽는다.
    return _guideline_dict(await _get_guideline_or_404(db, guideline_id))


async def _get_guideline_or_404(db: AsyncSession, guideline_id: int) -> Guideline:
    result = await db.execute(
        select(Guideline)
        .where(Guideline.id == guideline_id)
        .options(selectinload(Guideline.versions))
    )
    guideline = result.scalar_one_or_none()
    if guideline is None:
        raise HTTPException(status_code=404, detail="가이드라인을 찾을 수 없습니다.")
    return guideline


def _guideline_dict(g: Guideline) -> dict:
    return {
        **{c.key: getattr(g, c.key) for c in Guideline.__table__.columns},
        "latest_published_date": (
            max((v.published_date for v in g.versions), default=None)
            if g.versions else None
        ),
        "version_count": len(g.versions),
    }


@router.get("/guidelines/recent-changes", response_model=list[RecentChangeOut])
async def list_recent_changes(
    days: int = Query(30, ge=1, le=365, description="최근 N일 이내"),
    agency_code: str | None = Query(None, description="기관 코드 필터"),
    item_type: ItemType | None = Query(
        ItemType.GUIDELINE,
        description="기본값: guideline (실제 가이드라인 개정이력만). 전체 보려면 '' 전달",
    ),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """최근 신규 등록 또는 버전 갱신된 가이드라인 목록.

    기준은 실제 발행일(published_date). 시스템 탐지 시각(detected_at)이 아님.
    """
    from datetime import date as date_cls
    from app.models.agency import Agency

    cutoff = date_cls.today() - timedelta(days=days)

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
        .where(GuidelineVersion.published_date >= cutoff)
        .where(Guideline.excluded_at.is_(None))
        .order_by(GuidelineVersion.published_date.desc())
        .limit(limit)
    )

    if agency_code:
        stmt = stmt.where(Agency.code == agency_code.upper())
    if item_type:
        stmt = stmt.where(Guideline.item_type == item_type)

    result = await db.execute(stmt)
    rows = result.all()

    # 각 Guideline의 이전 버전(현재 버전보다 과거 발행일 중 최신)을 가져오기
    # guideline_id → [(published_date, version_label), ...] 정렬된 버전 리스트
    gl_ids = list({row[1].id for row in rows})
    version_history: dict[int, list[tuple[date, str | None]]] = {}
    if gl_ids:
        all_versions = await db.execute(
            select(
                GuidelineVersion.guideline_id,
                GuidelineVersion.published_date,
                GuidelineVersion.version_label,
            )
            .where(GuidelineVersion.guideline_id.in_(gl_ids))
            .order_by(GuidelineVersion.guideline_id, GuidelineVersion.published_date.desc())
        )
        for gid, pd, vl in all_versions.all():
            version_history.setdefault(gid, []).append((pd, vl))

    # guideline_id별 전체 버전 수를 위해 별도 쿼리
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

    # 의미 있는 개정 판정 기준:
    # - 이전 버전과 7일 이상 간격, 또는
    # - version_label이 다름 (예: v1 → v2)
    # 그 외는 "재게시"로 취급 (change_type="new"로 처리해도 좋지만
    # 이 경우 단순 reshow라 가장 최근 버전만 한 번 나오게 함 — out에서 중복 억제)

    MIN_UPDATE_GAP_DAYS = 7

    out = []
    for ver, gl, agency_code_val, agency_name, _ in rows:
        # 이전 버전 찾기
        prev_pd = None
        prev_vl = None
        for pd, vl in version_history.get(gl.id, []):
            if pd < ver.published_date:
                prev_pd = pd
                prev_vl = vl
                break

        # 의미 있는 개정인지 판단
        is_meaningful_update = False
        if prev_pd is not None:
            gap_days = (ver.published_date - prev_pd).days
            label_changed = (prev_vl or "") != (ver.version_label or "")
            if gap_days >= MIN_UPDATE_GAP_DAYS or label_changed:
                is_meaningful_update = True

        version_count = ver_count_map.get(gl.id, 1)

        if version_count == 1:
            change_type = "new"
        elif is_meaningful_update:
            change_type = "updated"
        else:
            # multi-version이지만 의미 없는 중복 (같은 게시물 재수집)
            # 해당 항목은 출력 skip
            continue

        out.append({
            "guideline_id": gl.id,
            "title": gl.title,
            "agency_code": agency_code_val,
            "agency_name": agency_name,
            "category": gl.category.value if hasattr(gl.category, "value") else gl.category,
            "change_type": change_type,
            "version_label": ver.version_label,
            "published_date": ver.published_date,
            "previous_published_date": prev_pd,
            "previous_version_label": prev_vl,
            "detected_at": ver.detected_at,
            "version_count": version_count,
        })
    return out


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
