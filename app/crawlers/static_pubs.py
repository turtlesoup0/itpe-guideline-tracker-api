"""
Static Publications Page 크롤러 — 구현 방식별 모듈.

**적용 대상**: 단일 페이지에 여러 발간자료가 SSR로 렌더링된 구조.
list/detail 분리 없이 한 페이지에서 제목 + 다운로드 링크를 모두 얻을 수 있음.

예시:
- 국정원(NIS) 사이버·AI안보 발간자료 (`/AF/1_7_7_1.do`)
- 기타 소수의 공공기관이 "발간자료 모음" 형태로 운영하는 페이지

**전략**:
1. 지정 URL fetch
2. 정규식으로 각 발간자료 항목 블록 추출 (제목 + 다운로드 URL)
3. 키워드 필터로 가이드라인성 항목만 선별
4. CrawlResult 반환

각 사이트별 설정(`StaticPubsProfile`) 추가만으로 재사용.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

from app.config import get_settings
from app.crawlers.base import CrawledItem, CrawlResult

logger = logging.getLogger(__name__)


# ── 프로필 정의 ────────────────────────────────────────────


@dataclass
class StaticPubsProfile:
    """Static Publications Page 크롤러의 사이트별 설정.

    블록 분리 → 각 블록 내에서 title/seq/date 개별 매칭 방식.
    cross-match(항목 경계를 넘어 다음 항목의 필드를 가져오는 오류) 방지.
    """

    agency_code: str
    config_label: str
    url: str                            # 발간자료 페이지 URL (단일)

    # 항목 블록 경계 (lookahead regex). 이 패턴 직전에서 split.
    block_splitter: re.Pattern

    # 각 블록 내에서 개별 필드 추출 regex
    title_regex: re.Pattern             # group(1) = 제목
    seq_regex: re.Pattern               # group(1) = 다운로드 seq/id
    date_regex: Optional[re.Pattern] = None  # group(1) = YYYY-MM-DD (옵션)

    # 다운로드 URL 템플릿 — {seq} 자리에 추출값 대입
    download_url_template: str = ""
    request_timeout: int = 20


# ── 핵심 로직 ──────────────────────────────────────────────


async def crawl_static_pubs(
    profile: StaticPubsProfile,
    keyword_filter: list[str],
) -> CrawlResult:
    """단일 발간자료 페이지를 파싱해 항목 목록을 CrawlResult로 반환."""
    settings = get_settings()
    started_at = datetime.now()
    result = CrawlResult(
        agency_code=profile.agency_code,
        config_label=profile.config_label,
        started_at=started_at,
    )

    try:
        async with httpx.AsyncClient(
            timeout=profile.request_timeout,
            headers={"User-Agent": settings.crawl_user_agent},
            follow_redirects=True,
        ) as client:
            res = await client.get(profile.url)
            if res.status_code != 200:
                result.error = f"HTTP {res.status_code}"
                result.finished_at = datetime.now()
                return result
            html = res.text
    except Exception as e:
        result.error = f"fetch 실패: {e}"
        result.finished_at = datetime.now()
        return result

    # ── 블록 단위 분리 후 각 블록 파싱 ──
    blocks = profile.block_splitter.split(html)
    seen_seq: set[str] = set()
    for block in blocks[1:]:  # 첫 블록은 splitter 이전 = 페이지 헤더
        title_m = profile.title_regex.search(block)
        seq_m = profile.seq_regex.search(block)
        if not title_m or not seq_m:
            continue

        # 제목 정제: <br>, </br>, 다중 공백 정규화
        raw_title = title_m.group(1)
        title = re.sub(r"</?br\s*/?>", " ", raw_title)
        title = re.sub(r"\s+", " ", title).strip()
        seq = seq_m.group(1).strip()
        if not title or not seq or seq in seen_seq:
            continue
        seen_seq.add(seq)

        # 날짜 추출 (프로필 지원 시)
        published_date = None
        if profile.date_regex:
            date_m = profile.date_regex.search(block)
            if date_m:
                try:
                    published_date = datetime.strptime(date_m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    pass

        # 키워드 필터
        if keyword_filter and not any(kw in title for kw in keyword_filter):
            continue

        download_url = (
            profile.download_url_template.format(seq=seq)
            if profile.download_url_template
            else ""
        )
        # source URL은 다운로드 URL로 (seq 단위로 고유 — sync 중복 체크 회피)
        # 다운로드 URL이 없으면 페이지URL#seq 형태로 최소한의 유일성 확보
        source_url = download_url or f"{profile.url}#{seq}"
        item = CrawledItem(
            title=title,
            url=source_url,
            attachment_urls=[download_url] if download_url else [],
            published_date=published_date,
        )
        result.items.append(item)
        logger.info(
            f"[{profile.agency_code}] 수집: "
            f"[{published_date or '?'}] {title[:60]}"
        )

    result.finished_at = datetime.now()
    logger.info(
        f"[{profile.agency_code}] {profile.config_label} 완료: "
        f"총 {len(seen_seq)}건, 매칭 {len(result.items)}건"
    )
    return result


# ── 사이트별 프로필 레지스트리 ─────────────────────────────


# NIS 사이버·AI안보 발간자료 (각 항목: img alt 제목 + download.do seq + 등록일자 행)
_NIS_AF_BLOCK_SPLITTER = re.compile(r'(?=<img[^>]*class="border-gray01"[^>]*alt=")')
_NIS_AF_TITLE = re.compile(r'<img[^>]*class="border-gray01"[^>]*alt="([^"]+)"')
_NIS_AF_SEQ = re.compile(r'href="/common/download\.do\?seq=([A-F0-9]+)"')
_NIS_AF_DATE = re.compile(r'등록일자.*?(\d{4}-\d{2}-\d{2})', re.DOTALL)

# MSIT AI 기본법 가이드라인 (KOSA AI 기본법 지원데스크 통합 페이지)
# 페이지 구조: <a class="guide-item" href="...Download.do?cfIdx=CFxxxxx..."><span>제목</span></a>
_MSIT_AI_BLOCK_SPLITTER = re.compile(r'(?=<a[^>]*class="guide-item")')
_MSIT_AI_TITLE = re.compile(r'<span>([^<]+(?:<br\s*/?>[^<]+|</br>[^<]+)*)</span>')
_MSIT_AI_SEQ = re.compile(r'cfIdx=(CF\d+)')

PROFILES: dict[str, list[StaticPubsProfile]] = {
    "NIS": [
        StaticPubsProfile(
            agency_code="NIS",
            config_label="사이버·AI안보 발간자료",
            url="https://www.nis.go.kr:4016/AF/1_7_7_1.do",
            block_splitter=_NIS_AF_BLOCK_SPLITTER,
            title_regex=_NIS_AF_TITLE,
            seq_regex=_NIS_AF_SEQ,
            date_regex=_NIS_AF_DATE,
            download_url_template="https://www.nis.go.kr:4016/common/download.do?seq={seq}",
        ),
    ],
    "MSIT": [
        StaticPubsProfile(
            agency_code="MSIT",
            config_label="AI 기본법 가이드라인 (KOSA 통합)",
            url="https://www.sw.or.kr/AI_act_helpdesk/main.jsp",
            block_splitter=_MSIT_AI_BLOCK_SPLITTER,
            title_regex=_MSIT_AI_TITLE,
            seq_regex=_MSIT_AI_SEQ,
            download_url_template="https://www.sw.or.kr/common/files/Download.do?cfIdx={seq}&cfGroup=COMMON",
        ),
    ],
    # 향후 동일 패턴 사이트 추가 시 여기에 프로필 추가
}


def get_profiles(agency_code: str) -> list[StaticPubsProfile]:
    """기관의 모든 static_pubs 프로필 반환 (없으면 빈 리스트)."""
    return PROFILES.get(agency_code, [])
