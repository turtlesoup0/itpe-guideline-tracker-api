"""
Alembic 환경 설정.

앱의 SQLAlchemy 모델 메타데이터를 참조하여 autogenerate를 지원합니다.
DB URL은 app.config에서 가져옵니다.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from app.config import get_settings
from app.models.base import Base

# 모든 모델을 import하여 Base.metadata에 등록
import app.models.agency  # noqa: F401
import app.models.guideline  # noqa: F401

config = context.config

# 로깅 설정
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate용 메타데이터
target_metadata = Base.metadata

# DB URL을 앱 설정에서 주입 (asyncpg → 동기 드라이버로 변환)
settings = get_settings()
sync_url = settings.database_url.replace("+asyncpg", "")
config.set_main_option("sqlalchemy.url", sync_url)


def run_migrations_offline() -> None:
    """오프라인 모드 마이그레이션."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """온라인 모드 마이그레이션."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
