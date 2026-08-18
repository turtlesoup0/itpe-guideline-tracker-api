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
from app.models.crawl_decision import CrawlDecision, DecisionOutcome
from app.models.guideline import Guideline, GuidelineCategory, GuidelineVersion

logger = logging.getLogger(__name__)


# ── 판정 기록 (crawl_decisions) ─────────────────────────
# 수집/제외/보류 판정을 URL 단위로 기록한다.
# - 감사: 제외 항목이 사유와 함께 남아 필터 튜닝 근거가 됨
# - 캐시: EXCLUDED로 기판정된 URL(제목 동일)은 LLM 재호출 없이 스킵
# - 재시도: PENDING(LLM 실패 등)은 다음 크롤에서 자동 재판정


def _upsert_decision(
    db_add,
    cache: dict[str, CrawlDecision],
    *,
    agency_id: int,
    config_label: str,
    item: CrawledItem,
    outcome: DecisionOutcome,
    stage: str,
    reason: str | None = None,
) -> None:
    """판정 행을 갱신하거나 새로 만든다. db_add는 db.add 함수."""
    existing = cache.get(item.url)
    if existing is not None:
        existing.title = item.title[:500]
        existing.config_label = config_label[:200] if config_label else None
        existing.outcome = outcome
        existing.stage = stage
        existing.reason = reason
        existing.keyword_matched = item.keyword_matched
        return
    decision = CrawlDecision(
        agency_id=agency_id,
        config_label=config_label[:200] if config_label else None,
        url=item.url[:1000],
        title=item.title[:500],
        outcome=outcome,
        stage=stage,
        reason=reason,
        keyword_matched=item.keyword_matched,
    )
    db_add(decision)
    cache[item.url] = decision


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


def content_fingerprint(pdf_url: str | None, source_url: str | None) -> str | None:
    """문서 콘텐츠 식별자(fingerprint) 추출.

    동일 문서가 게시일만 바뀌어 재수집될 때 중복 버전 생성을 막기 위해,
    파일 자체를 가리키는 안정적 식별자를 추출한다.

    우선순위:
    1. KOSA(sw.or.kr) cfIdx — 파일 고유 ID
    2. 법제처/일반 게시판 seq / nttNo / bbsNo 등 게시물 ID
    3. 그 외: PDF URL 자체 (쿼리스트링 제외)
    """
    for u in (pdf_url, source_url):
        if not u:
            continue
        # KOSA 파일 ID
        m = re.search(r"cfIdx=(CF\d+)", u)
        if m:
            return f"cf:{m.group(1)}"
    # 게시물 시퀀스 (seq / nttNo)
    for u in (source_url, pdf_url):
        if not u:
            continue
        m = re.search(r"(?:seq|nttNo|bbsNo|fileSeq|fileNo)=(\d+)", u)
        if m:
            return f"seq:{m.group(1)}"
    # fallback: PDF URL의 path 부분 (쿼리 제거)
    if pdf_url:
        return "url:" + pdf_url.split("?")[0]
    return None


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


# ── IT 도메인 관련성 필터 ────────────────────────────────
# 제목이 IT/정보보안/디지털 도메인과 무관하면 수집 제외.
# 키워드 매칭("운영지침" 등)만으로 비IT 행정문서가 수집되는 것을 방지.

# 포괄적 IT 도메인 키워드 (하나라도 있으면 IT 관련으로 간주)
_IT_DOMAIN_RE = re.compile(
    r"정보보호|정보보안|사이버|개인정보|정보주체|가명정보|익명정보|영상정보|생체정보|"
    r"CCTV|위치정보|접근배제|자동화된\s*결정|프라이버시|"
    r"데이터|빅데이터|메타데이터|"
    r"정보통신|통신|방송|전파|주파수|네트워크|망|인터넷|"
    r"소프트웨어|SW|시큐어코딩|코딩|개발보안|보안약점|오픈소스|공개SW|"
    r"클라우드|Cloud|SaaS|PaaS|IaaS|"
    r"인공지능|AI|머신러닝|딥러닝|LLM|생성형|알고리즘|자율주행|로봇|드론|"
    r"전자정부|정보시스템|정보화|정보자원|전자문서|전자서명|전자민원|"
    r"디지털|ICT|IT\b|스마트|모바일|앱\b|플랫폼|"
    r"보안|암호|인증|침해|취약점|해킹|악성|랜섬|포렌식|관제|"
    r"제로트러스트|ISMS|ISMP|ISP|CSAP|N2SF|OT\b|IoT|블록체인|지능정보|"
    r"전자금융|금융보안|핀테크|망분리|"
    r"양자|반도체|우주.*보안|코드표준|표준규격|표준화|영상회의|인터넷전화|"
    r"키오스크|디지털배움|정보격차|디지털포용",
    re.I,
)

