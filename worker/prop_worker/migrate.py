"""Apply worker/migrations/*.sql to Postgres, in filename order, each once, then verify the lockdown.

Run:  python -m prop_worker.migrate            apply pending migrations, then run the security check
      python -m prop_worker.migrate --status   list applied / pending + the security check, change nothing

Each file runs in its own transaction and is recorded in schema_migrations with a sha256.
If an already-applied file has been edited since, the run stops: write a new migration instead.

Security check (exit 1 if anything FAILs; WARN lines don't change the exit code):
  * every table in `public` has row-level security,
  * every view in `public` is security_invoker,
  * the Supabase API roles (anon, authenticated) hold no privilege on any public table, view,
    sequence or function,
  * future objects won't grant them any: default privileges owned by a role this one can act for
    (itself, or a role it is a member of) are a FAIL, because migration 004 can and should revoke
    them. Default privileges owned by a role it can't act for (on Supabase: supabase_admin, the
    platform's own role) are a WARN naming that role. Only `postgres` can't revoke them, and they
    apply only to objects that role creates; the checks above still cover every object that exists,
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


def _default_grants(conn, role: str) -> list[tuple[str, str, int, bool]]:
    """[(owner role, object type, privilege count, can this role revoke it)] of default privileges
    in `public` that would give `role` something on future objects."""
    kinds = {"r": "tables", "S": "sequences", "f": "functions", "T": "types", "n": "schemas"}
    rows = conn.execute(
        """select o.rolname, d.defaclobjtype::text, count(*),
                  pg_has_role(current_user, d.defaclrole, 'MEMBER')
           from pg_default_acl d
           join pg_namespace n on n.oid = d.defaclnamespace
           join pg_roles o on o.oid = d.defaclrole
           cross join lateral aclexplode(d.defaclacl) a
           where n.nspname = 'public' and a.grantee = (select oid from pg_roles where rolname = %s)
           group by 1, 2, 4 order by 1, 2""", (role,)).fetchall()
    return [(owner, kinds.get(t, t), n, revocable) for owner, t, n, revocable in rows]


def verify(conn) -> tuple[list[str], list[str], list[str]]:
    """(problems, warnings, ok_lines). Read-only except a rolled-back write probe."""
    problems, warnings, ok = [], [], []
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

    user = conn.execute("select current_user").fetchone()[0]
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
        if grants or seqs:
            problems.append(f"role {role} has privileges on existing objects: {len(grants)} table/function "
                            f"grant(s), {len(seqs)} sequence(s) (e.g. {[g[0] for g in grants[:3]]})")
        else:
            ok.append(f"role {role}: no privileges on any existing public table, view, sequence or function")

        defaults = _default_grants(conn, role)
        mine = [d for d in defaults if d[3]]
        theirs = [d for d in defaults if not d[3]]
        if mine:
            owners = sorted({d[0] for d in mine})
            problems.append(f"role {role} would get privileges on future objects created by {owners} "
                            f"({sum(d[2] for d in mine)} default grant(s)); {user} can revoke these: "
                            "re-run migration 004's step 4 for that owner")
        for owner in sorted({d[0] for d in theirs}):
            parts = ", ".join(f"{kind} {n}" for o, kind, n, _ in theirs if o == owner)
            warnings.append(f"role {role}: {sum(d[2] for d in theirs if d[0] == owner)} default grant(s) "
                            f"owned by {owner} ({parts}). {user} isn't a member of {owner}, so it can't "
                            f"revoke them; they apply only to objects {owner} creates in public, and any "
                            "such object would still fail the checks above")
        if not defaults:
            ok.append(f"role {role}: no default privileges on future objects")
    if not present:
        ok.append(f"no {'/'.join(API_ROLES)} roles here (not Supabase): role checks skipped")

    try:
        with conn.transaction(force_rollback=True):
            conn.execute("select count(*) from players").fetchone()
            conn.execute("insert into job_runs (job, status) values ('migrate-selfcheck', 'green')")
        ok.append(f"worker role {user} can read and write (probe rolled back)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"worker role {user} cannot read/write: {type(exc).__name__}: {exc}")
    return problems, warnings, ok


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
        problems, warnings, ok = verify(conn)
    print("security check:")
    for line in ok:
        print(f"  OK    {redact(line)}")
    for line in warnings:
        print(f"  WARN  {redact(line)}")
    for line in problems:
        print(f"  FAIL  {redact(line)}")
    return 1 if problems or changed else 0


if __name__ == "__main__":
    sys.exit(main())
