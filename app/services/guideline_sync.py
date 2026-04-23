"""
크롤링 결과 → Guideline + GuidelineVersion 자동 저장 서비스.

핵심 로직:
1. source_url 기준으로 기존 가이드라인 존재 여부 확인
2. 신규 → Guideline + 첫 GuidelineVersion 생성
3. 동일 제목 패턴(연도/판 제거 후 비교) + 다른 URL → 기존 Guideline에 새 Version 추가
"""

import logging
import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crawlers.base import CrawledItem
from app.models.guideline import Guideline, GuidelineCategory, GuidelineVersion

logger = logging.getLogger(__name__)


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
    r"표준\(?안?\)?|규격|모음집|이용지침|"
    # 공식 연간 발간물·보고서류
    r"백서|연례보고서|연간보고서|정보보호\s*보고서"
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
    # ── 계획·목록·교육·운영 ──
    r"추진계획\s*[\(（]|추진계획\s*$|종합계획\s*수립|수립결과|선정제품\s*목록|"
    r"교육교재|교육과정\s*안내|인식제고\s*교육|수칙\s*팜플렛|"
    r"운영방안\s*안내|평가\s*운영방안|"
    # ── 목록·인덱스 페이지 ──
    r"안내서\s*전체\s*목록"
)


def classify_title(title: str) -> bool | None:
    """제목 기반 가이드라인 분류 (3단계).

    Returns:
        True  — 확실한 가이드라인 (Stage 2: 강한 키워드)
        False — 확실한 비-가이드라인 (Stage 1: 제외 패턴)
        None  — 판단 불가, LLM 분류 필요 (Stage 3: 경계 케이스)
    """
    # Stage 1) 제외 패턴 매칭 → 무조건 비-가이드라인
    if _EXCLUDE_PATTERNS.search(title):
        return False

    # Stage 2) 강한 키워드 매칭 → 가이드라인
    if _STRONG_KEYWORDS.search(title):
        return True

    # Stage 3) 둘 다 아님 → 경계 케이스, LLM 판단 필요
    return None


# 하위 호환용 래퍼 (기존 코드에서 bool 반환 기대하는 곳용)
def is_guideline_title(title: str) -> bool:
    """classify_title()의 하위 호환 래퍼. None은 False로 처리."""
    result = classify_title(title)
    return result is True


# ── 카테고리 자동분류 ──────────────────────────────────


_CATEGORY_RULES: list[tuple[re.Pattern, "GuidelineCategory"]] = []


def _build_category_rules() -> list[tuple[re.Pattern, "GuidelineCategory"]]:
    """카테고리 분류 규칙 (우선순위 순서, 먼저 매칭되면 확정)."""
    from app.models.guideline import GuidelineCategory as GC

    return [
        # AI (가장 먼저 — AI 키워드가 다른 도메인과 겹치는 경우 우선)
        (re.compile(r"인공지능|AI\b|자율주행|로봇|드론|지능정보|LLM|생성형|메타버스", re.I), GC.AI),
        # 개인정보
        (re.compile(r"개인정보|프라이버시|가명정보|가명.*익명|영상정보|CCTV|생체정보|마이데이터|CPO|위치정보|접근배제|정보주체"), GC.PRIVACY),
        # 정보보안
        (re.compile(
            r"정보보호|정보보안|사이버|보안모델|취약점|침해|제로트러스트|ISMS|"
            r"암호|시큐어코딩|보안약점|OWASP|CSAP|IoT|보안가이드|"
            r"보안취약|보안인증|보안업무|보안관리|통신비밀|주요정보통신기반"
        ), GC.INFO_SECURITY),
        # 클라우드
        (re.compile(r"클라우드|Cloud|SaaS|PaaS|IaaS"), GC.CLOUD),
        # 소프트웨어
        (re.compile(r"소프트웨어|SW\s|SW사업|대가산정|개발보안|공개SW|영향평가|ISP|ISMP"), GC.SOFTWARE),
        # 데이터
        (re.compile(r"데이터|빅데이터|공공데이터|품질관리\s*지침"), GC.DATA),
        # 전자정부
        (re.compile(
            r"전자정부|정보시스템|감리|정보화|웹사이트|UI/UX|표준운영|"
            r"정보자원|코드표준|스마트워크|모바일.*서비스|전자민원|"
            r"스마트빌리지|영상회의|인터넷전화|정보통신서비스|GNS"
        ), GC.E_GOV),
        # 금융
        (re.compile(r"전자금융|금융보안|핀테크|금융"), GC.FINANCE),
    ]


def auto_categorize(title: str) -> "GuidelineCategory":
    """제목 기반 카테고리 자동 분류. 매칭 안 되면 OTHER."""
    global _CATEGORY_RULES
    if not _CATEGORY_RULES:
        _CATEGORY_RULES = _build_category_rules()

    for pattern, category in _CATEGORY_RULES:
        if pattern.search(title):
            return category

    from app.models.guideline import GuidelineCategory
    return GuidelineCategory.OTHER


# ── 메인 동기화 함수 ────────────────────────────────────


async def sync_crawl_results(
    agency_id: int,
    items: list[CrawledItem],
    db: AsyncSession,
    *,
    config_label: str = "",
    agency_name: str = "",
) -> dict:
    """크롤링 결과를 Guideline + GuidelineVersion으로 변환·저장합니다.

    3단계 필터링:
    - Stage 1-2: 정규식 패턴 (즉시, 비용 0)
    - Stage 3: 로컬 Gemma LLM (경계 케이스만, ~0.5초/건)

    Returns:
        {"new": ..., "updated": ..., "skipped": ..., "filtered": ..., "llm_classified": ...}
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
    llm_classified_count = 0

    for item in items:
        # 0) 3단계 가이드라인 분류
        classification = classify_title(item.title)

        if classification is False:
            # Stage 1: 확실한 비-가이드라인
            filtered_count += 1
            continue

        if classification is None:
            # Stage 3: 경계 케이스 → LLM 분류
            try:
                from app.services.llm_classifier import classify_with_llm

                result = await classify_with_llm(
                    title=item.title,
                    board_label=config_label,
                    agency_name=agency_name,
                    detail_url=item.url,
                )
                llm_classified_count += 1

                if not result.is_guideline:
                    logger.info(
                        "LLM 제외: %s (%s)", item.title[:60], result.reason,
                    )
                    filtered_count += 1
                    continue

                logger.info(
                    "LLM 수집: %s (%s)", item.title[:60], result.reason,
                )
            except Exception as e:
                # LLM 실패 시 보수적으로 제외
                logger.warning("LLM 분류 실패, 제외: %s — %s", item.title[:60], e)
                filtered_count += 1
                continue

        # classification is True (Stage 2) 또는 LLM YES → 수집 진행

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
            category=auto_categorize(item.title),
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

    return {
        "new": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "filtered": filtered_count,
        "llm_classified": llm_classified_count,
    }
