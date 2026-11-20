"""Shared HTTP session: one honest User-Agent, fixed timeout, small retry on 5xx/connection errors."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HTTP_TIMEOUT, USER_AGENT


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    # 429 included: Kalshi rate-limits bursts across its series endpoints (2026-11-19 ingest RED).
    # Retry-After is honored when the server sends it.
    retry = Retry(total=2, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.request = _with_timeout(s.request)  # type: ignore[method-assign]
    return s


def _with_timeout(fn):
    def wrapped(method, url, **kw):
        kw.setdefault("timeout", HTTP_TIMEOUT)
        return fn(method, url, **kw)
    return wrapped
