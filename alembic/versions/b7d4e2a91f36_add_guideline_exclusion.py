"""add_guideline_exclusion

수집 제외 필드 — 추적 불필요 항목을 사람이 표시하고, 그 사유를
분류로 축적해 필터 규칙 후보의 근거로 삼는다.

Revision ID: b7d4e2a91f36
Revises: e58b21c04a77
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d4e2a91f36'
down_revision: Union[str, Sequence[str], None] = 'e58b21c04a77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


exclusion_category = sa.Enum(
    'PHYSICAL_SECURITY',
    'INTL_AGREEMENT',
    'EDUCATION_PROMO',
    'PLAN_REPORT',
    'NON_IT',
    'DUPLICATE',
    'OTHER',
    name='exclusioncategory',
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    exclusion_category.create(bind, checkfirst=True)

    op.add_column(
        'guidelines',
        sa.Column(
            'excluded_at', sa.DateTime(timezone=True), nullable=True,
            comment='수집 제외 처리 시각 (None이면 추적 대상)',
        ),
    )
    op.add_column(
        'guidelines',
        sa.Column(
            'exclusion_category', exclusion_category, nullable=True,
            comment='제외 사유 분류',
        ),
    )
    op.add_column(
        'guidelines',
        sa.Column(
            'exclusion_note', sa.Text(), nullable=True,
            comment='제외 사유 메모 (category=other 일 때 필수)',
        ),
    )
    op.create_index(
        op.f('ix_guidelines_excluded_at'), 'guidelines', ['excluded_at'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_guidelines_excluded_at'), table_name='guidelines')
    op.drop_column('guidelines', 'exclusion_note')
    op.drop_column('guidelines', 'exclusion_category')
    op.drop_column('guidelines', 'excluded_at')
    exclusion_category.drop(op.get_bind(), checkfirst=True)
