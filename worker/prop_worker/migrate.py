"""Apply worker/migrations/*.sql to Postgres, in filename order, each once, then verify the lockdown.

Run:  python -m prop_worker.migrate            apply pending migrations, then run the security check
      python -m prop_worker.migrate --status   list applied / pending + the security check, change nothing

Each file runs in its own transaction and is recorded in schema_migrations with a sha256.
If an already-applied file has been edited since, the run stops: write a new migration instead.

Security check (exit 1 if anything fails):
  * every table in `public` has row-level security,
  * every view in `public` is security_invoker,
  * the Supabase API roles (anon, authenticated) hold no privilege on any public table, view,
    sequence or function, and future objects won't grant them any,
  * the worker's own role can still read and write (a job_runs insert, rolled back).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from .db import connect
from .masking import redact

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
API_ROLES = ("anon", "authenticated")

_BOOKKEEPING = """
create table if not exists schema_migrations (
    version     text primary key,
    sha256      text not null,
    applied_at  timestamptz not null default now()
);
alter table schema_migrations enable row level security;
"""


def discover(directory: Path = MIGRATIONS) -> list[tuple[str, str, str]]:
    """[(version, sha256, sql)] sorted by filename."""
    out = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text()
        out.append((path.stem, hashlib.sha256(sql.encode()).hexdigest(), sql))
    return out


def plan(conn, migrations) -> tuple[list, list, list]:
    """(applied, pending, changed) — changed = applied files whose contents no longer match."""
    conn.execute(_BOOKKEEPING)
    conn.commit()
    done = dict(conn.execute("select version, sha256 from schema_migrations").fetchall())
    applied, pending, changed = [], [], []
    for version, sha, sql in migrations:
        if version not in done:
            pending.append((version, sha, sql))
        elif done[version] != sha:
            changed.append(version)
        else:
            applied.append(version)
    return applied, pending, changed


def apply(conn, migrations) -> list[str]:
    applied, pending, changed = plan(conn, migrations)
    if changed:
        raise SystemExit(f"already-applied migration(s) edited since: {changed}. "
                         "Restore them and add a new numbered migration instead.")
    ran = []
    for version, sha, sql in pending:
        with conn.transaction():
            conn.execute(sql)
            conn.execute("insert into schema_migrations (version, sha256) values (%s, %s)", (version, sha))
        ran.append(version)
    return ran


def verify(conn) -> tuple[list[str], list[str]]:
    """(problems, ok_lines). Read-only except a rolled-back write probe."""
    problems, ok = [], []
    no_rls = [r[0] for r in conn.execute(
        """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'public' and c.relkind in ('r', 'p') and not c.relrowsecurity order by 1""")]
    if no_rls:
        problems.append(f"tables without row-level security: {no_rls}")
    else:
        ok.append("row-level security on every public table")
    definer_views = [r[0] for r in conn.execute(
        """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'public' and c.relkind = 'v'
             and not coalesce('security_invoker=true' = any(c.reloptions), false) order by 1""")]
    if definer_views:
        problems.append(f"views without security_invoker: {definer_views}")
    else:
        ok.append("every public view is security_invoker")

    present = [r[0] for r in conn.execute("select rolname from pg_roles where rolname = any(%s)", (list(API_ROLES),))]
    for role in present:
        # has_*_privilege answers "can this role do it", whoever granted it (incl. via PUBLIC).
        grants = conn.execute(
            """select c.relname, 'table' from pg_class c join pg_namespace n on n.oid = c.relnamespace
               where n.nspname = 'public' and c.relkind in ('r', 'p', 'v', 'm', 'f')
                 and has_table_privilege(%s, c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
               union all
               select p.proname, 'function' from pg_proc p join pg_namespace n on n.oid = p.pronamespace
               where n.nspname = 'public' and has_function_privilege(%s, p.oid, 'EXECUTE')""",
            (role, role)).fetchall()
        seqs = conn.execute(
            """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
               where n.nspname = 'public' and c.relkind = 'S'
                 and (has_sequence_privilege(%s, c.oid, 'USAGE') or has_sequence_privilege(%s, c.oid, 'SELECT'))""",
            (role, role)).fetchall()
        defaults = conn.execute(
            """select d.defaclobjtype from pg_default_acl d join pg_namespace n on n.oid = d.defaclnamespace
               cross join lateral aclexplode(d.defaclacl) a
               where n.nspname = 'public' and a.grantee = (select oid from pg_roles where rolname = %s)""",
            (role,)).fetchall()
        if grants or seqs or defaults:
            problems.append(f"role {role} still has privileges: {len(grants)} table/function grant(s), "
                            f"{len(seqs)} sequence(s), {len(defaults)} default grant(s) "
                            f"(e.g. {[g[0] for g in grants[:3]]})")
        else:
            ok.append(f"role {role}: no privileges on public tables, views, sequences, functions or future objects")
    if not present:
        ok.append(f"no {'/'.join(API_ROLES)} roles here (not Supabase): role checks skipped")

    user = conn.execute("select current_user").fetchone()[0]
    try:
        with conn.transaction(force_rollback=True):
            conn.execute("select count(*) from players").fetchone()
            conn.execute("insert into job_runs (job, status) values ('migrate-selfcheck', 'green')")
        ok.append(f"worker role {user} can read and write (probe rolled back)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"worker role {user} cannot read/write: {type(exc).__name__}: {exc}")
    return problems, ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="show applied/pending + security check, change nothing")
    args = ap.parse_args(argv)

    migrations = discover()
    with connect() as conn:
        if args.status:
            applied, pending, changed = plan(conn, migrations)
            for v in applied:
                print(f"applied  {v}")
            for v, _, _ in pending:
                print(f"PENDING  {v}")
            for v in changed:
                print(f"CHANGED  {v}  (edited after it was applied)")
        else:
            ran = apply(conn, migrations)
            tables = conn.execute(
                "select count(*) from information_schema.tables where table_schema = 'public'").fetchone()[0]
            print(f"applied {len(ran)} migration(s): {', '.join(ran) or 'none pending'}; {tables} public tables/views")
            changed = []
        problems, ok = verify(conn)
    print("security check:")
    for line in ok:
        print(f"  OK    {redact(line)}")
    for line in problems:
        print(f"  FAIL  {redact(line)}")
    return 1 if problems or changed else 0


if __name__ == "__main__":
    sys.exit(main())
