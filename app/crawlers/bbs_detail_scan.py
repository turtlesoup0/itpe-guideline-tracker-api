"""
BBS Detail Scan 크롤러 — 구현 방식별 모듈.

**적용 대상**: list 페이지가 JavaScript 렌더링이라 BeautifulSoup 파싱 불가하지만,
detail 페이지는 SSR인 공공기관 게시판 (금융보안원, 기타 SPA 기반 사이트).

**전략 (증분 ID 스캔)**:
1. DB의 해당 기관 기존 가이드라인 source_url에서 max sequence ID 추출
2. 기준점 다음부터 scan_window개 bbsNo(또는 유사 ID) 범위 병렬 fetch
3. 오류 페이지(404/에러 템플릿) 제외 → 제목 있는 것만 수집
4. 제목에 지정 키워드 포함 여부로 필터
5. CrawlResult 반환

**각 사이트별 설정(ScanProfile)** 만으로 재사용 가능:
- url_template: detail URL 템플릿 (f-string)
- title_regex, date_regex, error_marker: 파싱 정규식
- file_regex: 첨부파일 추출 정규식 (옵션)
- baseline_id, scan_window: 스캔 범위 제어
- id_param_name: DB URL에서 ID 추출할 파라미터 이름

이 방식의 약한 가정: ID는 대체로 시간순 증가. gap이 있어도 scan_window가 충분히
크면 커버됨. 한 번 실행당 100개 스캔(=FSI 기준 ~2~4주치) 정도면 안전.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crawlers.base import CrawledItem, CrawlResult
from app.db.session import async_session_factory
from app.models.agency import Agency
from app.models.guideline import Guideline

logger = logging.getLogger(__name__)


# ── 스캔 프로필 정의 (사이트별 파서 설정) ────────────────────


@dataclass
class ScanProfile:
    """BBS Detail Scan 크롤러의 사이트별 설정.

    신규 사이트 추가 시 ScanProfile 인스턴스 하나만 만들면 됨.
    """

    agency_code: str
    list_url: str                       # CrawlConfig.url과 매칭하는 키 (list 페이지 URL)
    id_param_name: str                  # URL 쿼리파라미터 이름 (예: "bbsNo")
    url_template: str                   # detail URL 템플릿 (예: "https://.../detail?menuNo=222&bbsNo={id}")
    error_marker: str                   # 에러 페이지 판별 문자열
    title_regex: re.Pattern             # 제목 추출 regex (group 1 = title)
    title_section_marker: str = ""      # 제목 검색 범위 제한 (예: "titleBox"). 비우면 전체
    date_regex: Optional[re.Pattern] = None          # 날짜 추출 (YYYY-MM-DD group 1)
    file_regex: Optional[re.Pattern] = None          # 첨부파일 추출 (file id group 1, filename group 2)
    file_download_template: Optional[str] = None     # 첨부 다운로드 URL 템플릿
    baseline_id: int = 1                # 최초 실행 시 스캔 시작점
    scan_window: int = 100              # 매 실행 시 스캔할 ID 개수
    max_parallel: int = 10              # 동시 요청 제한
    request_timeout: int = 10


# ── 공통 로직 ─────────────────────────────────────────────


async def _get_last_id(
    db: AsyncSession,
    agency_id: int,
    id_param_name: str,
    baseline: int,
) -> int:
    """DB의 해당 기관 source_url에서 최대 ID를 추출. 없으면 baseline."""
    result = await db.execute(
        select(Guideline.source_url).where(Guideline.agency_id == agency_id)
    )
    urls = [row[0] for row in result.all() if row[0]]

    max_id = baseline
    pattern = re.compile(rf"{re.escape(id_param_name)}=(\d+)")
    for url in urls:
        m = pattern.search(url)
        if m:
            max_id = max(max_id, int(m.group(1)))
    return max_id


def _parse_with_profile(html: str, sequence_id: int, profile: ScanProfile) -> Optional[CrawledItem]:
    """프로필 기반 detail 페이지 파싱. 에러/미검출 시 None."""
    if profile.error_marker in html:
        return None

    # 제목 추출 (섹션 마커가 있으면 그 뒤에서만 검색 — 오매칭 방지)
    search_region = html
    if profile.title_section_marker:
        parts = html.split(profile.title_section_marker, 1)
        if len(parts) < 2:
            return None
        search_region = parts[1][:2000]

    title_m = profile.title_regex.search(search_region)
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    # 날짜 (옵션)
    published_date = None
    if profile.date_regex:
        date_m = profile.date_regex.search(html)
        if date_m:
            try:
                published_date = datetime.strptime(date_m.group(1), "%Y-%m-%d").date()
            except ValueError:
                pass

    # 첨부파일 (옵션)
    attachment_urls: list[str] = []
    if profile.file_regex and profile.file_download_template:
        for m in profile.file_regex.finditer(html):
            file_id = m.group(1)
            filename = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
            if filename.lower().endswith((".pdf", ".hwp", ".hwpx")) or not filename:
                attachment_urls.append(profile.file_download_template.format(file_id=file_id))

    return CrawledItem(
        title=title,
        url=profile.url_template.format(id=sequence_id),
        published_date=published_date,
        attachment_urls=attachment_urls,
    )


async def _fetch_one(
    client: httpx.AsyncClient,
    sequence_id: int,
    profile: ScanProfile,
) -> Optional[CrawledItem]:
    """단일 ID의 detail 페이지 fetch + 파싱."""
    url = profile.url_template.format(id=sequence_id)
    try:
        res = await client.get(url)
        if res.status_code != 200:
            return None
        return _parse_with_profile(res.text, sequence_id, profile)
    except Exception as e:
        logger.debug(f"[{profile.agency_code}] id={sequence_id} fetch 실패: {e}")
        return None


async def crawl_bbs_detail_scan(
    profile: ScanProfile,
    keyword_filter: list[str],
    config_label: str = "bbs detail scan",
) -> CrawlResult:
    """ScanProfile 기반 bbsNo 증분 스캔 실행."""
    settings = get_settings()
    started_at = datetime.now()
    result = CrawlResult(
        agency_code=profile.agency_code,
        config_label=config_label,
        started_at=started_at,
    )

    # ── 1. 기관 확인 + last ID 파악 ──
    async with async_session_factory() as db:
        agency_res = await db.execute(select(Agency).where(Agency.code == profile.agency_code))
        agency = agency_res.scalar_one_or_none()
        if not agency:
            result.error = f"Agency {profile.agency_code} not seeded"
            result.finished_at = datetime.now()
            return result
        last_id = await _get_last_id(db, agency.id, profile.id_param_name, profile.baseline_id)

    start = last_id + 1
    end = last_id + profile.scan_window
    logger.info(
        f"[{profile.agency_code}] {profile.id_param_name} 스캔: "
        f"{start}~{end} (last={last_id})"
    )

    # ── 2. 병렬 fetch ──
    sem = asyncio.Semaphore(profile.max_parallel)

    async def bounded(client, seq_id):
        async with sem:
            return seq_id, await _fetch_one(client, seq_id, profile)

    async with httpx.AsyncClient(
        timeout=profile.request_timeout,
        headers={"User-Agent": settings.crawl_user_agent},
        follow_redirects=True,
    ) as client:
        tasks = [bounded(client, n) for n in range(start, end + 1)]
        fetched = await asyncio.gather(*tasks, return_exceptions=False)

    # ── 3. 필터링 ──
    exists = 0
    for seq_id, item in fetched:
        if item is None:
            continue
        exists += 1
        if any(kw in item.title for kw in keyword_filter):
            result.items.append(item)
            logger.info(f"[{profile.agency_code}] 수집: {profile.id_param_name}={seq_id} {item.title[:60]}")

    result.finished_at = datetime.now()
    logger.info(
        f"[{profile.agency_code}] 완료: 스캔 {profile.scan_window}건, 존재 {exists}건, 매칭 {len(result.items)}건"
    )
    return result


# ── 사이트별 프로필 레지스트리 ─────────────────────────────


_FSI_TITLE_RE = re.compile(r"<h3>([^<]+)</h3>")
_FSI_DATE_RE = re.compile(r'class="date"[^>]*>(\d{4}-\d{2}-\d{2})')
_FSI_FILE_RE = re.compile(
    r'fileNo="(\d+)"\s*[^>]*filePage="board"[^>]*>([^<]+)</a>',
    re.DOTALL,
)
_FSI_DOWNLOAD_TPL = "https://www.fsec.or.kr/bbs/downloadFile?fileNo={file_id}&filePage=board"
_FSI_ERROR = "요청하신 페이지에서 오류가 발생"

PROFILES: list[ScanProfile] = [
    # FSI 자료마당 가이드 (menuNo=222)
    ScanProfile(
        agency_code="FSI",
        list_url="https://www.fsec.or.kr/bbs/list?menuNo=222",
        id_param_name="bbsNo",
        url_template="https://www.fsec.or.kr/bbs/detail?menuNo=222&bbsNo={id}",
        error_marker=_FSI_ERROR,
        title_section_marker="titleBox",
        title_regex=_FSI_TITLE_RE,
        date_regex=_FSI_DATE_RE,
        file_regex=_FSI_FILE_RE,
        file_download_template=_FSI_DOWNLOAD_TPL,
        baseline_id=11900,
        scan_window=100,
    ),
    # FSI 알림마당 보도자료 (menuNo=69)
    ScanProfile(
        agency_code="FSI",
        list_url="https://www.fsec.or.kr/bbs/list?menuNo=69",
        id_param_name="bbsNo",
        url_template="https://www.fsec.or.kr/bbs/detail?menuNo=69&bbsNo={id}",
        error_marker=_FSI_ERROR,
        title_section_marker="titleBox",
        title_regex=_FSI_TITLE_RE,
        date_regex=_FSI_DATE_RE,
        file_regex=_FSI_FILE_RE,
        file_download_template=_FSI_DOWNLOAD_TPL,
        baseline_id=11500,
        scan_window=200,
    ),
]


def get_profile_by_url(agency_code: str, list_url: str) -> Optional[ScanProfile]:
    """agency_code + list URL로 프로필 조회 (CrawlConfig.url과 매칭)."""
    for p in PROFILES:
        if p.agency_code == agency_code and p.list_url == list_url:
            return p
    return None


def get_profile(agency_code: str) -> Optional[ScanProfile]:
    """하위 호환: agency_code로 첫 번째 프로필 반환."""
    for p in PROFILES:
        if p.agency_code == agency_code:
            return p
    return None
