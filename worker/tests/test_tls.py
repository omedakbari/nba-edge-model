"""TLS is judged on the client side, and a remote connection without it never gets used."""

import psycopg
import pytest

from prop_worker import db, healthcheck as hc

REMOTE = "postgresql://u:p@pooler.example.invalid:5432/postgres"


def test_remote_connection_to_a_server_without_tls_is_refused(pg_admin_url, monkeypatch):
    # The local test server has no TLS. Pretend it is remote: either libpq refuses it outright
    # (sslmode=require over TCP) or, over a unix socket, the client-side check closes it.
    monkeypatch.setattr(db, "is_local", lambda url: False)
    with pytest.raises(db.DatabaseUnavailable) as err:
        db.connect(pg_admin_url)
    assert err.value.__cause__ is None


def test_client_side_check_runs_even_if_sslmode_was_not_forced(pg_admin_url, monkeypatch):
    monkeypatch.setattr(db, "is_local", lambda url: False)
    monkeypatch.setattr(db, "connect_kwargs", lambda url, **kw: {"connect_timeout": 5})
    if "host=/" not in pg_admin_url and "@/" not in pg_admin_url:
        pytest.skip("needs a unix-socket test server (TCP refuses earlier, covered above)")
    with pytest.raises(db.DatabaseUnavailable, match="without TLS"):
        db.connect(pg_admin_url)


def test_local_connection_reports_tls_off(pg_admin_url):
    with db.connect(pg_admin_url) as conn:
        assert db.client_tls(conn) is False


class _Conn:
    """Stands in for a pooled Supabase connection: client TLS as given, pg_stat_ssl as given."""

    def __init__(self, client_tls, server_ssl):
        self.pgconn = type("P", (), {"ssl_in_use": client_tls})()
        self.server_ssl = server_ssl

    def execute(self, sql, *a):
        rows = {"version": [("PostgreSQL 17.4 on aarch64, compiled by gcc",)],
                "pg_stat_ssl": [(self.server_ssl,)], "relrowsecurity": []}
        key = next(k for k in rows if k in sql)
        return type("C", (), {"fetchone": lambda s: rows[key][0] if rows[key] else None,
                              "__iter__": lambda s: iter(rows[key])})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_passes_when_client_has_tls_even_if_pg_stat_ssl_says_off(monkeypatch):
    # Supavisor: this machine -> pooler is TLS; pooler -> Postgres shows ssl = false.
    monkeypatch.setattr(db, "connect", lambda url, **kw: _Conn(True, False))
    status, detail = hc.probe_postgres(REMOTE, require=True)
    assert status == hc.PASS and "TLS on (checked client side" in detail and "pg_stat_ssl=off" in detail


def test_probe_fails_when_client_has_no_tls_even_if_pg_stat_ssl_says_on(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda url, **kw: _Conn(False, True))
    status, detail = hc.probe_postgres(REMOTE, require=True)
    assert status == hc.FAIL and "WITHOUT TLS" in detail


def test_psycopg_exposes_client_tls():
    assert hasattr(psycopg.pq.PGconn, "ssl_in_use")
