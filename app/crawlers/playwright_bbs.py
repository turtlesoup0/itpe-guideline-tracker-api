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

    # 파싱 모드
    #   "link_list"  : <a> 링크 목록형 (TTA 등 일반 게시판) — 기본
    #   "table_group": 발간물 1건 = <table> 1개, th/td 라벨 구조 (NCSC 신 사이트)
    mode: str = "link_list"

    # ── mode="table_group" 전용 ──
    group_selector: str = "table"             # 항목 1건에 해당하는 컨테이너
    title_label: str = "발간자료 명"           # 제목이 담긴 th 라벨
    date_label: str = "등록일자"               # 날짜가 담긴 th 라벨
    id_attr: str = "data-bbsctt-id"           # 항목 고유 ID 속성
    detail_url_template: str = ""             # {id} → 항목 URL (없으면 list_url#id)

    # ── 진입 방식 ──
    # entry_url + entry_link_text 가 있으면, 먼저 entry_url을 열고
    # 텍스트가 일치하는 링크의 href를 얻어 이동한다 (토큰 URL 대응).
    entry_url: str = ""
    entry_link_text: str = ""


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
    # ── NCSC 신규 사이트 (2026 개편) ──
    # 기존 ncsc.go.kr:4018/main/cop/bbs/... 경로는 404. 새 사이트는
    # JS 렌더링 + 토큰 URL(PageLink.html?token=...) 구조라 Playwright 필요.
    # 게시판 URL이 토큰이라 하드코딩 대신 메인에서 링크 텍스트로 진입한다.
    PlaywrightBbsProfile(
        agency_code="NIS",
        config_label="NCSC 발간자료",
        list_url="https://www.ncsc.go.kr/ko/main/main.html",
        item_link_selector="",              # table_group 모드에선 미사용
        mode="table_group",
        entry_url="https://www.ncsc.go.kr/ko/main/main.html",
        entry_link_text="발간자료",
        group_selector="table",
        title_label="발간자료 명",
        date_label="등록일자",
        id_attr="data-bbsctt-id",
        wait_timeout_ms=45000,   # NCSC는 초기 로딩이 느림
        extra_wait_ms=3000,
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

            # 진입 URL 결정 — entry_url이 있으면 링크 텍스트로 실제 URL을 얻는다.
            # (NCSC처럼 게시판 주소가 토큰 URL이라 하드코딩이 위험한 경우 대응)
            target_url = profile.list_url
            if profile.entry_url and profile.entry_link_text:
                await page.goto(
                    profile.entry_url,
                    wait_until=profile.wait_until,
                    timeout=profile.wait_timeout_ms,
                )
                await page.wait_for_timeout(profile.extra_wait_ms)
                found = await page.eval_on_selector_all(
                    "a[href]",
                    """(els, label) => {
                        const t = els.find(a => (a.innerText || '').trim() === label);
                        return t ? t.href : null;
                    }""",
                    profile.entry_link_text,
                )
                if found:
                    target_url = found
                else:
                    logger.warning(
                        "[%s] entry_link_text '%s' 미발견 — list_url로 fallback",
                        profile.agency_code, profile.entry_link_text,
                    )

            await page.goto(
                target_url,
                wait_until=profile.wait_until,
                timeout=profile.wait_timeout_ms,
            )
            await page.wait_for_timeout(profile.extra_wait_ms)

            # ── mode="table_group": 항목 1건 = 컨테이너 1개 (th 라벨로 필드 식별) ──
            if profile.mode == "table_group":
                groups = await page.query_selector_all(profile.group_selector)
                for grp in groups:
                    fields = await grp.eval_on_selector_all(
                        "tr",
                        """rows => rows.map(r => {
                            const th = r.querySelector('th');
                            const td = r.querySelector('td');
                            return {
                                k: th ? (th.innerText || '').trim() : '',
                                v: td ? (td.innerText || '').trim() : '',
                            };
                        })""",
                    )
                    fmap = {f["k"]: f["v"] for f in fields if f["k"]}
                    title = (fmap.get(profile.title_label) or "").strip()
                    if not title:
                        continue

                    pub_date: date | None = None
                    raw_date = fmap.get(profile.date_label) or ""
                    m = profile.date_regex.search(raw_date)
                    if m:
                        try:
                            pub_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        except ValueError:
                            pass

                    item_id = await grp.eval_on_selector_all(
                        f"[{profile.id_attr}]",
                        f"els => els.length ? els[0].getAttribute('{profile.id_attr}') : null",
                    )
                    if profile.detail_url_template and item_id:
                        item_url = profile.detail_url_template.format(id=item_id)
                    elif item_id:
                        item_url = f"{target_url}#{item_id}"
                    else:
                        item_url = target_url

                    if keyword_filter and not any(kw in title for kw in keyword_filter):
                        continue

                    result.items.append(CrawledItem(
                        title=title,
                        url=item_url,
                        published_date=pub_date,
                    ))

                await browser.close()
                result.finished_at = datetime.now()
                logger.info(
                    "[%s] %s 완료(table_group): %d건",
                    profile.agency_code, profile.config_label, len(result.items),
                )
                return result

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
