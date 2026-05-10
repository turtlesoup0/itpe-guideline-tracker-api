"""
Playwright 기반 BBS 크롤러 — JavaScript 렌더링이 필요한 SPA 사이트용.

적용: TTA(www.tta.or.kr) 등 Next.js/React SPA로 운영되는 게시판.
일반 httpx+BeautifulSoup으로는 HTML 본문에 데이터가 없음.

구성:
- PlaywrightBbsProfile: 사이트별 셀렉터/날짜 추출 설정
- crawl_playwright_bbs(): chromium headless로 렌더링 후 항목 추출
- PROFILES 레지스트리에 TTA 게시판 등록

요청 사항:
- chromium headless 다운로드 완료 필요 (`playwright install chromium`)
- 메모리 사용 다소 큼 → 동시 실행 1개 (worker_concurrency=1과 자연스럽게 맞음)
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from app.crawlers.base import CrawledItem, CrawlResult

logger = logging.getLogger(__name__)


@dataclass
class PlaywrightBbsProfile:
    """Playwright 기반 BBS 게시판 프로필."""

    agency_code: str
    config_label: str
    list_url: str                            # 게시판 list URL
    item_link_selector: str                  # 게시물 링크 a 태그 셀렉터
    # 행 단위 fallback (link selector가 매칭 안 될 때)
    row_selector: str = "table tbody tr"
    # 날짜 추출: row.inner_text()에서 YYYY.MM.DD 또는 YYYY-MM-DD 추출
    date_regex: re.Pattern = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
    wait_until: str = "networkidle"          # domcontentloaded | load | networkidle
    wait_timeout_ms: int = 20000
    extra_wait_ms: int = 1500                # 렌더링 후 추가 대기


# ── 사이트별 프로필 ────────────────────────────────────────


PROFILES: list[PlaywrightBbsProfile] = [
    PlaywrightBbsProfile(
        agency_code="TTA",
        config_label="보도자료",
        list_url="https://www.tta.or.kr/tta/selectBbsNttList?bbsNo=22&key=11",
        item_link_selector="a[href*='selectBbsNttView']",
    ),
    PlaywrightBbsProfile(
        agency_code="TTA",
        config_label="공지사항",
        list_url="https://www.tta.or.kr/tta/selectBbsNttList?bbsNo=20&key=10",
        item_link_selector="a[href*='selectBbsNttView']",
    ),
    PlaywrightBbsProfile(
        agency_code="TTA",
        config_label="TTA 간행물 (Journal)",
        list_url="https://www.tta.or.kr/tta/selectBbsNttList?bbsNo=24&key=12",
        item_link_selector="a[href*='selectBbsNttView']",
    ),
]


def get_profile_by_url(agency_code: str, list_url: str) -> Optional[PlaywrightBbsProfile]:
    for p in PROFILES:
        if p.agency_code == agency_code and p.list_url == list_url:
            return p
    return None


# ── 크롤러 ─────────────────────────────────────────────────


async def crawl_playwright_bbs(
    profile: PlaywrightBbsProfile,
    keyword_filter: list[str],
) -> CrawlResult:
    """Playwright headless로 게시판 list 페이지를 렌더링하여 항목 추출."""
    from playwright.async_api import async_playwright

    started = datetime.now()
    result = CrawlResult(
        agency_code=profile.agency_code,
        config_label=profile.config_label,
        started_at=started,
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            await page.goto(
                profile.list_url,
                wait_until=profile.wait_until,
                timeout=profile.wait_timeout_ms,
            )
            await page.wait_for_timeout(profile.extra_wait_ms)

            # 1차: link 셀렉터 기반 (가장 안정)
            link_els = await page.query_selector_all(profile.item_link_selector)
            if not link_els:
                # 2차 fallback: row 셀렉터 안에서 a 태그 찾기
                rows = await page.query_selector_all(profile.row_selector)
                link_els = []
                for r in rows:
                    a = await r.query_selector("a")
                    if a:
                        link_els.append(a)

            for el in link_els:
                href = await el.get_attribute("href") or ""
                text = (await el.inner_text() or "").strip()
                if not text or not href:
                    continue
                # 절대 URL
                if href.startswith("/"):
                    href = "https://www.tta.or.kr" + href
                # 행에서 날짜 추출 — 부모 row 찾기
                pub_date: date | None = None
                try:
                    row = await el.evaluate_handle("el => el.closest('tr,li')")
                    if row:
                        row_text = await row.evaluate("el => el.innerText || el.textContent || ''")
                        m = profile.date_regex.search(row_text or "")
                        if m:
                            pub_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except Exception:
                    pass

                # 키워드 필터
                if keyword_filter and not any(kw in text for kw in keyword_filter):
                    continue

                result.items.append(CrawledItem(
                    title=text,
                    url=href,
                    published_date=pub_date,
                ))

            await browser.close()

    except Exception as e:
        logger.error(f"[{profile.agency_code}] Playwright 크롤 실패: {e}")
        result.error = f"Playwright 실패: {e}"

    result.finished_at = datetime.now()
    return result
