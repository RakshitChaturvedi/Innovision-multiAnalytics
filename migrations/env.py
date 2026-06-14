"""
Alembic environment — Innovision multiAnalytics
------------------------------------------------
Key decisions derived from the actual codebase:

1. writer.py uses SQLAlchemy *async* (AsyncSession + asyncpg driver).
   Alembic's own migration runner is synchronous, so we strip any
   "+asyncpg" driver suffix from DATABASE_URL before handing it to
   engine_from_config.  The app keeps using asyncpg at runtime; only
   the migration process uses psycopg2.

2. No ORM metadata is imported here.  All migrations are written as
   raw SQL via op.execute() so that shared.schemas models never become
   a hard dependency of the migration runner.

3. prepend_sys_path = . in alembic.ini puts the project root on
   sys.path, which is required for `from shared.schemas...` imports
   in migration files should any future migration need them.
"""

import os
import re
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

# Load .env for local dev; no-op when env vars are already injected by Docker
load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Database URL normalisation
# ---------------------------------------------------------------------------

def _sync_url(url: str) -> str:
    """
    Alembic's synchronous runner cannot use asyncpg.
    Convert postgresql+asyncpg://... → postgresql+psycopg2://...
    Also handles bare postgresql://... (leaves it unchanged).
    """
    return re.sub(r"postgresql\+asyncpg", "postgresql+psycopg2", url)


raw_url = os.environ.get("DATABASE_URL")
if not raw_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Copy .env.example to .env and fill in your credentials."
    )

config.set_main_option("sqlalchemy.url", _sync_url(raw_url).replace("%", "%%"))

# ---------------------------------------------------------------------------
# No ORM metadata — all migrations use raw SQL
# ---------------------------------------------------------------------------

target_metadata = None

# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """
    Offline mode: emit SQL to stdout without a live DB connection.
    Useful for generating a migration script to review before applying.
    Run with: alembic -c migrations/alembic.ini upgrade head --sql
    """
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
    """
    Online mode: connect to the live DB and apply migrations.
    NullPool is intentional — Alembic is a short-lived CLI process,
    not an application server; connection pooling adds no value here.
    """
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