# 명백한 비IT 행정/정책 패턴 (모든 기관 공통 제외)
_NON_IT_RE = re.compile(
    r"법정민원|자원봉사|고유가|피해지원금|간첩|"
    r"청렴도|공무원\s*행동강령|보수규정|당직|비상근무|"
    r"회계관계공무원|관직지정|공무국외출장|이해충돌|부패신고|공익신고|"
    r"성희롱|성폭력|스토킹|행동강령|민원행정서비스헌장|기록물\s*관리|"
    r"비영리법인|소관\s*비영리|국민제안|제안\s*운영"
)

# 금융위(FSC) 일반 규제 — IT/보안 무관한 금융 정책 (제외 대상)
_FINANCE_NONIT_RE = re.compile(
    r"자본시장|대부업|여신전문|상장폐지|상장규정|증권|공모주|코너스톤|"
    r"펀드|자산운용|집무규칙|감독규정|운영규칙|업감독|투자자\s*유의|"
    r"ETF|ETN|레버리지|인버스|채무조정|신용정보법|불법사금융|"
    r"금융투자업|보험업|은행업|포용금융|서민금융|금융생활"
)


def is_it_relevant(title: str, agency_code: str | None = None) -> bool:
    """제목이 IT/정보보안 도메인과 관련 있는지 판별.

    - 명백한 비IT 행정 패턴 → False
    - 금융위(FSC): 금융 일반규제 패턴이면서 IT 키워드 없으면 → False
    - IT 도메인 키워드 있으면 → True
    - 그 외: 보수적으로 True (false negative 방지)
    """
    # 1) 명백한 비IT 행정 문서
    if _NON_IT_RE.search(title):
        return False

    has_it = bool(_IT_DOMAIN_RE.search(title))

    # 2) 금융위: 금융 일반규제 + IT 무관 → 제외
    if agency_code == "FSC":
        if _FINANCE_NONIT_RE.search(title) and not has_it:
            return False

    return True


# ── 발표/보도 패턴 (item_type=announcement 판별) ──
# 제목에 가이드라인 키워드가 있어도 "발표/공고/보도자료"성 제목이면
# 실제 문서가 아닌 보도성 게시물로 분류.
_ANNOUNCEMENT_PATTERNS = re.compile(
    r"(?:^|\s|['\"’”])발표(?:$|\s|['\"‘“])|"       # "...가이드라인' 발표"
    r"발표\s*$|"                                    # 제목 끝이 "발표"
    r"보도자료|"
    r"공고$|공고\s|"
    r"배포\s*(?:안내|알림)?\s*$|"                  # "... 배포" "...배포 안내"
    r"제정\s*(?:안내|알림)?\s*$|"
    r"개정\s*(?:안내|알림)?\s*$|"
    r"발간\s*(?:안내|알림)?\s*$"
)


