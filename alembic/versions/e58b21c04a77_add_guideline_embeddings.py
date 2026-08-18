"""add_guideline_embeddings

Revision ID: e58b21c04a77
Revises: c31a7de90f12
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e58b21c04a77'
down_revision: Union[str, Sequence[str], None] = 'c31a7de90f12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'guideline_embeddings',
        sa.Column('guideline_id', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False, comment='임베딩 모델명 (모델 교체 시 재계산 판단용)'),
        sa.Column('dim', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False, comment='임베딩 당시 제목 (제목 변경 감지용)'),
        sa.Column('vector', sa.LargeBinary(), nullable=False, comment='float32 packed, L2-normalized'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['guideline_id'], ['guidelines.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('guideline_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('guideline_embeddings')
