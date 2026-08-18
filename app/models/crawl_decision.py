"""
크롤 판정 기록 — 수집/제외/보류 결정의 감사 로그 + 재판정 캐시.

목적:
1. 감사(audit): 무엇이 어떤 사유로 제외됐는지 기록 → 필터 튜닝 근거 확보.
   (기존에는 크롤 시점 keyword_filter 탈락 항목이 기록 없이 소실됨)
2. 캐시: 이미 판정한 URL은 재크롤 시 LLM 재호출 없이 스킵.
"""

from enum import Enum as PyEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DecisionOutcome(str, PyEnum):
    """판정 결과."""
    ACCEPTED = "accepted"    # 수집됨 (Guideline 생성/버전 추가)
    EXCLUDED = "excluded"    # 제외됨 (stage/reason에 사유)
    PENDING = "pending"      # 판정 보류 (LLM 호출 실패 등 — 재시도 대상)


class CrawlDecision(Base, TimestampMixin):
    """크롤 항목별 판정 기록.

    URL 하나당 한 행 (unique). 같은 URL이 재수집되면 캐시로 동작하되,
    제목이 바뀐 경우(게시물 수정)에는 재판정 후 행을 갱신한다.
    """

    __tablename__ = "crawl_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"), nullable=False)
    config_label: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="판정 당시 크롤 대상 게시판 라벨"
    )
    url: Mapped[str] = mapped_column(
        String(1000), nullable=False, unique=True, index=True,
        comment="게시물 상세 URL (판정 캐시 키)"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    outcome: Mapped[DecisionOutcome] = mapped_column(
        Enum(DecisionOutcome), nullable=False, index=True
    )
    stage: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="판정 단계: it_domain | regex_exclude | regex_strong | "
                "announcement_pass | llm | llm_error | sync_no_llm",
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="판정 근거 요약")
    keyword_matched: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True,
        comment="크롤 시점 keyword_filter 매칭 여부 (soft 필터 신호)"
    )

    def __repr__(self) -> str:
        return f"<CrawlDecision [{self.outcome.value}/{self.stage}] {self.title[:40]}>"
