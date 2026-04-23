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
    """Static Publications Page 크롤러의 사이트별 설정."""

    agency_code: str
    config_label: str
    url: str                       # 발간자료 페이지 URL (단일)
    # 각 항목 매칭 regex: group(1)=제목, group(2)=다운로드 seq/id
    # DOTALL 모드 권장 (항목 블록이 여러 줄에 걸침)
    item_regex: re.Pattern
    # 다운로드 URL 템플릿 — {seq} 자리에 추출값 대입
    download_url_template: str
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

    # ── 각 발간자료 항목 추출 ──
    seen_seq: set[str] = set()
    for m in profile.item_regex.finditer(html):
        title = m.group(1).strip()
        seq = m.group(2).strip()
        if not title or not seq:
            continue
        if seq in seen_seq:
            continue                # 동일 seq 중복 방지
        seen_seq.add(seq)

        # 키워드 필터
        if keyword_filter and not any(kw in title for kw in keyword_filter):
            continue

        download_url = profile.download_url_template.format(seq=seq)
        item = CrawledItem(
            title=title,
            url=profile.url,                    # 페이지 자체를 source URL로 (detail 페이지 없음)
            attachment_urls=[download_url],
            published_date=None,                # 페이지에서 개별 날짜 추출 어려움
        )
        result.items.append(item)
        logger.info(f"[{profile.agency_code}] 수집: {title[:60]}")

    result.finished_at = datetime.now()
    logger.info(
        f"[{profile.agency_code}] {profile.config_label} 완료: "
        f"총 {len(seen_seq)}건, 매칭 {len(result.items)}건"
    )
    return result


# ── 사이트별 프로필 레지스트리 ─────────────────────────────


# NIS 사이버·AI안보 발간자료 (img alt + download.do?seq= 패턴)
# 제목과 다운로드 링크가 인접하게 반복되는 블록
_NIS_AF_ITEM_REGEX = re.compile(
    r'<img[^>]*class="border-gray01"[^>]*alt="([^"]+)".*?'
    r'href="/common/download\.do\?seq=([A-F0-9]+)"',
    re.DOTALL,
)

PROFILES: dict[str, list[StaticPubsProfile]] = {
    "NIS": [
        StaticPubsProfile(
            agency_code="NIS",
            config_label="사이버·AI안보 발간자료",
            url="https://www.nis.go.kr:4016/AF/1_7_7_1.do",
            item_regex=_NIS_AF_ITEM_REGEX,
            download_url_template="https://www.nis.go.kr:4016/common/download.do?seq={seq}",
        ),
    ],
    # 향후 동일 패턴 사이트 추가 시 여기에 프로필 추가
}


def get_profiles(agency_code: str) -> list[StaticPubsProfile]:
    """기관의 모든 static_pubs 프로필 반환 (없으면 빈 리스트)."""
    return PROFILES.get(agency_code, [])
