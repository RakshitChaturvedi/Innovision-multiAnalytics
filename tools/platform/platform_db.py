"""Create this use case's database on the PLATFORM Postgres and migrate it.

    python -m tools.platform.platform_db        (scripts\\platform_db.ps1)

1. DATABASE_URL must name database innovision_analytics (never innovision_platform).
2. CREATE DATABASE innovision_analytics if missing (via the maintenance db "postgres").
3. CREATE EXTENSION IF NOT EXISTS vector (clear FIX if not installed / not permitted).
4. alembic upgrade head, then print alembic heads and current.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys

from sqlalchemy.engine import make_url

from tools.platform.common import ROOT, Reporter, load_env, mask_text, mask_url

EXPECTED_DB = "innovision_analytics"
FORBIDDEN_DB = "innovision_platform"


def _asyncpg_kwargs(url, database: str) -> dict:
    return {"host": url.host or "localhost", "port": url.port or 5432, "user": url.username,
            "password": url.password, "database": database, "timeout": 10}


async def _ensure_database(url, r: Reporter) -> bool:
    import asyncpg

    try:
        conn = await asyncpg.connect(**_asyncpg_kwargs(url, "postgres"))
    except Exception as exc:
        r.fail("postgres", f"cannot connect to {url.host}:{url.port or 5432} as {url.username}: "
               f"{mask_text(str(exc))}",
               "start the platform stack and check the user/password in DATABASE_URL "
               "(the platform Postgres credentials)")
        return False
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if exists:
            r.ok("database", f"{url.database} exists")
            return True
        try:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
        except asyncpg.InsufficientPrivilegeError:
            r.fail("database", f"{url.username} may not create databases",
                   f"as the Postgres superuser run: CREATE DATABASE {url.database} OWNER {url.username};")
            return False
        r.ok("database", f"{url.database} created")
        return True
    finally:
        await conn.close()


async def _ensure_vector(url, r: Reporter) -> bool:
    import asyncpg

    conn = await asyncpg.connect(**_asyncpg_kwargs(url, url.database))
    try:
        available = await conn.fetchval(
            "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
        if available is None:
            r.fail("pgvector", "the vector extension is not installed on this Postgres server",
                   "the platform Postgres image must include pgvector (e.g. pgvector/pgvector:pg16); "
                   "ask the platform team or switch the image, then re-run")
            return False
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except asyncpg.InsufficientPrivilegeError:
            r.fail("pgvector", f"{url.username} may not create the vector extension",
                   f"as the Postgres superuser, connected to {url.database}, run: "
                   "CREATE EXTENSION IF NOT EXISTS vector;")
            return False
        version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        r.ok("pgvector", f"vector {version} enabled in {url.database}")
        return True
    finally:
        await conn.close()


def _alembic(args: list[str], database_url: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", *args],
                       cwd=ROOT, env={**os.environ, "DATABASE_URL": database_url},
                       capture_output=True, text=True)
    return p.returncode, mask_text((p.stdout or "") + (p.stderr or ""))


def setup(database_url: str, r: Reporter, expected_db: str = EXPECTED_DB) -> None:
    url = make_url(database_url)
    if url.database == FORBIDDEN_DB or url.database != expected_db:
        r.fail("DATABASE_URL", f"names database {url.database!r}; this repo only uses {expected_db!r}",
               f"set DATABASE_URL=...@<host>:5432/{expected_db} in .env "
               f"(never {FORBIDDEN_DB}, that is the platform's)")
        return
    r.ok("DATABASE_URL", mask_url(database_url))
    if not asyncio.run(_ensure_database(url, r)):
        return
    if not asyncio.run(_ensure_vector(url, r)):
        return
    code, out = _alembic(["upgrade", "head"], database_url)
    if code != 0:
        r.fail("migrate", "alembic upgrade head failed: " + " | ".join(out.strip().splitlines()[-3:]),
               "fix the error above; if a revision is missing, git pull this repo")
        return
    r.ok("migrate", "alembic upgrade head")
    _, heads = _alembic(["heads"], database_url)
    _, current = _alembic(["current"], database_url)
    heads_s = " ".join(ln.strip() for ln in heads.splitlines() if "(head)" in ln)
    current_s = " ".join(ln.strip() for ln in current.splitlines() if ln.strip() and "INFO" not in ln)
    if heads_s and heads_s.split()[0] in current_s:
        r.ok("alembic", f"heads: {heads_s}; current: {current_s}")
    else:
        r.fail("alembic", f"heads: {heads_s or '?'}; current: {current_s or '?'}",
               "run scripts\\platform_db.ps1 again and read the migrate error")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    env = load_env()
    r = Reporter()
    if not env.get("DATABASE_URL"):
        r.fail("DATABASE_URL", "not set", "copy .env.platform.example to .env and fill in DATABASE_URL")
    else:
        setup(env["DATABASE_URL"], r)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())
