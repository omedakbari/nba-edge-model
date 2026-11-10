"""Shared helpers for job tests: fixture loading, a routing fake HTTP session, player seeding."""

import copy
import json
from datetime import datetime

from conftest import FIXTURES
from prop_worker import config, xref
from prop_worker.runner import Context

NBA_ABBRS = ["ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL",
             "MEM", "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR",
             "UTA", "WAS"]
_ESPN_DIFFERENT = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
ESPN_TO_ABBR = {**{a: a for a in NBA_ABBRS if a not in _ESPN_DIFFERENT.values()}, **_ESPN_DIFFERENT}

NOW = datetime(2026, 11, 10, 13, 40, tzinfo=config.ET)       # 1:40 pm ET on a 6-game day


def load(name):
    return copy.deepcopy(json.loads((FIXTURES / name).read_text()))


class Resp:
    def __init__(self, payload=None, status=200, content=b"", headers=None):
        self._payload, self.status_code, self.content = payload, status, content
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Net:
    """Fake session. `routes` maps a predicate (url, params) -> Resp | callable returning Resp."""

    def __init__(self, routes=None, heads=None):
        self.routes = list(routes or [])
        self.heads = heads or {}
        self.calls = []
        self.posts = []

    def add(self, match, resp):
        self.routes.insert(0, (match, resp))
        return self

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for match, resp in self.routes:
            if match(url, params or {}):
                return resp(url, params or {}) if callable(resp) else resp
        raise AssertionError(f"unrouted GET {url} {params}")

    def head(self, url, **kw):
        return Resp(status=self.heads.get(url, 403))

    def post(self, url, data=None, headers=None, **kw):
        self.posts.append((url, data, headers))
        return Resp()


def scoreboards(by_date=None):
    """Route ESPN scoreboard?dates=... to fixtures; any other date is an empty day."""
    by_date = by_date or {"20261109": load("espn/scoreboard_20261109.json"),
                          "20261110": load("espn/scoreboard_20261110.json")}
    empty = load("espn/scoreboard_20260801.json")
    return (lambda u, p: u.endswith("/scoreboard"),
            lambda u, p: Resp(by_date.get(p.get("dates"), empty)))


def ctx(pg, net, now=NOW, **options):
    return Context(pg, session=net, now=now, options=options)


def seed_players(pg, players):
    """players: [(nba_id, full_name, team_abbr)] — inserted as if from nba_edge.db, with this season's team."""
    from datetime import date
    xref.seed(pg, [(str(i), n, t, date(2026, 11, 8), True) for i, n, t in players])


def run(pg, net, name, fn, now=NOW, **options):
    from prop_worker.runner import run_job
    clock = iter([0.0, 5.0])                     # every job "takes" 5 s unless a test says otherwise
    return run_job(ctx(pg, net, now, **options), name, fn, clock=lambda: next(clock))
