"""normalize_decision_urls

crawl_decisions.url 을 정규화 키(세션 토큰 제거)로 통일한다.

기존 행에는 `;jsessionid=…` 가 박힌 URL이 남아 있어, 같은 게시물이 매 크롤
다른 키로 보이며 판정 캐시(제외 포함)를 비껴갔다. 정규화 후 키가 겹치는
행은 가장 최근 판정만 남긴다.

Revision ID: c9a2f5e731b8
Revises: b7d4e2a91f36
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.url_key import normalize_decision_url


# revision identifiers, used by Alembic.
revision: str = 'c9a2f5e731b8'
down_revision: Union[str, Sequence[str], None] = 'b7d4e2a91f36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, url, updated_at FROM crawl_decisions ORDER BY id")
    ).fetchall()

    # 정규화 키 → 유지할 행 (updated_at 최신, 동률이면 id 큰 쪽)
    keep: dict[str, tuple[int, object]] = {}
    drop: list[int] = []
    for row_id, url, updated_at in rows:
        key = normalize_decision_url(url) or url
        current = keep.get(key)
        if current is None:
            keep[key] = (row_id, updated_at)
            continue
        cur_id, cur_updated = current
        newer = (updated_at, row_id) > (cur_updated, cur_id)
        if newer:
            drop.append(cur_id)
            keep[key] = (row_id, updated_at)
        else:
            drop.append(row_id)

    if drop:
        bind.execute(
            sa.text("DELETE FROM crawl_decisions WHERE id = ANY(:ids)"),
            {"ids": drop},
        )

    for key, (row_id, _) in keep.items():
        bind.execute(
            sa.text("UPDATE crawl_decisions SET url = :url WHERE id = :id AND url <> :url"),
            {"url": key[:1000], "id": row_id},
        )


def downgrade() -> None:
    """Downgrade schema.

    정규화로 제거된 세션 토큰과 병합된 행은 복원할 수 없다 (원본 미보존).
    스키마 변경이 없으므로 no-op.
    """
    pass
