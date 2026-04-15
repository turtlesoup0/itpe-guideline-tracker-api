"""
크롤링 결과 → Guideline + GuidelineVersion 자동 저장 서비스.

핵심 로직:
1. source_url 기준으로 기존 가이드라인 존재 여부 확인
2. 신규 → Guideline + 첫 GuidelineVersion 생성
3. 동일 제목 패턴(연도/판 제거 후 비교) + 다른 URL → 기존 Guideline에 새 Version 추가
"""

import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crawlers.base import CrawledItem
from app.models.guideline import Guideline, GuidelineCategory, GuidelineVersion


# ── 제목 정규화 (버전 매칭용) ────────────────────────────


_YEAR_PATTERN = re.compile(
    r"[\(\[\s]?"
    r"(20\d{2})\s*[년판]?"
    r"[\)\]\s]?"
    r"[\s]*(?:개정|수정|제정)?(?:판|본|버전|version|v\d+)?"
    r"[\s]*$",
    re.IGNORECASE,
)

_VERSION_PATTERN = re.compile(
    r"[\s_\-]?v?(\d+(?:\.\d+)?)\s*(?:판|본|버전|version)?"
    r"[\s]*$",
    re.IGNORECASE,
)

_NOISE_PATTERN = re.compile(
    r"[\s]*(안내|배포|공고|공지|게시|알림|발간|제정|개정|수정|전부개정|일부개정)[\s]*$"
)

_PAREN_SUFFIX = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def normalize_title(title: str) -> str:
    """비교용 제목 정규화.

    연도, 버전, 괄호 부가정보, 안내/배포 등 접미사를 제거하여
    같은 가이드라인의 다른 버전을 매칭할 수 있게 합니다.
    """
    t = title.strip()
    # 순서: 괄호 접미사 → 연도/버전 → 노이즈 단어
    t = _PAREN_SUFFIX.sub("", t)
    t = _YEAR_PATTERN.sub("", t)
    t = _VERSION_PATTERN.sub("", t)
    t = _NOISE_PATTERN.sub("", t)
    # 공백 정규화
    t = re.sub(r"\s+", " ", t).strip()
    return t


_PAREN_YEAR = re.compile(r"[\(\[](20\d{2})\s*[년판본]?[^\)\]]*[\)\]]")


def extract_version_label(title: str) -> str | None:
    """제목에서 버전 라벨 추출 (예: '2025년', 'v2.0')."""
    # 괄호 안 연도 우선 (예: "(2025년판)")
    m = _PAREN_YEAR.search(title)
    if m:
        return f"{m.group(1)}년"
    m = _YEAR_PATTERN.search(title)
    if m:
        return f"{m.group(1)}년"
    m = _VERSION_PATTERN.search(title)
    if m:
        return f"v{m.group(1)}"
    return None


def _find_pdf_url(attachment_urls: list[str]) -> str | None:
    """첨부파일 중 PDF URL을 찾습니다."""
    for url in attachment_urls:
        if url.lower().endswith(".pdf") or "pdf" in url.lower():
            return url
    return attachment_urls[0] if attachment_urls else None


# ── 가이드라인 제목 필터링 (오탐 방지) ──────────────────


# 강한 키워드: 제목에 포함되면 가이드라인 문서일 확률이 높은 명칭
_STRONG_KEYWORDS = re.compile(
    r"가이드라인|가이드(?!라인)|안내서|지침서|지침(?!서)|"
    r"핸드북|해설서|매뉴얼|사례집|체크리스트|점검표|"
    r"표준\(?안?\)?|규격|모음집|이용지침"
)

# 제외 컨텍스트: "안내" 키워드가 이런 패턴과 함께 쓰이면 비-가이드라인
_EXCLUDE_PATTERNS = re.compile(
    r"설명회\s*안내|개최\s*안내|모집\s*안내|조사\s*안내|"
    r"공모전?\s*안내|신청\s*안내|세미나\s*안내|포럼\s*안내|"
    r"교육생?\s*안내|과정\s*안내|장애\s*안내|사칭\s*안내|"
    r"주의\s*안내|변경\s*안내|결과\s*안내|청강\s*안내|"
    r"콘퍼런스|해커톤|Hackathon|경진대회|공모전|서포터즈|"
    r"인턴십|공청회|투표단|시상|검증$|공개검증|"
    r"재분류|선정기준을\s*마련|채용자와\s*재직|친인척\s*현황|"
    r"청렴도\s*평가|탄소중립|사전조사|수요조사|만족도\s*조사|"
    r"공표\s*예정일|시스템\s*장애|이름\s*공모|"
    r"사업자\s*선정|수요기관|평가위원|컨설팅\s*지원|"
    r"Alliance|공개\s*모집|참여기업|설명회\s*자료|"
    # ── 비-IT 도메인 오탐 방지 (2026-04-16) ──
    r"오피스텔|지방공무원|지방세|시가표준액|보수업무|"
    r"정주생활지원금|개방형직위|공모직위|지방규제|"
    r"지방별정직|인사운영|민원행정|제도개선\s*기본|"
    r"국가표준.*시행계획|국가표준기본계획|"
    # ── 보도자료·공고·입안예고 ──
    r"합동.*발표$|국제표준\s*됐다|"
    r"시행계획\s*공고|입안예고|"
    # ── 목록·인덱스 페이지 ──
    r"안내서\s*전체\s*목록"
)


