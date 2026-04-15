"""
9개 추적 대상 기관의 크롤링 설정 레지스트리.

DB 시드 데이터로 사용됩니다. 각 기관별 게시판 URL, CSS 셀렉터,
크롤링 주기, 키워드 필터를 정의합니다.
"""

from dataclasses import dataclass, field


@dataclass
class CrawlTarget:
    """크롤링 대상 페이지 설정."""

    label: str
    source_type: str                       # "rss" | "bbs_list" | "law_api"
    url: str
    schedule: str = "weekly"               # daily | weekly | monthly | quarterly
    list_selector: str | None = None       # 게시판 행(row) CSS 셀렉터
    title_selector: str | None = None
    date_selector: str | None = None
    link_selector: str | None = None
    pagination_param: str | None = None
    max_pages: int = 3
    keyword_filter: list[str] = field(default_factory=list)


@dataclass
class AgencySeed:
    """기관 시드 데이터."""

    code: str
    name: str
    short_name: str
    homepage_url: str
    description: str
    targets: list[CrawlTarget]


# ── 키워드 필터 공통 ─────────────────────────────────────

GUIDELINE_KEYWORDS = [
    "가이드라인", "가이드", "지침", "안내서", "매뉴얼", "기준",
    "해설서", "안내", "표준", "핸드북", "가이드북", "점검표",
]


# ── 9개 기관 시드 데이터 ─────────────────────────────────

