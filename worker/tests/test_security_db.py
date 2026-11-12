"""Migration 004: with Supabase's default grants recreated, the API roles end up with nothing."""

import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from prop_worker import migrate

ROLES = ("anon", "authenticated")


@pytest.fixture
def supabase_like(pg_admin_url):
    """A fresh database whose creator grants everything to anon/authenticated, like Supabase does."""
    name = f"sec_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        for role in ROLES:
            if not admin.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                admin.execute(f"create role {role} nologin")
        admin.execute(f'create database "{name}"')
    conn = psycopg.connect(make_conninfo(pg_admin_url, dbname=name))
    for kind in ("tables", "sequences", "functions"):
        conn.execute(f"alter default privileges in schema public grant all on {kind} to anon, authenticated")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')


def _apply(conn, upto=None):
    ms = [m for m in migrate.discover() if upto is None or m[0] < upto]
    return migrate.apply(conn, ms)


def test_before_004_the_check_catches_the_exposure(supabase_like):
    _apply(supabase_like, upto="004")
    assert supabase_like.execute("select has_table_privilege('anon', 'players', 'SELECT')").fetchone()[0]
    problems, warnings, _ = migrate.verify(supabase_like)
    for role in ROLES:
        assert any(f"role {role} has privileges on existing objects" in p for p in problems)
        assert any(f"role {role} would get privileges on future objects" in p for p in problems)
    assert warnings == []


def test_after_004_api_roles_have_nothing(supabase_like):
    _apply(supabase_like)
    c = supabase_like
    for role in ROLES:
        for table in ("players", "game_logs", "book_quotes", "quote_history", "job_runs", "schema_migrations"):
            assert not c.execute("select has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE')",
                                 (role, table)).fetchone()[0], (role, table)
        assert not c.execute("select has_table_privilege(%s, 'player_xref_wide', 'SELECT')", (role,)).fetchone()[0]
        assert not c.execute("select has_sequence_privilege(%s, 'players_player_id_seq', 'USAGE')",
                             (role,)).fetchone()[0]
        assert not c.execute("select has_function_privilege(%s, 'book_quotes_change_only()', 'EXECUTE')",
                             (role,)).fetchone()[0]
    problems, warnings, ok = migrate.verify(c)
    assert problems == [] and warnings == [], (problems, warnings)
    assert any("worker role" in line and "can read and write" in line for line in ok)


def test_future_tables_start_locked(supabase_like):
    _apply(supabase_like)
    supabase_like.execute("create table public.added_later (id int)")
    assert not supabase_like.execute("select has_table_privilege('anon', 'added_later', 'SELECT')").fetchone()[0]
    problems, _, _ = migrate.verify(supabase_like)       # ...but RLS is per table: the check flags it
    assert problems == ["tables without row-level security: ['added_later']"]


def test_every_table_has_rls_and_every_view_is_security_invoker(supabase_like):
    _apply(supabase_like)
    no_rls = supabase_like.execute(
        """select relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where nspname = 'public' and relkind = 'r' and not relrowsecurity""").fetchall()
    assert no_rls == []
    views = supabase_like.execute(
        """select relname, reloptions from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where nspname = 'public' and relkind = 'v'""").fetchall()
    assert views and all("security_invoker=true" in (opts or []) for _, opts in views)


def test_functions_have_fixed_search_path(supabase_like):
    _apply(supabase_like)
    loose = supabase_like.execute(
        """select proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where nspname = 'public' and not exists (
               select 1 from unnest(coalesce(p.proconfig, '{}')) cfg where cfg like 'search_path=%')""").fetchall()
    assert loose == []


def test_owner_still_works_through_triggers(supabase_like):
    _apply(supabase_like)
    c = supabase_like
    c.execute("""insert into kalshi_markets (ticker, event_ticker, series_ticker, market_kind)
                 values ('T-26NOV10AAABBB-X', 'T-26NOV10AAABBB', 'T', 'player_prop')""")
    for _ in range(2):
        c.execute("insert into kalshi_quotes (ticker, yes_ask) values ('T-26NOV10AAABBB-X', 0.4)")
    assert c.execute("select count(*) from kalshi_quotes").fetchone()[0] == 1      # change-only trigger ran


@pytest.fixture
def platform_owned_defaults(supabase_like, pg_admin_url):
    """Like Supabase: a platform role (supabase_admin there) owns default grants to the API roles,
    and the worker's role is not a member of it, so it can't revoke them."""
    c = supabase_like
    _apply(c)
    dbname = c.info.dbname
    worker = f"w_{uuid.uuid4().hex[:8]}"
    owner = f"platform_{uuid.uuid4().hex[:8]}"
    c.execute(f"create role {owner} nologin")
    c.execute(f"create role {worker} nologin bypassrls")       # stands in for the table owner
    c.execute(f"grant usage, create on schema public to {worker}")
    c.execute(f"grant select, insert on all tables in schema public to {worker}")
    c.execute(f"grant usage on all sequences in schema public to {worker}")
    for kind in ("tables", "sequences", "functions"):
        c.execute(f"alter default privileges for role {owner} in schema public grant all on {kind} to anon, authenticated")
    c.commit()
    try:
        yield c, worker, owner
    finally:
        c.rollback()
        c.execute("reset role")
        for kind in ("tables", "sequences", "functions"):
            c.execute(f"alter default privileges for role {owner} in schema public revoke all on {kind} from anon, authenticated")
        c.execute(f"drop owned by {worker}")
        c.execute(f"drop owned by {owner}")
        c.execute(f"drop role {worker}")
        c.execute(f"drop role {owner}")
        c.commit()


def test_default_grants_owned_by_a_role_we_cant_act_for_are_a_warning_naming_it(platform_owned_defaults):
    c, worker, owner = platform_owned_defaults
    c.execute(f"set role {worker}")
    problems, warnings, ok = migrate.verify(c)
    assert problems == [], problems
    for role in ROLES:
        line = next(w for w in warnings if w.startswith(f"role {role}:"))
        assert f"owned by {owner}" in line and f"{worker} isn't a member of {owner}" in line
        assert "tables" in line and "sequences" in line and "functions" in line
        assert any(f"role {role}: no privileges on any existing" in o for o in ok)


def test_default_grants_we_can_revoke_stay_a_failure(platform_owned_defaults):
    c, worker, owner = platform_owned_defaults
    c.execute(f"grant {owner} to {worker}")            # now the worker CAN act for the owner
    c.execute(f"set role {worker}")
    problems, warnings, _ = migrate.verify(c)
    assert warnings == []
    assert any("would get privileges on future objects" in p and owner in p for p in problems)
    c.execute("reset role")
    c.execute(f"revoke {owner} from {worker}")


def test_migrate_cli_exits_0_with_only_warnings(platform_owned_defaults, monkeypatch, capsys):
    c, worker, owner = platform_owned_defaults
    c.execute(f"alter table schema_migrations owner to {worker}")     # migrate re-runs its bookkeeping DDL
    c.execute(f"set role {worker}")

    class Keep:
        def __enter__(self):
            return c

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(migrate, "connect", lambda: Keep())
    assert migrate.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "  WARN  role anon:" in out and "FAIL" not in out
