"""Apply worker/migrations/*.sql to Postgres, in filename order, each once.

Run:  python -m prop_worker.migrate            apply pending migrations
      python -m prop_worker.migrate --status   list applied / pending, change nothing

Each file runs in its own transaction and is recorded in schema_migrations with a sha256.
If an already-applied file has been edited since, the run stops: write a new migration instead.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from .db import connect

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="show applied/pending and exit")
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
            return 1 if changed else 0
        ran = apply(conn, migrations)
        tables = conn.execute(
            "select count(*) from information_schema.tables where table_schema = 'public'").fetchone()[0]
    print(f"applied {len(ran)} migration(s): {', '.join(ran) or 'none pending'}; {tables} public tables/views")
    return 0


if __name__ == "__main__":
    sys.exit(main())