AGENCY_SEEDS: list[AgencySeed] = [
    # ─────────────────────────────────────────────────────
    # 1. 개인정보보호위원회 (PIPC)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="PIPC",
        name="개인정보보호위원회",
        short_name="개인정보위",
        homepage_url="https://www.pipc.go.kr",
        description="개인정보 보호 정책 수립, 고시·가이드라인 발행",
        targets=[
            CrawlTarget(
                label="법령정보 (고시·훈령)",
                source_type="bbs_list",
                url="https://www.pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS217&mCode=D010030000",
                schedule="weekly",
                pagination_param="pageIndex",
                max_pages=8,  # 현행 안내서가 4페이지 이후에도 존재
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
            CrawlTarget(
                label="보도자료",
                source_type="bbs_list",
                url="https://www.pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS074&mCode=C020010000",
                schedule="daily",
                pagination_param="pageIndex",
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 2. 과학기술정보통신부 (MSIT)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="MSIT",
        name="과학기술정보통신부",
        short_name="과기정통부",
        homepage_url="https://www.msit.go.kr",
        description="ICT 정책, 정보보호, SW산업 관련 고시·훈령 발행",
        targets=[
            # NOTE: msit.go.kr 게시판은 JS 동적 렌더링 → 정적 크롤링 불가.
            # 법제처 행정규칙 API (kordoc MCP search_admin_rule)로 대체 수집.
            CrawlTarget(
                label="훈령·예규·고시 (법제처 API)",
                source_type="law_api",
                url="https://www.law.go.kr",  # 법제처 API 사용
                schedule="weekly",
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 3. 한국인터넷진흥원 (KISA)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="KISA",
        name="한국인터넷진흥원",
        short_name="KISA",
        homepage_url="https://www.kisa.or.kr",
        description="정보보호·개인정보 기술 가이드라인, 보안 안내서 발행",
        targets=[
            CrawlTarget(
                label="정보보호 안내서",
                source_type="bbs_list",
                url="https://www.kisa.or.kr/2060204",
                schedule="weekly",
                pagination_param="page",
                max_pages=5,
                keyword_filter=[],  # 자료실 자체가 가이드라인 전용
            ),
            CrawlTarget(
                label="정보보호 매뉴얼·사례집",
                source_type="bbs_list",
                url="https://www.kisa.or.kr/2060205",
                schedule="weekly",
                pagination_param="page",
                max_pages=5,
                keyword_filter=[],
            ),
            CrawlTarget(
                label="개인정보보호 가이드라인",
                source_type="bbs_list",
                url="https://www.kisa.or.kr/2060202",
                schedule="weekly",
                pagination_param="page",
                max_pages=5,
                keyword_filter=[],
            ),
            CrawlTarget(
                label="가이드라인 자료실 (통합)",
                source_type="bbs_list",
                url="https://www.kisa.or.kr/2060207",
                schedule="weekly",
                pagination_param="page",
                keyword_filter=[],
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 4. 국가정보원 / 국가사이버안보센터 (NIS)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="NIS",
        name="국가사이버안보센터",
        short_name="NCSC",
        homepage_url="https://www.ncsc.go.kr",
        description="보안적합성 검증, 암호모듈 검증기준, 사이버보안 가이드",
        targets=[
            CrawlTarget(
                label="자료실",
                source_type="bbs_list",
                url="https://www.ncsc.go.kr:4018/main/cop/bbs/selectBoardList.do?bbsId=SecurityAdvice_main",
                schedule="monthly",
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 5. 금융위원회 (FSC)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="FSC",
        name="금융위원회",
        short_name="금융위",
        homepage_url="https://www.fsc.go.kr",
        description="전자금융감독규정, 금융보안 가이드라인 발행",
        targets=[
            CrawlTarget(
                label="보도자료",
                source_type="bbs_list",
                url="https://www.fsc.go.kr/no010101",
                schedule="daily",
                list_selector=".board-list-wrap > ul > li",
                title_selector=".subject a",
                pagination_param="curPage",
                keyword_filter=GUIDELINE_KEYWORDS + ["전자금융", "금융보안", "핀테크"],
            ),
            CrawlTarget(
                label="고시·훈령",
                source_type="bbs_list",
                url="https://www.fsc.go.kr/po040200",
                schedule="weekly",
                list_selector=".board-list-wrap > ul > li",
                title_selector=".subject a",
                pagination_param="curPage",
                max_pages=5,
                keyword_filter=GUIDELINE_KEYWORDS + ["전자금융", "금융보안"],
            ),
            CrawlTarget(
                label="RSS 보도자료",
                source_type="rss",
                url="https://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111",
                schedule="daily",
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 6. 한국지능정보사회진흥원 (NIA)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="NIA",
        name="한국지능정보사회진흥원",
        short_name="NIA",
        homepage_url="https://www.nia.or.kr",
        description="전자정부 표준프레임워크, 정보화 가이드, AI 윤리 가이드",
        targets=[
            CrawlTarget(
                label="발간물",
                source_type="bbs_list",
                url="https://www.nia.or.kr/site/nia_kor/ex/bbs/List.do?cbIdx=39485",
                schedule="monthly",
                list_selector=".board_type01 li",
                title_selector="a",       # a[title] 속성에 제목 포함
                date_selector="span.src",  # "2026.04.13조회수 235" → 날짜 파싱
                pagination_param="pageIndex",
                keyword_filter=[],  # 발간물 페이지 자체가 선별 콘텐츠
            ),
            CrawlTarget(
                label="공지사항",
                source_type="bbs_list",
                url="https://www.nia.or.kr/site/nia_kor/ex/bbs/List.do?cbIdx=99835",
                schedule="weekly",
                list_selector=".board_type01 li",
                title_selector="a",
                date_selector="span.src",
                pagination_param="pageIndex",
                max_pages=15,
                keyword_filter=GUIDELINE_KEYWORDS + ["배포", "발간"],
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 7. 행정안전부 (MOIS)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="MOIS",
        name="행정안전부",
        short_name="행안부",
        homepage_url="https://www.mois.go.kr",
        description="정보시스템 구축·운영 지침, 클라우드 보안, 전자정부 가이드",
        targets=[
            CrawlTarget(
                label="보도자료",
                source_type="bbs_list",
                url="https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardList.do?bbsId=BBSMSTR_000000000008",
                schedule="daily",
                pagination_param="pageIndex",
                keyword_filter=GUIDELINE_KEYWORDS + ["정보시스템", "클라우드", "전자정부"],
            ),
            CrawlTarget(
                label="정보화 표준·지침 자료실",
                source_type="bbs_list",
                url="https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardList.do?bbsId=BBSMSTR_000000000045",
                schedule="weekly",
                pagination_param="pageIndex",
                max_pages=5,
                keyword_filter=[],  # 자료실 자체가 표준·지침 전용
            ),
            CrawlTarget(
                label="훈령·예규·고시",
                source_type="bbs_list",
                url="https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardList.do?bbsId=BBSMSTR_000000000016",
                schedule="weekly",
                pagination_param="pageIndex",
                max_pages=5,
                keyword_filter=GUIDELINE_KEYWORDS + ["정보시스템", "클라우드", "전자정부", "보안"],
            ),
            CrawlTarget(
                label="RSS 피드",
                source_type="rss",
                url="https://www.mois.go.kr/gpms/view/jsp/rss/rss.jsp?ctxCd=1012",
                schedule="daily",
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 8. 소프트웨어정책연구소 (SPRi)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="SPRI",
        name="소프트웨어정책연구소",
        short_name="SPRi",
        homepage_url="https://www.spri.kr",
        description="SW대가기준, SW산업 동향, 정책 연구 보고서",
        targets=[
            CrawlTarget(
                label="발간물",
                source_type="bbs_list",
                url="https://www.spri.kr/posts?code=data_all",
                schedule="monthly",
                list_selector=".com_list_box > ul > li",
                title_selector=".title a",
                date_selector=".data_list_area .list li:first-child .text",
                pagination_param="data_page",
                keyword_filter=[],  # 발간물 페이지 자체가 선별 콘텐츠
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────
    # 9. 방송통신위원회 (KCC)
    # ─────────────────────────────────────────────────────
    AgencySeed(
        code="KCC",
        name="방송통신위원회",
        short_name="방통위",
        homepage_url="https://www.kmcc.go.kr",
        description="이용자보호 기준, 스팸방지 가이드, 통신 관련 고시",
        targets=[
            # NOTE: kcc.go.kr → kmcc.go.kr 리다이렉트 + JS 동적 렌더링 → BBS 크롤링 불가.
            # 법제처 행정규칙 DRF API로 대체 수집.
            CrawlTarget(
                label="고시·훈령 (법제처 API)",
                source_type="law_api",
                url="https://www.law.go.kr",
                schedule="weekly",
                keyword_filter=GUIDELINE_KEYWORDS,
            ),
        ],
    ),
]


def get_agency_seed(code: str) -> AgencySeed | None:
    """기관 코드로 시드 데이터 조회."""
    return next((a for a in AGENCY_SEEDS if a.code == code), None)
