"""fsc_date_selector

금융위원회 게시판(보도자료·고시·훈령) 크롤 설정에 date_selector='.day' 를 넣는다.

금융위 목록은 ul/li 구조라 date_selector 없이는 td 기반 날짜 탐색이 실패하고,
published_date 가 비어 크롤한 날짜(date.today())로 저장됐다. 게시물이 2페이지로
밀려 URL 매칭까지 실패하면 같은 보도자료에 매일 새 버전이 붙었다.

registry.py 는 최초 시드에만 쓰이므로 이미 만들어진 crawl_configs 행은 여기서 고친다.

Revision ID: f3a9c1d07e52
Revises: d1e8b3c62a47
Create Date: 2026-11-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d07e52'
down_revision: Union[str, Sequence[str], None] = 'd1e8b3c62a47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FSC_BBS_URLS = (
    "https://www.fsc.go.kr/no010101",
    "https://www.fsc.go.kr/po040200",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.get_bind().execute(
        sa.text(
            "UPDATE crawl_configs SET date_selector = '.day' "
            "WHERE url IN :urls AND date_selector IS NULL"
        ).bindparams(sa.bindparam("urls", expanding=True)),
        {"urls": list(_FSC_BBS_URLS)},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.get_bind().execute(
        sa.text(
            "UPDATE crawl_configs SET date_selector = NULL "
            "WHERE url IN :urls AND date_selector = '.day'"
        ).bindparams(sa.bindparam("urls", expanding=True)),
        {"urls": list(_FSC_BBS_URLS)},
    )
