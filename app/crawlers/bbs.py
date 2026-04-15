"""
BBS 게시판 크롤러.

정부기관 게시판(공지사항, 자료실, 법령정보)의 HTML을 파싱하여
가이드라인 관련 게시물을 추출합니다.

각 기관 게시판의 HTML 구조가 다르므로 CrawlConfig의 CSS 셀렉터를
기관별로 설정하여 대응합니다.
"""

import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.crawlers.base import BaseCrawler, CrawledItem, CrawlResult


class BbsCrawler(BaseCrawler):
    """정부기관 게시판 HTML 스크래핑 크롤러."""

    def __init__(
        self,
        agency_code: str,
        base_url: str,
        *,
        list_selector: str | None = None,
        title_selector: str | None = None,
        date_selector: str | None = None,
        link_selector: str | None = None,
        pagination_param: str | None = None,
        max_pages: int = 3,
        keyword_filter: list[str] | None = None,
        config_label: str = "bbs",
    ) -> None:
        super().__init__(agency_code, config_label)
        self.base_url = base_url
        self.list_selector = list_selector
        self.title_selector = title_selector
        self.date_selector = date_selector
        self.link_selector = link_selector
        self.pagination_param = pagination_param
        self.max_pages = max_pages
        self.keyword_filter = keyword_filter or []

    def _matches_keywords(self, title: str) -> bool:
        """키워드 필터 매칭. 필터가 비어있으면 모든 항목 통과."""
        if not self.keyword_filter:
            return True
        title_lower = title.lower()
        return any(kw in title_lower for kw in self.keyword_filter)

    def _build_page_url(self, page_num: int) -> str:
        """페이지네이션 URL 생성."""
        if not self.pagination_param:
            return self.base_url

        sep = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{sep}{self.pagination_param}={page_num}"

    @staticmethod
    def _parse_date_text(text: str) -> date | None:
        """다양한 날짜 형식 파싱.

        정부 사이트에서 흔히 사용하는 형식:
        - 2026-04-15, 2026.04.15, 2026/04/15
        - 20260415
        """
        text = text.strip()

        # YYYYMMDD (8자리)
        if re.match(r"^\d{8}$", text):
            try:
                return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                pass

        # 구분자 포함
        for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue

        # 날짜 패턴 추출 (텍스트 안에 날짜가 섞여 있는 경우)
        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

        return None

    def _extract_items_from_html(self, html: str, page_url: str) -> list[CrawledItem]:
        """HTML에서 게시물 항목을 추출합니다.

        셀렉터가 설정된 경우 해당 셀렉터로, 없으면 범용 휴리스틱으로 추출합니다.
        """
        soup = BeautifulSoup(html, "lxml")
        items: list[CrawledItem] = []

        if self.list_selector:
            rows = soup.select(self.list_selector)
        else:
            # 범용 휴리스틱: 게시판 테이블의 tbody tr 또는 리스트 아이템
            rows = (
                soup.select("table.board_list tbody tr")
                or soup.select("table.bbsList tbody tr")
                or soup.select(".board_list li")
                or soup.select(".bbs_list li")
                or soup.select("table tbody tr")
            )

        for row in rows:
            if not isinstance(row, Tag):
                continue

            item = self._parse_row(row, page_url)
            if item and self._matches_keywords(item.title):
                items.append(item)

        return items

    def _parse_row(self, row: Tag, page_url: str) -> CrawledItem | None:
        """게시판 행(row)에서 제목, URL, 날짜를 추출합니다."""
        # 제목 추출
        title_tag: Tag | None = None
        if self.title_selector:
            title_tag = row.select_one(self.title_selector)
        if not title_tag:
            title_tag = row.select_one("a") or row.select_one("td.title a")

        if not title_tag:
            return None

        # 공백/개행 정제 (정부 사이트 HTML에 불필요한 공백이 많음)
        title = " ".join(title_tag.get_text(strip=True).split())
        if not title:
            return None

        # URL 추출
        link_tag: Tag | None = None
        if self.link_selector:
            link_tag = row.select_one(self.link_selector)
        if not link_tag:
            link_tag = title_tag if title_tag.name == "a" else title_tag.find_parent("a")

        url = ""
        if link_tag and link_tag.get("href"):
            href = str(link_tag["href"])
            url = urljoin(page_url, href)

        # 날짜 추출
        pub_date: date | None = None
        if self.date_selector:
            date_tag = row.select_one(self.date_selector)
            if date_tag:
                pub_date = self._parse_date_text(date_tag.get_text())
        else:
            # 날짜 패턴이 있는 td 찾기
            for td in row.select("td"):
                text = td.get_text(strip=True)
                if re.match(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}", text):
                    pub_date = self._parse_date_text(text)
                    break

        # 첨부파일 (PDF 링크)
        attachments: list[str] = []
        for a_tag in row.select("a[href]"):
            href = str(a_tag.get("href", ""))
            if href.lower().endswith(".pdf"):
                attachments.append(urljoin(page_url, href))

        return CrawledItem(
            title=title,
            url=url,
            published_date=pub_date,
            attachment_urls=attachments,
        )

    async def crawl(self) -> CrawlResult:
        """게시판을 크롤링하여 가이드라인 관련 항목을 추출합니다.

        설정된 max_pages만큼 페이지를 순회합니다.
        """
        started = datetime.now()
        all_items: list[CrawledItem] = []

        try:
            for page_num in range(1, self.max_pages + 1):
                page_url = self._build_page_url(page_num)

                try:
                    html = await self.fetch_page(page_url)
                except Exception as e:
                    # 개별 페이지 실패는 건너뜀
                    if page_num == 1:
                        # 첫 페이지 실패 → 전체 실패
                        return CrawlResult(
                            agency_code=self.agency_code,
                            config_label=self.config_label,
                            started_at=started,
                            finished_at=datetime.now(),
                            error=f"Page 1 fetch failed: {e}",
                        )
                    break

                items = self._extract_items_from_html(html, page_url)

                if not items:
                    # 더 이상 항목이 없으면 중단
                    break

                all_items.extend(items)

            # 중복 제거 (URL 기준)
            seen_urls: set[str] = set()
            unique_items: list[CrawledItem] = []
            for item in all_items:
                if item.url and item.url not in seen_urls:
                    seen_urls.add(item.url)
                    unique_items.append(item)
                elif not item.url:
                    unique_items.append(item)

            return CrawlResult(
                agency_code=self.agency_code,
                config_label=self.config_label,
                started_at=started,
                finished_at=datetime.now(),
                items=unique_items,
            )

        except Exception as e:
            return CrawlResult(
                agency_code=self.agency_code,
                config_label=self.config_label,
                started_at=started,
                finished_at=datetime.now(),
                error=str(e),
            )
