"""
크롤러 추상 베이스 클래스.

모든 기관별 크롤러는 BaseCrawler를 상속하여 구현합니다.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from app.config import get_settings


@dataclass
class CrawledItem:
    """크롤링으로 수집된 개별 항목."""

    title: str
    url: str
    published_date: date | None = None
    category: str | None = None          # 게시판 분류 (있는 경우)
    attachment_urls: list[str] = field(default_factory=list)  # PDF 등 첨부파일
    raw_html: str | None = None          # 원본 HTML (디버깅용)

    def __repr__(self) -> str:
        date_str = self.published_date.isoformat() if self.published_date else "?"
        return f"<CrawledItem [{date_str}] {self.title[:50]}>"


@dataclass
class CrawlResult:
    """크롤링 실행 결과."""

    agency_code: str
    config_label: str
    started_at: datetime
    finished_at: datetime | None = None
    items: list[CrawledItem] = field(default_factory=list)
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def count(self) -> int:
        return len(self.items)


class BaseCrawler(ABC):
    """크롤러 추상 클래스.

    각 기관 크롤러는 이 클래스를 상속하고 crawl()을 구현합니다.
    공통 기능: HTTP 클라이언트, 요청 딜레이, User-Agent 관리.
    """

    def __init__(self, agency_code: str, config_label: str = "default") -> None:
        self.agency_code = agency_code
        self.config_label = config_label
        self._settings = get_settings()
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """지연 초기화되는 HTTP 클라이언트."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._settings.crawl_user_agent},
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """HTTP 클라이언트 정리."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch_page(self, url: str) -> str:
        """URL에서 HTML을 가져옵니다. 요청 간 딜레이를 적용합니다."""
        import asyncio
        await asyncio.sleep(self._settings.crawl_request_delay_sec)

        client = await self.get_client()
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    @abstractmethod
    async def crawl(self) -> CrawlResult:
        """크롤링 실행. 서브클래스에서 구현."""
        ...

    async def __aenter__(self) -> "BaseCrawler":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
