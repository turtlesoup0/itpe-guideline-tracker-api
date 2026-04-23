"""
금융보안원(FSI) 기관 + 내부업무망 SaaS 망분리 예외 보안 해설서 수동 등록.

FSI 웹사이트는 목록 페이지가 JS 렌더링이라 BbsCrawler로 자동 수집 불가.
detail 페이지는 SSR이므로 bbsNo를 알면 개별 등록 가능.

추후 Playwright 기반 FSI 전용 크롤러 도입 시 기관 정보 그대로 사용 가능.

실행:
    source .venv/bin/activate
    python scripts/seed_fsi_manual.py
"""

import asyncio
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.agency import Agency
from app.models.guideline import Guideline, GuidelineCategory, GuidelineVersion


FSI_AGENCY = {
    "code": "FSI",
    "name": "금융보안원",
    "short_name": "금융보안원",
    "homepage_url": "https://www.fsec.or.kr",
    "description": "금융분야 IT 보안 정책·기준·가이드 발행 (금융위 산하)",
}

# 수동 등록 대상 가이드라인 목록
# (list 페이지 JS 렌더링 이슈 해결 전까지 수동 유지보수)
MANUAL_GUIDELINES = [
    {
        "title": "내부업무망 SaaS 망분리 예외 적용에 따른 보안 해설서",
        "category": GuidelineCategory.FINANCE,
        "description": "전자금융감독규정 개정으로 내부업무망에서 SaaS 이용이 가능해짐에 따른 금융권 대상 보안 해설서",
        "source_url": "https://www.fsec.or.kr/bbs/detail?menuNo=222&bbsNo=11929",
        "pdf_url": "https://www.fsec.or.kr/bbs/downloadFile?fileNo=13550&filePage=board",
        "published_date": date(2026, 4, 20),
        "version_label": "v1.0 (2026.04)",
    },
    # 향후 수동 추가 시 여기에 append
]


async def seed_fsi(db: AsyncSession) -> dict:
    # ── 1. FSI 기관 등록 (upsert) ──
    result = await db.execute(select(Agency).where(Agency.code == FSI_AGENCY["code"]))
    agency = result.scalar_one_or_none()

    if agency is None:
        agency = Agency(**FSI_AGENCY)
        db.add(agency)
        await db.flush()
        print(f"[FSI] Agency created: id={agency.id}, code={agency.code}")
    else:
        print(f"[FSI] Agency already exists: id={agency.id}")

    # ── 2. 수동 가이드라인 등록 ──
    created = 0
    skipped = 0
    for gl_data in MANUAL_GUIDELINES:
        existing = await db.execute(
            select(Guideline).where(
                Guideline.agency_id == agency.id,
                Guideline.title == gl_data["title"],
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            print(f"[FSI] Skipped (exists): {gl_data['title']}")
            continue

        guideline = Guideline(
            agency_id=agency.id,
            title=gl_data["title"],
            category=gl_data["category"],
            description=gl_data["description"],
            source_url=gl_data["source_url"],
            pdf_url=gl_data["pdf_url"],
        )
        db.add(guideline)
        await db.flush()

        version = GuidelineVersion(
            guideline_id=guideline.id,
            published_date=gl_data["published_date"],
            version_label=gl_data["version_label"],
            detected_at=datetime.now(timezone.utc),
        )
        db.add(version)

        created += 1
        print(f"[FSI] Created: {gl_data['title']} (gl_id={guideline.id})")

    await db.commit()
    return {"agency_id": agency.id, "created": created, "skipped": skipped}


async def main():
    async with async_session_factory() as db:
        result = await seed_fsi(db)
        print(f"\n완료: {result}")


if __name__ == "__main__":
    asyncio.run(main())
