"""
가이드라인 + 법적 근거 + 갭 분석 모델.

2계층 구조:
  legal_bases (고시/훈령) → mandates (위임 항목) → guidelines (실제 발행물)
                                                    └→ guideline_versions (버전 이력)
  gap_analysis: 위임은 있으나 가이드라인 미발행/미갱신 항목
"""

from datetime import date, datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


# ── Enums ────────────────────────────────────────────────


class LegalBasisType(str, PyEnum):
    """법적 근거 유형."""
    GOSI = "gosi"            # 고시
    HUNRYEONG = "hunryeong"  # 훈령
    YEGYU = "yegyu"          # 예규
    GOJUNG = "gojung"        # 고정 (행정규칙 기타)


class GuidelineCategory(str, PyEnum):
    """가이드라인 분야."""
    INFO_SECURITY = "info_security"       # 정보보안
    PRIVACY = "privacy"                   # 개인정보
    SOFTWARE = "software"                 # 소프트웨어
    DATA = "data"                         # 데이터
    CLOUD = "cloud"                       # 클라우드
    AI = "ai"                             # 인공지능
    E_GOV = "e_gov"                       # 전자정부
    FINANCE = "finance"                   # 금융보안
    OTHER = "other"


class GapStatus(str, PyEnum):
    """갭 분석 상태."""
    MISSING = "missing"                   # 가이드라인 미발행
    OUTDATED = "outdated"                 # 근거 개정 후 가이드라인 미갱신
    RESOLVED = "resolved"                 # 해소됨


# ── LegalBasis (고시/훈령) ───────────────────────────────


class LegalBasis(Base, TimestampMixin):
    """법적 근거 — 고시/훈령/예규.

    법제처 행정규칙 또는 kordoc MCP로 수집.
    예: '개인정보의 안전성 확보조치 기준' (개인정보보호위원회 고시)
    """

    __tablename__ = "legal_bases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"), nullable=False)
    basis_type: Mapped[LegalBasisType] = mapped_column(Enum(LegalBasisType), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="고시/훈령 제목")
    law_api_id: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="법제처 행정규칙 ID")
    promulgation_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="공포일")
    enforcement_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="시행일")
    parent_law_name: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="모법명 (예: 개인정보 보호법)")
    category: Mapped[GuidelineCategory] = mapped_column(
        Enum(GuidelineCategory), default=GuidelineCategory.OTHER
    )
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Relations
    mandates: Mapped[list["Mandate"]] = relationship(back_populates="legal_basis", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<LegalBasis [{self.basis_type.value}] {self.title[:30]}>"


# ── Mandate (위임 항목) ──────────────────────────────────


class Mandate(Base, TimestampMixin):
    """고시/훈령이 위임하는 구체적 가이드라인 항목.

    예: '개인정보의 안전성 확보조치 기준' 제7조 →
        "개인정보처리자는 ... 기술적·관리적 보호조치 기준에 따라 ... 세부 지침을 마련하여야 한다"
    """

    __tablename__ = "mandates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    legal_basis_id: Mapped[int] = mapped_column(ForeignKey("legal_bases.id"), nullable=False)
    article_ref: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="근거 조항 (예: 제7조제1항)")
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="위임 내용 요약")
    expected_guideline_title: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="예상되는 가이드라인 제목 (매칭용)"
    )

    # Relations
    legal_basis: Mapped["LegalBasis"] = relationship(back_populates="mandates")
    guidelines: Mapped[list["Guideline"]] = relationship(back_populates="mandate")

    def __repr__(self) -> str:
        return f"<Mandate {self.article_ref}: {self.description[:30]}>"


# ── Guideline (실제 발행된 가이드라인) ────────────────────


class Guideline(Base, TimestampMixin):
    """기관이 실제 발행한 가이드라인/안내서/매뉴얼."""

    __tablename__ = "guidelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"), nullable=False)
    mandate_id: Mapped[int | None] = mapped_column(ForeignKey("mandates.id"), nullable=True, comment="매칭된 위임 항목 (없으면 자발적 발행)")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[GuidelineCategory] = mapped_column(
        Enum(GuidelineCategory), default=GuidelineCategory.OTHER
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="가이드라인 개요")
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="원문 게시 URL")
    pdf_url: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="PDF 다운로드 URL")

    # Relations
    mandate: Mapped["Mandate | None"] = relationship(back_populates="guidelines")
    versions: Mapped[list["GuidelineVersion"]] = relationship(
        back_populates="guideline", cascade="all, delete-orphan", order_by="GuidelineVersion.published_date.desc()"
    )

    @property
    def latest_version(self) -> "GuidelineVersion | None":
        return self.versions[0] if self.versions else None

    def __repr__(self) -> str:
        return f"<Guideline {self.title[:40]}>"


# ── GuidelineVersion (버전 이력) ─────────────────────────


class GuidelineVersion(Base):
    """가이드라인 버전별 이력.

    같은 가이드라인이 개정되면 새 버전 레코드 추가.
    """

    __tablename__ = "guideline_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guideline_id: Mapped[int] = mapped_column(ForeignKey("guidelines.id"), nullable=False)
    version_label: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="버전 표기 (예: v2.0, 2026년판)")
    published_date: Mapped[date] = mapped_column(Date, nullable=False, comment="발행일")
    pdf_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="변경사항 요약 (LLM 생성)")
    significance: Mapped[str | None] = mapped_column(Text, nullable=True, comment="변경 의의 (LLM 생성)")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relations
    guideline: Mapped["Guideline"] = relationship(back_populates="versions")

    def __repr__(self) -> str:
        return f"<GuidelineVersion {self.guideline_id} {self.published_date}>"


# ── GapAnalysis ──────────────────────────────────────────


class GapAnalysis(Base, TimestampMixin):
    """갭 분석 — 법적 근거(위임)는 있지만 가이드라인이 없거나 오래된 항목."""

    __tablename__ = "gap_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[int] = mapped_column(ForeignKey("mandates.id"), nullable=False)
    guideline_id: Mapped[int | None] = mapped_column(ForeignKey("guidelines.id"), nullable=True)
    status: Mapped[GapStatus] = mapped_column(Enum(GapStatus), nullable=False)
    basis_last_amended: Mapped[date | None] = mapped_column(Date, nullable=True, comment="근거 최종 개정일")
    guideline_last_updated: Mapped[date | None] = mapped_column(Date, nullable=True, comment="가이드라인 최종 갱신일")
    days_gap: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="개정일~가이드라인 갱신일 차이(일)")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<GapAnalysis mandate={self.mandate_id} status={self.status.value}>"