def classify_item_type(title: str) -> str:
    """제목이 실제 가이드라인 문서(guideline)인지 발표/보도(announcement)인지 판별.

    Returns:
        "guideline"     — 실제 문서 (기본)
        "announcement"  — 보도·발표·공고 성격
    """
    if _ANNOUNCEMENT_PATTERNS.search(title):
        return "announcement"
    return "guideline"


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
        # 금융 (자본시장/신용정보/ETF 등 금융 관련은 최우선 — 다른 키워드와 겹치는 경우 많음)
        (re.compile(
            r"전자금융|금융보안|핀테크|자본시장|신용정보|ETF|ELS|IPO|코너스톤|"
            r"상장|증권|채무조정|공모주|펀드|자산운용|보험|은행|여신|대출|"
            r"금융기관|금융회사|금융규제|금융투자|금융감독"
        ), GC.FINANCE),
        # AI (AI 키워드가 다른 도메인과 겹치는 경우 우선 — 단 '금융AI'는 위에서 이미 매칭)
        (re.compile(r"인공지능|AI\b|AI[가-힣]|자율주행|로봇|드론|지능정보|LLM|생성형|메타버스|머신러닝|딥러닝", re.I), GC.AI),
        # 개인정보
        (re.compile(r"개인정보|프라이버시|가명정보|가명.*익명|영상정보|CCTV|생체정보|마이데이터|CPO|위치정보|접근배제|정보주체"), GC.PRIVACY),
        # 정보보안 (해운·항만·기반시설 보안 포함)
        (re.compile(
            r"정보보호|정보보안|사이버|보안모델|취약점|침해|제로트러스트|ISMS|"
            r"암호|시큐어코딩|보안약점|OWASP|CSAP|IoT|보안가이드|"
            r"보안취약|보안인증|보안업무|보안관리|통신비밀|주요정보통신기반|"
            r"해운.*보안|항만.*보안|기반시설.*보안|포렌식|악성|랜섬|해킹"
        ), GC.INFO_SECURITY),
        # 클라우드
        (re.compile(r"클라우드|Cloud|SaaS|PaaS|IaaS"), GC.CLOUD),
        # 소프트웨어
        (re.compile(r"소프트웨어|SW\s|SW사업|SW융합|대가산정|개발보안|공개SW|영향평가|ISP|ISMP|프로젝트\s*규모|오토파일럿|SW안전"), GC.SOFTWARE),
        # 데이터
        (re.compile(r"데이터|빅데이터|공공데이터|품질관리\s*지침|블록체인|전력거래"), GC.DATA),
        # 전자정부
        (re.compile(
            r"전자정부|정보시스템|감리|정보화|웹사이트|UI/UX|표준운영|"
            r"정보자원|코드표준|스마트워크|모바일.*서비스|전자민원|"
            r"스마트빌리지|영상회의|인터넷전화|정보통신서비스|GNS|"
            r"디지털\s*(정부|공공|서비스|포용|혁신|전환)|공공기관|행정"
        ), GC.E_GOV),
    ]


def auto_categorize(title: str, agency_code: str | None = None) -> "GuidelineCategory":
    """제목 기반 카테고리 자동 분류. 매칭 안 되면 기관 힌트로 fallback.

    agency_code: 제목만으로 분류 안 될 때 기관 도메인을 힌트로 사용.
      FSC(금융위), FSI(금융보안원) → FINANCE
      KISA/NIS → INFO_SECURITY
      PIPC → PRIVACY
    """
    global _CATEGORY_RULES
    if not _CATEGORY_RULES:
        _CATEGORY_RULES = _build_category_rules()

    for pattern, category in _CATEGORY_RULES:
        if pattern.search(title):
            return category

    from app.models.guideline import GuidelineCategory
    # 기관 힌트 fallback
    AGENCY_HINT = {
        "FSC": GuidelineCategory.FINANCE,
        "FSI": GuidelineCategory.FINANCE,
        "KISA": GuidelineCategory.INFO_SECURITY,
        "NIS": GuidelineCategory.INFO_SECURITY,
        "PIPC": GuidelineCategory.PRIVACY,
        "SPRI": GuidelineCategory.SOFTWARE,
    }
    if agency_code and agency_code in AGENCY_HINT:
        return AGENCY_HINT[agency_code]
    return GuidelineCategory.OTHER


# ── 메인 동기화 함수 ────────────────────────────────────


