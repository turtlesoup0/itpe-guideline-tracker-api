"""제외 규칙 후보 — 주기 분석이 도출한 "이런 제목은 거를 만하다" 제안.

자동으로 필터에 반영하지 않는다. 후보는 사람이 승인해야 효력이 생긴다:
잘못된 규칙 하나가 정상 문서를 조용히 지우는 쪽이, 잡음 몇 건이 남는 쪽보다
훨씬 나쁘기 때문이다.

각 후보는 근거를 함께 들고 다닌다:
  support_count         — 이 패턴에 걸리는 '제외된' 항목 수 (규칙의 근거)
  false_positive_count  — 이 패턴에 걸리는 '살아있는' 항목 수 (규칙의 위험)
  sample_titles         — 사람이 눈으로 확인할 실제 제목
false_positive_count 이 0이 아니면 승격 후보로 올리지 않는다.
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.guideline import ExclusionCategory


class RuleCandidateStatus(str, PyEnum):
    """후보 검토 상태."""
    PENDING = "pending"      # 검토 대기
    APPROVED = "approved"    # 승인 — 필터에 반영
    REJECTED = "rejected"    # 반려 — 다시 제안하지 않음


class ExclusionRuleCandidate(Base, TimestampMixin):
    """제목 패턴 기반 제외 규칙 후보."""

    __tablename__ = "exclusion_rule_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True, index=True,
        comment="제목에 포함되면 제외 후보가 되는 문자열",
    )
    category: Mapped[ExclusionCategory | None] = mapped_column(
        Enum(ExclusionCategory), nullable=True,
        comment="근거가 된 제외 항목들의 최빈 사유 분류",
    )
    support_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="이 패턴에 걸리는 제외 항목 수 (근거)",
    )
    false_positive_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="이 패턴에 걸리는 활성 항목 수 (0이 아니면 승격 금지)",
    )
    sample_titles: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="근거 제목 예시 (줄바꿈 구분)",
    )
    status: Mapped[RuleCandidateStatus] = mapped_column(
        Enum(RuleCandidateStatus), nullable=False,
        default=RuleCandidateStatus.PENDING, index=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="사람이 승인/반려한 시각",
    )

    def __repr__(self) -> str:
        return (
            f"<ExclusionRuleCandidate {self.pattern!r} "
            f"support={self.support_count} fp={self.false_positive_count}>"
        )
