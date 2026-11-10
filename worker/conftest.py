"""Shared pytest setup.

Database tests get a fresh, migrated database per test from, in order:
  1. TEST_DATABASE_URL (a disposable Postgres; CI uses a service container), or
  2. a throwaway local server from the `pgserver` package, if it is installed, or
  3. they are skipped.
They never touch SUPABASE_DB_URL.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

FIXTURES = Path(__file__).parent / "tests" / "fixtures"


@pytest.fixture(scope="session")
def pg_admin_url():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("no TEST_DATABASE_URL and pgserver not installed: database tests skipped")
    server = pgserver.get_server(tempfile.mkdtemp(prefix="prop_worker_pg_"), cleanup_mode="stop")
    yield server.get_uri()


@pytest.fixture
def pg(pg_admin_url):
    """A connection to a brand-new database with every migration applied."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from prop_worker import migrate

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    url = make_conninfo(pg_admin_url, dbname=name)
    conn = psycopg.connect(url)
    migrate.apply(conn, migrate.discover())
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')
