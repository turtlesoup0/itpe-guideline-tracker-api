"""
기관(Agency) + 크롤링 설정/실행 이력 모델.

agencies          추적 대상 기관 (PIPC, KISA, MSIT, ...)
crawl_configs     기관별 크롤링 대상 페이지 설정
crawl_runs        크롤링 실행 이력 (성공/실패, 신규 발견 건수)
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


# ── Enums ────────────────────────────────────────────────


class CrawlSchedule(str, PyEnum):
    """크롤링 주기."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class CrawlSourceType(str, PyEnum):
    """크롤링 소스 유형."""
    RSS = "rss"                    # RSS/Atom 피드
    BBS_LIST = "bbs_list"          # 게시판 목록 페이지 스크래핑
    LAW_API = "law_api"            # 법제처 행정규칙 API


class CrawlRunStatus(str, PyEnum):
    """크롤링 실행 상태."""
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"            # 일부 성공
    FAILED = "failed"


# ── Agency ───────────────────────────────────────────────


class Agency(Base, TimestampMixin):
    """추적 대상 정부 기관."""

    __tablename__ = "agencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, comment="기관 코드 (예: PIPC, KISA)")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="기관 정식명칭")
    short_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="약칭")
    homepage_url: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="주요 발행 분야 설명")

    # Relations
    crawl_configs: Mapped[list["CrawlConfig"]] = relationship(back_populates="agency", cascade="all, delete-orphan")
    crawl_runs: Mapped[list["CrawlRun"]] = relationship(back_populates="agency", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Agency {self.code}: {self.short_name}>"


# ── CrawlConfig ──────────────────────────────────────────


class CrawlConfig(Base, TimestampMixin):
    """기관별 크롤링 대상 페이지 설정.

    하나의 기관이 여러 크롤링 대상을 가질 수 있음.
    예: KISA → [가이드라인 자료실, 보안공지, RSS 피드]
    """

    __tablename__ = "crawl_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, comment="설정 라벨 (예: '가이드라인 자료실')")
    source_type: Mapped[CrawlSourceType] = mapped_column(
        Enum(CrawlSourceType), nullable=False
    )
    schedule: Mapped[CrawlSchedule] = mapped_column(
        Enum(CrawlSchedule), default=CrawlSchedule.WEEKLY
    )

    # 크롤링 파라미터 (소스 타입별 사용)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, comment="대상 URL")
    list_selector: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="게시판 목록 CSS 셀렉터")
    title_selector: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="제목 CSS 셀렉터")
    date_selector: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="날짜 CSS 셀렉터")
    link_selector: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="링크 CSS 셀렉터")
    pagination_param: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="페이지 파라미터명 (예: pageIndex)")
    max_pages: Mapped[int] = mapped_column(Integer, default=3, comment="최대 크롤링 페이지 수")

    # 키워드 필터 (가이드라인/지침/안내서 등 관련 게시물만 수집)
    keyword_filter: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="쉼표 구분 키워드 필터 (예: '가이드라인,지침,안내서,매뉴얼,기준')"
    )

    is_active: Mapped[bool] = mapped_column(default=True)

    # Relations
    agency: Mapped["Agency"] = relationship(back_populates="crawl_configs")

    def __repr__(self) -> str:
        return f"<CrawlConfig {self.agency_id}:{self.label} ({self.source_type.value})>"


# ── CrawlRun ─────────────────────────────────────────────


class CrawlRun(Base):
    """크롤링 실행 이력."""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"), nullable=False)
    config_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_configs.id"), nullable=True)
    status: Mapped[CrawlRunStatus] = mapped_column(Enum(CrawlRunStatus), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_found: Mapped[int] = mapped_column(Integer, default=0, comment="발견된 총 항목 수")
    items_new: Mapped[int] = mapped_column(Integer, default=0, comment="신규 항목 수")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relations
    agency: Mapped["Agency"] = relationship(back_populates="crawl_runs")

    def __repr__(self) -> str:
        return f"<CrawlRun {self.agency_id} {self.status.value} +{self.items_new}>"
