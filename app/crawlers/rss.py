"""
RSS/Atom 피드 크롤러.

행안부, 금융위, KISA 등 RSS를 제공하는 기관용.
feedparser 라이브러리로 피드를 파싱하고, 키워드 필터로
가이드라인 관련 항목만 추출합니다.
"""

from datetime import date, datetime

import feedparser

from app.crawlers.base import BaseCrawler, CrawledItem, CrawlResult


class RssCrawler(BaseCrawler):
    """RSS/Atom 피드 기반 크롤러."""

    def __init__(
        self,
        agency_code: str,
        feed_url: str,
        keyword_filter: list[str] | None = None,
        config_label: str = "rss",
    ) -> None:
        super().__init__(agency_code, config_label)
        self.feed_url = feed_url
        self.keyword_filter = keyword_filter or []

    def _matches_keywords(self, title: str) -> bool:
        """제목이 키워드 필터에 매칭되는지 확인.

        키워드 필터가 비어있으면 모든 항목 통과.
        """
        if not self.keyword_filter:
            return True
        title_lower = title.lower()
        return any(kw in title_lower for kw in self.keyword_filter)

    @staticmethod
    def _parse_date(entry: dict) -> date | None:
        """피드 항목의 날짜를 파싱합니다."""
        # feedparser가 parsed 날짜를 제공하면 사용
        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if published_parsed:
            try:
                return date(
                    published_parsed.tm_year,
                    published_parsed.tm_mon,
                    published_parsed.tm_mday,
                )
            except (ValueError, AttributeError):
                pass

        # 문자열 폴백
        date_str = entry.get("published") or entry.get("updated") or ""
        if date_str:
            for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
                try:
                    return datetime.strptime(date_str[:10], fmt).date()
                except ValueError:
                    continue

        return None

    async def crawl(self) -> CrawlResult:
        """RSS 피드를 파싱하여 가이드라인 관련 항목을 추출합니다."""
        started = datetime.now()
        items: list[CrawledItem] = []

        try:
            # RSS XML 가져오기
            xml_text = await self.fetch_page(self.feed_url)
            feed = feedparser.parse(xml_text)

            if feed.bozo and not feed.entries:
                return CrawlResult(
                    agency_code=self.agency_code,
                    config_label=self.config_label,
                    started_at=started,
                    finished_at=datetime.now(),
                    error=f"RSS 파싱 실패: {feed.bozo_exception}",
                )

            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()

                if not title or not link:
                    continue

                # 키워드 필터 적용
                if not self._matches_keywords(title):
                    continue

                # 첨부파일 추출 (enclosure 태그)
                attachments = []
                for enc in entry.get("enclosures", []):
                    href = enc.get("href", "")
                    if href and href.lower().endswith(".pdf"):
                        attachments.append(href)

                items.append(
                    CrawledItem(
                        title=title,
                        url=link,
                        published_date=self._parse_date(entry),
                        category=entry.get("category", None),
                        attachment_urls=attachments,
                    )
                )

            return CrawlResult(
                agency_code=self.agency_code,
                config_label=self.config_label,
                started_at=started,
                finished_at=datetime.now(),
                items=items,
            )

        except Exception as e:
            return CrawlResult(
                agency_code=self.agency_code,
                config_label=self.config_label,
                started_at=started,
                finished_at=datetime.now(),
                error=str(e),
            )
