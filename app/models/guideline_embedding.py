"""
가이드라인 제목 임베딩 저장 — 의미 기반 정체성 판정(Tier 1)용.

정규식 normalize_title이 못 잡는 제목 변형([현재/과거 안내서] 태그,
새글 배지, 띄어쓰기·조사 차이 등)을 의미 유사도로 매칭하기 위해
제목 임베딩 벡터를 보관한다.

규모(수백~수천 건)상 pgvector 없이 float32 packed bytes로 저장하고
Python에서 브루트포스 코사인 검색한다.
"""

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class GuidelineEmbedding(Base, TimestampMixin):
    """가이드라인 1건당 제목 임베딩 1행."""

    __tablename__ = "guideline_embeddings"

    guideline_id: Mapped[int] = mapped_column(
        ForeignKey("guidelines.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="임베딩 모델명 (모델 교체 시 재계산 판단용)"
    )
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="임베딩 당시 제목 (제목 변경 감지용)"
    )
    vector: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, comment="float32 packed, L2-normalized"
    )

    def __repr__(self) -> str:
        return f"<GuidelineEmbedding g={self.guideline_id} dim={self.dim}>"
