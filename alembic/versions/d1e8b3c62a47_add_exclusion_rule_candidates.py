"""add_exclusion_rule_candidates

주기 분석이 도출한 제외 규칙 후보. 자동 반영은 하지 않고 사람이 승인한
후보만 필터에 쓴다 — 근거(support)와 위험(false_positive)을 함께 저장한다.

Revision ID: d1e8b3c62a47
Revises: c9a2f5e731b8
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd1e8b3c62a47'
down_revision: Union[str, Sequence[str], None] = 'c9a2f5e731b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


status_enum = sa.Enum('PENDING', 'APPROVED', 'REJECTED', name='rulecandidatestatus')
# create_table 이 타입을 다시 만들지 않도록 참조 전용 핸들을 따로 둔다
status_ref = postgresql.ENUM(name='rulecandidatestatus', create_type=False)


def upgrade() -> None:
    """Upgrade schema."""
    status_enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        'exclusion_rule_candidates',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pattern', sa.String(length=200), nullable=False,
                  comment='제목에 포함되면 제외 후보가 되는 문자열'),
        # exclusioncategory 타입은 b7d4e2a91f36 에서 이미 생성됨 — 재생성하지 않는다
        sa.Column('category',
                  postgresql.ENUM(name='exclusioncategory', create_type=False),
                  nullable=True, comment='근거가 된 제외 항목들의 최빈 사유 분류'),
        sa.Column('support_count', sa.Integer(), nullable=False,
                  comment='이 패턴에 걸리는 제외 항목 수 (근거)'),
        sa.Column('false_positive_count', sa.Integer(), nullable=False,
                  comment='이 패턴에 걸리는 활성 항목 수 (0이 아니면 승격 금지)'),
        sa.Column('sample_titles', sa.Text(), nullable=True,
                  comment='근거 제목 예시 (줄바꿈 구분)'),
        sa.Column('status', status_ref, nullable=False),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True,
                  comment='사람이 승인/반려한 시각'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_exclusion_rule_candidates_pattern'),
                    'exclusion_rule_candidates', ['pattern'], unique=True)
    op.create_index(op.f('ix_exclusion_rule_candidates_status'),
                    'exclusion_rule_candidates', ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_exclusion_rule_candidates_status'),
                  table_name='exclusion_rule_candidates')
    op.drop_index(op.f('ix_exclusion_rule_candidates_pattern'),
                  table_name='exclusion_rule_candidates')
    op.drop_table('exclusion_rule_candidates')
    status_enum.drop(op.get_bind(), checkfirst=True)
