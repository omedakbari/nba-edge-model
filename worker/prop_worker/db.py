"""Postgres connection helper. One place that knows how to reach Supabase."""

from __future__ import annotations

import psycopg

from . import config


class NoDatabase(RuntimeError):
    pass


def connect(db_url: str | None = None, **kw) -> psycopg.Connection:
    url = db_url if db_url is not None else config.SUPABASE_DB_URL
    if not url:
        raise NoDatabase("SUPABASE_DB_URL is not set (see .env.example)")
    kw.setdefault("connect_timeout", 15)
    # Supabase's pooler can't hold server-side prepared statements across clients.
    kw.setdefault("prepare_threshold", None)
    return psycopg.connect(url, **kw)
