"""add_crawl_decisions

Revision ID: c31a7de90f12
Revises: 09147a946c96
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c31a7de90f12'
down_revision: Union[str, Sequence[str], None] = '09147a946c96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'crawl_decisions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('agency_id', sa.Integer(), nullable=False),
        sa.Column('config_label', sa.String(length=200), nullable=True, comment='판정 당시 크롤 대상 게시판 라벨'),
        sa.Column('url', sa.String(length=1000), nullable=False, comment='게시물 상세 URL (판정 캐시 키)'),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('outcome', sa.Enum('ACCEPTED', 'EXCLUDED', 'PENDING', name='decisionoutcome'), nullable=False),
        sa.Column('stage', sa.String(length=50), nullable=True, comment='판정 단계: it_domain | regex_exclude | regex_strong | announcement_pass | llm | llm_error | sync_no_llm'),
        sa.Column('reason', sa.Text(), nullable=True, comment='판정 근거 요약'),
        sa.Column('keyword_matched', sa.Boolean(), nullable=True, comment='크롤 시점 keyword_filter 매칭 여부 (soft 필터 신호)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agency_id'], ['agencies.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_crawl_decisions_url'), 'crawl_decisions', ['url'], unique=True)
    op.create_index(op.f('ix_crawl_decisions_outcome'), 'crawl_decisions', ['outcome'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_crawl_decisions_outcome'), table_name='crawl_decisions')
    op.drop_index(op.f('ix_crawl_decisions_url'), table_name='crawl_decisions')
    op.drop_table('crawl_decisions')
    sa.Enum(name='decisionoutcome').drop(op.get_bind(), checkfirst=True)