async def sync_crawl_results(
    agency_id: int,
    items: list[CrawledItem],
    db: AsyncSession,
    *,
    config_label: str = "",
    agency_name: str = "",
    config_item_type: str = "guideline",
    agency_code: str = "",
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
    url_index: dict[str, Guideline] = {
        g.source_url: g for g in existing_guidelines if g.source_url
    }
    # title → (guideline, set of published_dates, set of content fingerprints)
    title_index: dict[str, tuple[Guideline, set[date], set[str]]] = {}
    # guideline_id → 같은 entry 튜플 (의미 기반 매칭이 id로 찾을 때 사용)
    gid_entry_index: dict[int, tuple[Guideline, set[date], set[str]]] = {}
    for g in existing_guidelines:
        norm = normalize_title(g.title)
        dates = {v.published_date for v in g.versions}
        fps = set()
        for v in g.versions:
            fp = content_fingerprint(v.pdf_url, g.source_url)
            if fp:
                fps.add(fp)
        title_index[norm] = (g, dates, fps)
        gid_entry_index[g.id] = title_index[norm]

    # 임베딩 인덱스는 정규화 매칭이 실패한 항목이 처음 나올 때 lazy 로드
    emb_idx = None

    # 전역 content_hash 인덱스 (모든 기관 교차 — 동일 PDF 다른 출처 탐지)
    # hash → guideline_id (가장 먼저 수집된 원본)
    hash_index: dict[str, int] = {}
    hv_result = await db.execute(
        select(GuidelineVersion.content_hash, GuidelineVersion.guideline_id)
        .where(GuidelineVersion.content_hash.isnot(None))
        .order_by(GuidelineVersion.id)
    )
    for h, gid in hv_result.all():
        hash_index.setdefault(h, gid)

    # 판정 캐시 로드 (이번 배치의 URL만)
    decision_cache: dict[str, CrawlDecision] = {}
    item_urls = [i.url for i in items if i.url]
    if item_urls:
        dec_result = await db.execute(
            select(CrawlDecision).where(CrawlDecision.url.in_(item_urls))
        )
        decision_cache = {d.url: d for d in dec_result.scalars().all()}

    new_count = 0
    updated_count = 0
    skipped_count = 0
    filtered_count = 0
    llm_classified_count = 0
    duplicate_count = 0
    pending_count = 0
    cached_excluded_count = 0
    identity_matched_count = 0

    from app.models.guideline import ItemType
    target_type = (
        ItemType.ANNOUNCEMENT if config_item_type == "announcement"
        else ItemType.GUIDELINE
    )

    for item in items:
        # 캐시: 동일 제목으로 이미 EXCLUDED 판정된 URL → 재판정 없이 스킵.
        # PENDING은 재판정. ACCEPTED(동일 제목)는 아래에서 LLM 생략에 활용.
        cached = decision_cache.get(item.url)
        cache_hit = cached is not None and cached.title == item.title[:500]
        if cache_hit and cached.outcome == DecisionOutcome.EXCLUDED:
            cached_excluded_count += 1
            continue
        pre_accepted = cache_hit and cached.outcome == DecisionOutcome.ACCEPTED

        # 0) URL이 이미 수집돼 있으면 재분류 없이 item_type만 정정 후 스킵.
        # (분류보다 먼저 — 수집 완료 항목에 LLM을 재호출할 이유가 없다)
        if item.url in url_index:
            existing = url_index[item.url]
            if existing.item_type != target_type:
                existing.item_type = target_type
            skipped_count += 1
            continue

        # 0-1) IT 도메인 관련성 — 비IT 행정/정책 문서 제외 (모든 소스 공통)
        if not is_it_relevant(item.title, agency_code):
            _upsert_decision(
                db.add, decision_cache,
                agency_id=agency_id, config_label=config_label, item=item,
                outcome=DecisionOutcome.EXCLUDED, stage="it_domain",
                reason="비IT 도메인 제목",
            )
            filtered_count += 1
            continue

        # 0-2) 분류.
        #    - 기판정 ACCEPTED 캐시가 있으면 재판정 생략.
        #    - announcement 소스: 키워드 매칭 여부와 무관하게 LLM 게이트.
        #      (키워드만 믿으면 '안내·대책·방안' 등 광범위 단어로 비IT 잡음이
        #       대량 유입됨 — 2026-08-18 감사에서 announcement 147건 중
        #       92건이 잡음으로 확인된 데 따른 조치)
        #    - guideline 소스: 제목 3단계(제외 정규식 → 강한 키워드 → LLM).
        accept_reason: str | None = None
        if pre_accepted:
            classification = True
            accept_stage = cached.stage or "cached"
        elif config_item_type == "announcement":
            classification = None
            accept_stage = "announcement_llm"
        else:
            classification = classify_title(item.title)
            accept_stage = "regex_strong"

        if classification is False:
            # Stage 1: 확실한 비-가이드라인
            _upsert_decision(
                db.add, decision_cache,
                agency_id=agency_id, config_label=config_label, item=item,
                outcome=DecisionOutcome.EXCLUDED, stage="regex_exclude",
                reason="제외 패턴 매칭",
            )
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
                    mode=(
                        "announcement" if config_item_type == "announcement"
                        else "guideline"
                    ),
                )
                llm_classified_count += 1

                if result.confidence == "llm_error":
                    # LLM 호출 실패 → 제외가 아니라 보류 (다음 크롤에서 재판정)
                    logger.warning(
                        "LLM 호출 실패, 보류: %s (%s)", item.title[:60], result.reason,
                    )
                    _upsert_decision(
                        db.add, decision_cache,
                        agency_id=agency_id, config_label=config_label, item=item,
                        outcome=DecisionOutcome.PENDING, stage="llm_error",
                        reason=result.reason,
                    )
                    pending_count += 1
                    continue

                if not result.is_guideline:
                    logger.info(
                        "LLM 제외: %s (%s)", item.title[:60], result.reason,
                    )
                    _upsert_decision(
                        db.add, decision_cache,
                        agency_id=agency_id, config_label=config_label, item=item,
                        outcome=DecisionOutcome.EXCLUDED, stage="llm",
                        reason=result.reason,
                    )
                    filtered_count += 1
                    continue

                logger.info(
                    "LLM 수집: %s (%s)", item.title[:60], result.reason,
                )
                accept_stage = "llm"
                accept_reason = result.reason
            except Exception as e:
                # 예기치 못한 오류 → 보류 (다음 크롤에서 재판정)
                logger.warning("LLM 분류 오류, 보류: %s — %s", item.title[:60], e)
                _upsert_decision(
                    db.add, decision_cache,
                    agency_id=agency_id, config_label=config_label, item=item,
                    outcome=DecisionOutcome.PENDING, stage="llm_error",
                    reason=str(e)[:500],
                )
                pending_count += 1
                continue

        # classification is True (Stage 2/캐시) 또는 LLM YES → 수집 진행
        # 분류 판정을 기록 (dedup으로 스킵되더라도 판정 자체는 ACCEPTED)
        if not pre_accepted:
            _upsert_decision(
                db.add, decision_cache,
                agency_id=agency_id, config_label=config_label, item=item,
                outcome=DecisionOutcome.ACCEPTED, stage=accept_stage,
                reason=accept_reason,
            )

        norm_title = normalize_title(item.title)
        pdf_url = _find_pdf_url(item.attachment_urls)
        version_label = extract_version_label(item.title)
        pub_date = item.published_date or date.today()

        # 2) 같은 문서 찾기 — 정규화 제목 exact(빠른 경로) → 의미 기반 판정.
        # 정규식이 못 잡는 변형([현재/과거 안내서] 태그, 새글 배지 N 등)은
        # 임베딩 후보 + LLM 판정(identity.py)이 받아낸다.
        entry = title_index.get(norm_title)
        if entry is None:
            try:
                from app.services.identity import find_same_document, load_embedding_index

                if emb_idx is None:
                    emb_idx = await load_embedding_index(db, agency_id, target_type)
                match_gid = await find_same_document(emb_idx, agency_name, item.title)
                if match_gid is not None and match_gid in gid_entry_index:
                    entry = gid_entry_index[match_gid]
                    identity_matched_count += 1
            except Exception as e:
                # 정체성 판정 실패는 수집을 막지 않는다 (신규로 처리)
                logger.warning("[identity] 판정 실패, 신규로 처리: %s — %s", item.title[:50], e)

        if entry is not None:
            # 같은 문서의 다른 판 → 버전 추가 판단
            existing, existing_dates, existing_fps = entry

            # 콘텐츠 식별자(PDF cfIdx/seq)가 동일하면 같은 문서 → 새 버전 아님.
            # (게시일이 매주 재수집으로 바뀌어도 같은 파일이면 스킵)
            new_fp = content_fingerprint(pdf_url, item.url)
            if new_fp and new_fp in existing_fps:
                if existing.item_type != target_type:
                    existing.item_type = target_type
                skipped_count += 1
                continue

            # fingerprint를 못 구한 경우에만 날짜 기반 fallback
            if new_fp is None and pub_date in existing_dates:
                skipped_count += 1
                continue

            # 새 버전 후보 — PDF 해시 계산 (동일 hash면 같은 문서, 버전 추가 안 함)
            ver_hash: str | None = None
            if pdf_url:
                from app.services.pdf_hash import fetch_pdf_sha256
                ver_hash = await fetch_pdf_sha256(pdf_url)
                # 이 가이드라인의 기존 버전 hash와 같으면 동일 문서 → 스킵
                if ver_hash:
                    existing_hashes = {
                        v.content_hash for v in existing.versions if v.content_hash
                    }
                    if ver_hash in existing_hashes:
                        skipped_count += 1
                        continue

            new_version = GuidelineVersion(
                guideline_id=existing.id,
                version_label=version_label,
                published_date=pub_date,
                pdf_url=pdf_url,
                content_hash=ver_hash,
                detected_at=datetime.now(),
            )
            db.add(new_version)
            # 기존 가이드라인의 item_type도 소스 기준으로 정정
            if existing.item_type != target_type:
                existing.item_type = target_type
            # 인덱스 갱신
            url_index[item.url] = existing
            existing_dates.add(pub_date)
            if new_fp:
                existing_fps.add(new_fp)
            if ver_hash:
                hash_index.setdefault(ver_hash, existing.id)
            updated_count += 1
            continue

        # 3) 신규 가이드라인 — PDF 해시로 교차출처 중복 판정
        content_h: str | None = None
        dup_of: int | None = None
        if pdf_url:
            from app.services.pdf_hash import fetch_pdf_sha256
            content_h = await fetch_pdf_sha256(pdf_url)
            if content_h and content_h in hash_index:
                dup_of = hash_index[content_h]  # 동일 PDF 원본 id

        guideline = Guideline(
            agency_id=agency_id,
            title=item.title,
            category=auto_categorize(item.title, agency_code or None),
            item_type=target_type,
            source_url=item.url,
            pdf_url=pdf_url,
            duplicate_of_id=dup_of,
        )
        db.add(guideline)
        await db.flush()  # id 확보

        first_version = GuidelineVersion(
            guideline_id=guideline.id,
            version_label=version_label,
            published_date=pub_date,
            pdf_url=pdf_url,
            content_hash=content_h,
            detected_at=datetime.now(),
        )
        db.add(first_version)

        # 인덱스 갱신
        url_index[item.url] = guideline
        _new_fp = content_fingerprint(pdf_url, item.url)
        title_index[norm_title] = (
            guideline, {pub_date}, {_new_fp} if _new_fp else set(),
        )
        gid_entry_index[guideline.id] = title_index[norm_title]
        if content_h:
            hash_index.setdefault(content_h, guideline.id)

        # 제목 임베딩 저장 (의미 기반 정체성 판정용) — 실패해도 수집은 계속
        try:
            from app.services.identity import make_embedding_row
            emb_row, emb_vec = make_embedding_row(guideline.id, item.title)
            db.add(emb_row)
            if emb_idx is not None:
                emb_idx.add(guideline.id, item.title, emb_vec)
        except Exception as e:
            logger.warning("[identity] 임베딩 저장 실패 (무시): %s", e)

        if dup_of:
            duplicate_count += 1
            logger.info(
                "[dedup] 교차출처 중복: '%s' (guideline %d) == 원본 %d",
                item.title[:50], guideline.id, dup_of,
            )
        new_count += 1

    return {
        "new": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "filtered": filtered_count,
        "llm_classified": llm_classified_count,
        "duplicate": duplicate_count,
        "pending": pending_count,
        "cached_excluded": cached_excluded_count,
        "identity_matched": identity_matched_count,
    }


