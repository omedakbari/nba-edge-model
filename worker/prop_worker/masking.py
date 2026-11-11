"""Know which values are secret, redact them from any text, and mask them in GitHub Actions logs.

Stdlib only (no psycopg import), so the workflows can run `python3 -m prop_worker.masking` to mask
before any dependency is installed.

Sensitive values = every SECRET_ENV value, plus what can be derived from SUPABASE_DB_URL (user,
password raw and URL-decoded, host, Supabase project ref). Short or generic values ('postgres',
'5432', 'localhost') are not treated as secrets: masking them would only garble logs.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import unquote, urlsplit

SECRET_ENV = ("SUPABASE_DB_URL", "PROPLINE_API_KEY", "NTFY_TOPIC", "ODDS_API_KEY", "TEST_DATABASE_URL")
GENERIC = {"postgres", "postgresql", "localhost", "127.0.0.1", "5432", "6543", "supabase", "pooler"}
MIN_LEN = 6
REDACTED = "***"
_DSN = re.compile(r"postgres(?:ql)?://[^\s\"'<>]+", re.I)
_KV_PASSWORD = re.compile(r"(password\s*=\s*)(\S+)", re.I)


def db_url_parts(url: str) -> dict[str, str]:
    """user / password / host / port / dbname / project_ref from a URI or key=value DSN."""
    if not url:
        return {}
    parts: dict[str, str] = {}
    if "://" in url:
        u = urlsplit(url)
        parts = {"user": unquote(u.username or ""), "password": u.password or "", "host": u.hostname or "",
                 "port": str(u.port or ""), "dbname": u.path.lstrip("/")}
        if parts["password"]:
            parts["password_decoded"] = unquote(parts["password"])
    else:
        for tok in url.split():
            k, _, v = tok.partition("=")
            parts[k] = v.strip("'\"")
    user, host = parts.get("user", ""), parts.get("host", "")
    m = re.match(r"postgres\.([a-z0-9]{15,})$", user) or re.match(r"db\.([a-z0-9]{15,})\.supabase\.co$", host)
    if m:
        parts["project_ref"] = m.group(1)
    return parts


def sensitive_values(env=None) -> list[str]:
    env = os.environ if env is None else env
    values: set[str] = set()
    for key in SECRET_ENV:
        v = env.get(key) or ""
        if v:
            values.add(v)
    for key in ("SUPABASE_DB_URL", "TEST_DATABASE_URL"):
        p = db_url_parts(env.get(key) or "")
        for k in ("user", "password", "password_decoded", "host", "project_ref"):
            if p.get(k):
                values.add(p[k])
    return sorted((v for v in values if len(v) >= MIN_LEN and v.lower() not in GENERIC), key=len, reverse=True)


def redact(text, env=None) -> str:
    """Replace every sensitive value (longest first), any postgres URL and any password=... in `text`."""
    if text is None:
        return text
    out = str(text)
    for v in sensitive_values(env):
        out = out.replace(v, REDACTED)
    out = _DSN.sub(f"postgresql://{REDACTED}", out)
    return _KV_PASSWORD.sub(lambda m: m.group(1) + REDACTED, out)


def mask_commands(env=None) -> list[str]:
    return [f"::add-mask::{v}" for v in sensitive_values(env)]


def main() -> int:
    """In GitHub Actions: print ::add-mask:: for every sensitive value (the runner hides those lines)."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("refusing: this prints secret values and is only for GitHub Actions log masking", file=sys.stderr)
        return 2
    for line in mask_commands():
        print(line)
    print(f"masked {len(sensitive_values())} value(s) derived from secrets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