def is_guideline_title(title: str) -> bool:
    """제목이 실제 가이드라인 문서인지 판별합니다.

    2단계 필터 (제외 우선):
    1) 제외 패턴에 매칭되면 → 비-가이드라인 (강한 키워드가 있어도 제외)
    2) 강한 키워드가 있으면 → 가이드라인으로 판정
    3) 둘 다 아니면 → 보수적으로 제외
    """
    # 1) 제외 패턴 매칭 → 무조건 비-가이드라인 (우선순위 최상위)
    if _EXCLUDE_PATTERNS.search(title):
        return False

    # 2) 강한 키워드 매칭 → 가이드라인
    if _STRONG_KEYWORDS.search(title):
        return True

    # 3) 나머지: 키워드 필터를 이미 통과한 항목이지만
    #    강한 키워드가 없으므로 보수적으로 제외
    return False


# ── 메인 동기화 함수 ────────────────────────────────────


async def sync_crawl_results(
    agency_id: int,
    items: list[CrawledItem],
    db: AsyncSession,
) -> dict:
    """크롤링 결과를 Guideline + GuidelineVersion으로 변환·저장합니다.

    Returns:
        {"new": 신규 가이드라인 수, "updated": 버전 추가 수, "skipped": 중복 스킵 수}
    """
    if not items:
        return {"new": 0, "updated": 0, "skipped": 0}

    # 기존 가이드라인 로드 (해당 기관) + versions eager load
    existing_result = await db.execute(
        select(Guideline)
        .options(selectinload(Guideline.versions))
        .where(Guideline.agency_id == agency_id)
    )
    existing_guidelines = list(existing_result.scalars().all())

    # 인덱스 구축
    url_index: set[str] = {
        g.source_url for g in existing_guidelines if g.source_url
    }
    # title → (guideline, set of published_dates)
    title_index: dict[str, tuple[Guideline, set[date]]] = {}
    for g in existing_guidelines:
        norm = normalize_title(g.title)
        dates = {v.published_date for v in g.versions}
        title_index[norm] = (g, dates)

    new_count = 0
    updated_count = 0
    skipped_count = 0
    filtered_count = 0

    for item in items:
        # 0) 가이드라인 제목 필터링 (오탐 방지)
        if not is_guideline_title(item.title):
            filtered_count += 1
            continue

        # 1) URL 중복 → 스킵
        if item.url in url_index:
            skipped_count += 1
            continue

        norm_title = normalize_title(item.title)
        pdf_url = _find_pdf_url(item.attachment_urls)
        version_label = extract_version_label(item.title)
        pub_date = item.published_date or date.today()

        # 2) 같은 정규화 제목의 가이드라인 존재 → 버전 추가
        if norm_title in title_index:
            existing, existing_dates = title_index[norm_title]

            # 같은 날짜의 버전이 이미 있으면 스킵
            if pub_date in existing_dates:
                skipped_count += 1
                continue

            new_version = GuidelineVersion(
                guideline_id=existing.id,
                version_label=version_label,
                published_date=pub_date,
                pdf_url=pdf_url,
                detected_at=datetime.now(),
            )
            db.add(new_version)
            # 인덱스 갱신
            url_index.add(item.url)
            existing_dates.add(pub_date)
            updated_count += 1
            continue

        # 3) 신규 가이드라인 생성
        guideline = Guideline(
            agency_id=agency_id,
            title=item.title,
            category=GuidelineCategory.OTHER,
            source_url=item.url,
            pdf_url=pdf_url,
        )
        db.add(guideline)
        await db.flush()  # id 확보

        first_version = GuidelineVersion(
            guideline_id=guideline.id,
            version_label=version_label,
            published_date=pub_date,
            pdf_url=pdf_url,
            detected_at=datetime.now(),
        )
        db.add(first_version)

        # 인덱스 갱신
        url_index.add(item.url)
        title_index[norm_title] = (guideline, {pub_date})
        new_count += 1

    return {"new": new_count, "updated": updated_count, "skipped": skipped_count, "filtered": filtered_count}