# ── 동기 버전 (Celery 태스크용) ──────────────────────────


def sync_crawl_results_sync(
    agency_id: int,
    items: list[CrawledItem],
    db,
    *,
    config_label: str = "",
    agency_name: str = "",
    config_item_type: str = "guideline",
    agency_code: str = "",
) -> dict:
    """sync_crawl_results의 동기 버전 — Celery 동기 세션용.

    async 버전과 동일한 3단계 파이프라인(정규식 + 로컬 LLM 동기 호출)을 적용한다.
    """
    from app.models.guideline import Guideline, GuidelineVersion, ItemType

    # 기존 Guideline 조회 (agency_id로 필터)
    existing = db.query(Guideline).filter(Guideline.agency_id == agency_id).all()

    url_index: dict[str, Guideline] = {g.source_url: g for g in existing if g.source_url}
    title_index: dict[str, tuple[Guideline, set, set]] = {}
    gid_entry_index: dict[int, tuple[Guideline, set, set]] = {}
    for g in existing:
        fps = set()
        for v in g.versions:
            fp = content_fingerprint(v.pdf_url, g.source_url)
            if fp:
                fps.add(fp)
        norm = normalize_title(g.title)
        title_index[norm] = (
            g, {v.published_date for v in g.versions}, fps,
        )
        gid_entry_index[g.id] = title_index[norm]

    # 임베딩 인덱스는 정규화 매칭 실패 항목이 처음 나올 때 lazy 로드
    emb_idx = None

    target_type = (
        ItemType.ANNOUNCEMENT if config_item_type == "announcement"
        else ItemType.GUIDELINE
    )

    # 판정 캐시 로드 (이번 배치의 URL만)
    decision_cache: dict[str, CrawlDecision] = {}
    item_urls = [i.url for i in items if i.url]
    if item_urls:
        rows = (
            db.query(CrawlDecision)
            .filter(CrawlDecision.url.in_(item_urls))
            .all()
        )
        decision_cache = {d.url: d for d in rows}

    new_count = updated_count = skipped_count = filtered_count = 0
    pending_count = cached_excluded_count = llm_classified_count = 0
    identity_matched_count = 0

    for item in items:
        # 캐시: 동일 제목으로 이미 EXCLUDED 판정된 URL → 스킵.
        # ACCEPTED(동일 제목)는 재판정 생략에 활용.
        cached = decision_cache.get(item.url)
        cache_hit = cached is not None and cached.title == item.title[:500]
        if cache_hit and cached.outcome == DecisionOutcome.EXCLUDED:
            cached_excluded_count += 1
            continue
        pre_accepted = cache_hit and cached.outcome == DecisionOutcome.ACCEPTED

        # URL이 이미 수집돼 있으면 재분류 없이 item_type만 정정 후 스킵
        if item.url in url_index:
            ex = url_index[item.url]
            if ex.item_type != target_type:
                ex.item_type = target_type
            skipped_count += 1
            continue

        # IT 도메인 관련성 — 비IT 행정/정책 문서 제외
        if not is_it_relevant(item.title, agency_code):
            _upsert_decision(
                db.add, decision_cache,
                agency_id=agency_id, config_label=config_label, item=item,
                outcome=DecisionOutcome.EXCLUDED, stage="it_domain",
                reason="비IT 도메인 제목",
            )
            filtered_count += 1
            continue

        # 분류: ACCEPTED 캐시 → 생략, announcement 소스 → 전건 LLM 게이트,
        # guideline 소스 → 제목 3단계 (async 버전과 동일 정책)
        accept_reason: str | None = None
        if pre_accepted:
            c = True
            accept_stage = cached.stage or "cached"
        elif config_item_type == "announcement":
            c = None
            accept_stage = "announcement_llm"
        else:
            c = classify_title(item.title)
            accept_stage = "regex_strong"

        if c is False:
            _upsert_decision(
                db.add, decision_cache,
                agency_id=agency_id, config_label=config_label, item=item,
                outcome=DecisionOutcome.EXCLUDED, stage="regex_exclude",
                reason="제외 패턴 매칭",
            )
            filtered_count += 1
            continue

        if c is None:
            # 경계 케이스 → 로컬 LLM (동기 호출, Celery 워커에서 실행)
            from app.services.llm_classifier import classify_with_llm_sync

            result = classify_with_llm_sync(
                title=item.title,
                board_label=config_label,
                agency_name=agency_name,
                detail_url=item.url,
                mode=(
                    "announcement" if config_item_type == "announcement"
                    else "guideline"
                ),
            )
            llm_classified_count += 1

            if result.confidence == "llm_error":
                # LLM 호출 실패 → 제외가 아니라 보류 (다음 크롤에서 재판정)
                logger.warning(
                    "LLM 호출 실패, 보류: %s (%s)", item.title[:60], result.reason,
                )
                _upsert_decision(
                    db.add, decision_cache,
                    agency_id=agency_id, config_label=config_label, item=item,
                    outcome=DecisionOutcome.PENDING, stage="llm_error",
                    reason=result.reason,
                )
                pending_count += 1
                continue

            if not result.is_guideline:
                logger.info("LLM 제외: %s (%s)", item.title[:60], result.reason)
                _upsert_decision(
                    db.add, decision_cache,
                    agency_id=agency_id, config_label=config_label, item=item,
                    outcome=DecisionOutcome.EXCLUDED, stage="llm",
                    reason=result.reason,
                )
                filtered_count += 1
                continue

            logger.info("LLM 수집: %s (%s)", item.title[:60], result.reason)
            accept_stage = "llm"
            accept_reason = result.reason

        # 분류 통과 → 판정 기록
        if not pre_accepted:
            _upsert_decision(
                db.add, decision_cache,
                agency_id=agency_id, config_label=config_label, item=item,
                outcome=DecisionOutcome.ACCEPTED, stage=accept_stage,
                reason=accept_reason,
            )

        norm = normalize_title(item.title)
        pdf_url = _find_pdf_url(item.attachment_urls)
        version_label = extract_version_label(item.title)
        pub_date = item.published_date or date.today()

        # 같은 문서 찾기 — 정규화 제목 exact(빠른 경로) → 의미 기반 판정
        entry = title_index.get(norm)
        if entry is None:
            try:
                from app.services.identity import (
                    find_same_document_sync, load_embedding_index_sync,
                )

                if emb_idx is None:
                    emb_idx = load_embedding_index_sync(db, agency_id, target_type)
                match_gid = find_same_document_sync(emb_idx, agency_name, item.title)
                if match_gid is not None and match_gid in gid_entry_index:
                    entry = gid_entry_index[match_gid]
                    identity_matched_count += 1
            except Exception as e:
                logger.warning("[identity] 판정 실패, 신규로 처리: %s — %s", item.title[:50], e)

        if entry is not None:
            # 같은 문서의 다른 판 → 버전 추가 판단
            ex, dates, fps = entry

            # 콘텐츠 식별자 동일 → 같은 문서, 스킵
            new_fp = content_fingerprint(pdf_url, item.url)
            if new_fp and new_fp in fps:
                if ex.item_type != target_type:
                    ex.item_type = target_type
                skipped_count += 1
                continue
            if new_fp is None and pub_date in dates:
                skipped_count += 1
                continue

            v = GuidelineVersion(
                guideline_id=ex.id,
                version_label=version_label,
                published_date=pub_date,
                pdf_url=pdf_url,
                detected_at=datetime.now(),
            )
            db.add(v)
            if ex.item_type != target_type:
                ex.item_type = target_type
            url_index[item.url] = ex
            dates.add(pub_date)
            if new_fp:
                fps.add(new_fp)
            updated_count += 1
            continue

        # 신규 Guideline
        g = Guideline(
            agency_id=agency_id,
            title=item.title,
            category=auto_categorize(item.title, agency_code or None),
            item_type=target_type,
            source_url=item.url,
            pdf_url=pdf_url,
        )
        db.add(g)
        db.flush()
        v = GuidelineVersion(
            guideline_id=g.id,
            version_label=version_label,
            published_date=pub_date,
            pdf_url=pdf_url,
            detected_at=datetime.now(),
        )
        db.add(v)
        url_index[item.url] = g
        _new_fp = content_fingerprint(pdf_url, item.url)
        title_index[norm] = (g, {pub_date}, {_new_fp} if _new_fp else set())
        gid_entry_index[g.id] = title_index[norm]

        # 제목 임베딩 저장 (의미 기반 정체성 판정용) — 실패해도 수집은 계속
        try:
            from app.services.identity import make_embedding_row
            emb_row, emb_vec = make_embedding_row(g.id, item.title)
            db.add(emb_row)
            if emb_idx is not None:
                emb_idx.add(g.id, item.title, emb_vec)
        except Exception as e:
            logger.warning("[identity] 임베딩 저장 실패 (무시): %s", e)

        new_count += 1

    db.commit()
    return {
        "new": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "filtered": filtered_count,
        "llm_classified": llm_classified_count,
        "pending": pending_count,
        "cached_excluded": cached_excluded_count,
        "identity_matched": identity_matched_count,
    }
