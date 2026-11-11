"""Fails if any job output (stdout, step summary, ntfy body, artifact, job_runs, exceptions) contains
a secret: the fake ones planted here, values derived from them, and, on a machine with a .env file,
every real .env value. Failure messages name the leaked KEY only, never the value."""

import json
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from prop_worker import config, db, healthcheck as hc, masking, runner

FAKE = {
    "SUPABASE_DB_URL": "postgresql://postgres.abcdefghijklmnopqrst:Sup3r-S3cret%21Pw@aws-0-us-east-1.pooler.supabase.invalid:5432/postgres",
    "PROPLINE_API_KEY": "pl_live_FAKEKEY_0123456789abcdef",
    "NTFY_TOPIC": "nba-alerts-k3j4h5g6f7d8s9a0",
}
DERIVED = {"db user": "postgres.abcdefghijklmnopqrst", "db password (url-encoded)": "Sup3r-S3cret%21Pw",
           "db password": "Sup3r-S3cret!Pw", "db host": "aws-0-us-east-1.pooler.supabase.invalid",
           "project ref": "abcdefghijklmnopqrst"}
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _real_env_values() -> dict:
    if not ENV_FILE.exists():
        return {}
    from dotenv import dotenv_values
    out = {}
    for k, v in dotenv_values(ENV_FILE).items():
        if v and len(v) >= 8 and k in masking.SECRET_ENV:
            out[f".env {k}"] = v
            for part, pv in masking.db_url_parts(v).items() if "://" in v else []:
                if part in ("user", "password", "password_decoded", "host", "project_ref") and pv and len(pv) >= 6 \
                        and pv.lower() not in masking.GENERIC:
                    out[f".env {k} {part}"] = pv
    return out


SECRETS = {**FAKE, **DERIVED, **_real_env_values()}


def assert_clean(text, where):
    leaked = sorted(name for name, value in SECRETS.items() if value and value in str(text))
    assert not leaked, f"{where} leaked: {leaked}"          # names only


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
        monkeypatch.setattr(config, k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def test_redact_removes_every_value_of_the_current_environment_and_its_parts():
    text = " | ".join([*FAKE.values(), *DERIVED.values()])      # redact() knows the env it runs in
    leaked = [n for n, v in {**FAKE, **DERIVED}.items() if v in masking.redact(text)]
    assert not leaked, f"redact() leaked: {leaked}"


def test_failed_connection_never_shows_url_password_host_or_user():
    with pytest.raises(db.DatabaseUnavailable) as err:
        db.connect(FAKE["SUPABASE_DB_URL"], connect_timeout=2)
    assert_clean(str(err.value), "exception message")
    assert_clean("".join(traceback.format_exception(err.value)), "traceback")
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_remote_connections_force_tls_and_local_ones_may_skip_it():
    assert db.connect_kwargs(FAKE["SUPABASE_DB_URL"])["sslmode"] == "require"
    assert db.connect_kwargs(FAKE["SUPABASE_DB_URL"] + "?sslmode=disable")["sslmode"] == "require"
    assert "sslmode" not in db.connect_kwargs(FAKE["SUPABASE_DB_URL"] + "?sslmode=verify-full")  # stricter kept
    assert "sslmode" not in db.connect_kwargs("postgresql://postgres:@/postgres?host=/tmp/pg")
    assert "sslmode" not in db.connect_kwargs("postgresql://postgres:postgres@localhost:5432/postgres")


def test_healthcheck_table_and_artifact_are_clean(tmp_path, capsys, monkeypatch):
    from test_healthcheck import NOW, FakeSession, healthy_routes
    results = hc.run(FakeSession(healthy_routes()), NOW, db_url=FAKE["SUPABASE_DB_URL"], require_db=True,
                     propline_key=FAKE["PROPLINE_API_KEY"])
    pg = {p.name: p for p in results}["postgres"]
    assert pg.status == hc.FAIL and "could not connect" in pg.detail
    monkeypatch.setattr(hc, "run", lambda **kw: results)
    out_json = tmp_path / "healthcheck.json"
    hc.main(["--json", str(out_json)])
    assert_clean(capsys.readouterr().out, "healthcheck stdout")
    assert_clean(out_json.read_text(), "healthcheck artifact")
    assert_clean(json.dumps([asdict(p) for p in results]), "healthcheck results")


def test_runner_summary_alert_and_job_runs_are_clean(pg, tmp_path, capsys, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def leaky_error(ctx):
        raise RuntimeError(f"could not reach {FAKE['SUPABASE_DB_URL']} with key {FAKE['PROPLINE_API_KEY']} "
                           f"(password={DERIVED['db password']}) topic {FAKE['NTFY_TOPIC']}")

    def leaky_red(ctx):
        return runner.JobResult(1, 1, red_reasons=[f"host {DERIVED['db host']} user {DERIVED['db user']}"],
                                warnings=[f"ref {DERIVED['project ref']}"])

    sent = []
    code, _ = runner.run_all([("leaky_error", leaky_error), ("leaky_red", leaky_red)], conn=pg,
                             now=datetime(2026, 11, 11, 18, 0, tzinfo=config.ET),
                             notifier=lambda t, b: sent.append((t, b)) or "sent")
    assert code == 1 and sent
    assert_clean(capsys.readouterr().out, "stdout")
    assert_clean(summary.read_text(), "step summary")
    assert_clean(sent, "ntfy alert")
    rows = pg.execute("select error, red_reasons, warnings, meta::text from job_runs").fetchall()
    assert_clean(rows, "job_runs rows")


def test_ntfy_failure_message_hides_the_topic():
    class Broken:
        def post(self, url, **kw):
            raise ConnectionError(f"cannot reach {url}")
    msg = runner.send_ntfy("t", "b", session=Broken(), topic=FAKE["NTFY_TOPIC"])
    assert msg.startswith("ntfy FAILED") and FAKE["NTFY_TOPIC"] not in msg


def test_masking_cli_refuses_outside_actions_and_prints_only_mask_commands_inside(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert masking.main() == 2
    out = capsys.readouterr()
    assert_clean(out.out + out.err, "masking outside Actions")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert masking.main() == 0
    lines = capsys.readouterr().out.splitlines()
    commands = [line for line in lines if line.startswith("::add-mask::")]
    assert_clean("\n".join(line for line in lines if not line.startswith("::add-mask::")), "masking chatter")
    masked = {line.removeprefix("::add-mask::") for line in commands}
    assert set(DERIVED.values()) | set(FAKE.values()) <= masked
    assert "postgres" not in masked and "5432" not in masked          # generic words stay readable
