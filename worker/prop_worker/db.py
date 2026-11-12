"""Postgres connection helper. One place that knows how to reach Supabase.

Security:
  * Remote connections always use TLS: sslmode=require (a stricter verify-ca / verify-full in the
    URL is kept), so libpq refuses a server that won't encrypt. After connecting, the CLIENT side is
    checked too (libpq's PQsslInUse via conn.pgconn.ssl_in_use); a remote connection without TLS is
    closed and raises. Only local test servers (unix socket / localhost) may skip TLS.
  * Don't judge TLS from pg_stat_ssl on Supabase: through the Supavisor pooler it describes the
    pooler -> Postgres hop inside Supabase, not this machine -> pooler, and can say ssl = false.
  * A failed connection raises DatabaseUnavailable with the error text redacted, and suppresses the
    original exception chain, so no traceback, log line or job_runs row can carry the URL,
    password, host or user.
"""

from __future__ import annotations

import psycopg
from psycopg.conninfo import conninfo_to_dict

from . import config
from .masking import redact

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
STRICT_SSL = {"verify-ca", "verify-full"}


class NoDatabase(RuntimeError):
    pass


class DatabaseUnavailable(RuntimeError):
    pass


def is_local(url: str) -> bool:
    try:
        host = conninfo_to_dict(url).get("host") or ""
    except psycopg.ProgrammingError:
        return False
    return all(h.startswith("/") or h in LOCAL_HOSTS for h in host.split(","))


def connect_kwargs(url: str, **kw) -> dict:
    kw.setdefault("connect_timeout", 15)
    # Supabase's pooler can't hold server-side prepared statements across clients.
    kw.setdefault("prepare_threshold", None)
    if not is_local(url):
        requested = conninfo_to_dict(url).get("sslmode")
        if requested not in STRICT_SSL:
            kw["sslmode"] = "require"
    return kw


def client_tls(conn) -> bool:
    """True if this client's own socket to the server is encrypted (libpq PQsslInUse)."""
    return bool(conn.pgconn.ssl_in_use)


def connect(db_url: str | None = None, **kw) -> psycopg.Connection:
    url = db_url if db_url is not None else config.SUPABASE_DB_URL
    if not url:
        raise NoDatabase("SUPABASE_DB_URL is not set (see .env.example)")
    try:
        conn = psycopg.connect(url, **connect_kwargs(url, **kw))
    except psycopg.Error as exc:
        raise DatabaseUnavailable(f"could not connect to Postgres: {redact(str(exc))}") from None
    if not is_local(url) and not client_tls(conn):
        conn.close()
        raise DatabaseUnavailable("refusing a remote Postgres connection without TLS")
    return conn
