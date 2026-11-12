"use client";

import { useMemo, useState } from "react";
import {
  consensusLine,
  hitRates,
  noVigOver,
  played,
  statValue,
  type RateKey,
  type Side,
  type Stat,
} from "@/lib/hitRates";
import { age, type PlayerGame, type SlatePlayer } from "@/lib/slate";

const STAT_LABEL: Record<Stat, string> = { pts: "PTS", reb: "REB", ast: "AST", fg3m: "3PM", pra: "PRA", ra: "R+A" };
const RATE_LABEL: [RateKey, string][] = [["l5", "L5"], ["l10", "L10"], ["l15", "L15"], ["season", "Season"], ["h2h", "H2H"]];
const BOOK_LABEL: Record<string, string> = {
  draftkings: "DK", fanduel: "FD", betmgm: "MGM", pinnacle: "PIN", sleeper: "Sleeper", prizepicks: "PP",
};
type Venue = "all" | "home" | "away";

interface Props {
  player: SlatePlayer;
  params: { before: string; season_start: string; h2h_since: string };
  asOf: string;
}

export default function PlayerView({ player, params, asOf }: Props) {
  const stats = (Object.keys(STAT_LABEL) as Stat[]).filter((s) => player.lines[s]);
  const [stat, setStat] = useState<Stat>(stats[0] ?? "pts");
  const lines = player.lines[stat];
  const [consensus, nBooks] = consensusLine(
    Object.fromEntries(Object.entries(lines?.books ?? {}).map(([b, q]) => [b, q.line])),
  );
  const startLine = consensus ?? lines?.books.sleeper?.line ?? 0.5;
  const [line, setLine] = useState<number>(startLine);
  const [side, setSide] = useState<Side>("over");
  const [venue, setVenue] = useState<Venue>("all");
  const [minMinutes, setMinMinutes] = useState<number>(0);

  function pickStat(s: Stat) {
    setStat(s);
    const l = player.lines[s];
    const [c] = consensusLine(Object.fromEntries(Object.entries(l?.books ?? {}).map(([b, q]) => [b, q.line])));
    setLine(c ?? l?.books.sleeper?.line ?? 0.5);
  }

  const games = useMemo(
    () =>
      player.games.filter(
        (g: PlayerGame) =>
          (venue === "all" || (venue === "home") === g.is_home) && (g.minutes ?? 0) >= minMinutes,
      ),
    [player.games, venue, minMinutes],
  );
  const rates = hitRates(games, stat, line, {
    before: params.before, seasonStart: params.season_start, opp: player.opp, h2hSince: params.h2h_since, side,
  });
  const nv =
    consensus === null
      ? null
      : noVigOver(
          Object.entries(lines?.books ?? {})
            .filter(([b, q]) => b !== "sleeper" && b !== "prizepicks" && q.line === consensus)
            .map(([, q]) => [q.over, q.under]),
        );

  return (
    <>
      <h1>{player.name}</h1>
      <div className="muted small">
        {player.team} {player.is_home ? "vs" : "@"} {player.opp} ·{" "}
        {new Date(player.tip).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" })} ET
      </div>
      {player.injury && (
        <div className="banner">
          {player.injury.status} ({player.injury.source === "official" ? "official report" : "ESPN"})
          {player.injury.reason ? `: ${player.injury.reason}` : ""}
        </div>
      )}

      <h2>Stat</h2>
      <div className="tabs" role="tablist">
        {stats.map((s) => (
          <button key={s} role="tab" className="tab" aria-selected={s === stat} onClick={() => pickStat(s)}>
            {STAT_LABEL[s]}
          </button>
        ))}
      </div>

      <h2>Line</h2>
      <div className="card">
        <div className="stepper">
          <button aria-label="Lower line" onClick={() => setLine((l) => Math.max(0.5, l - 0.5))}>−</button>
          <div>
            <div className="value">{line}</div>
            <div className="muted small" style={{ textAlign: "center" }}>
              {consensus !== null
                ? `consensus ${consensus} (${nBooks} book${nBooks === 1 ? "" : "s"})${nv !== null ? ` · no-vig over ${Math.round(nv * 100)}%` : ""}`
                : "no consensus line"}
            </div>
          </div>
          <button aria-label="Raise line" onClick={() => setLine((l) => l + 0.5)}>+</button>
        </div>
        <div className="filters" style={{ marginTop: 10, justifyContent: "center" }}>
          {(["over", "under"] as Side[]).map((s) => (
            <button key={s} className="chip" aria-pressed={side === s} onClick={() => setSide(s)}>
              {s === "over" ? "Over" : "Under"}
            </button>
          ))}
        </div>
      </div>

      <h2>Lines</h2>
      <div className="chips">
        {Object.entries(lines?.books ?? {}).map(([book, q]) => (
          <button key={book} className="chip" aria-pressed={q.line === line} onClick={() => setLine(q.line)}>
            {BOOK_LABEL[book] ?? book} {q.line}
            {q.over !== null ? ` ${q.over > 0 ? "+" : ""}${q.over}` : ""}
            <span className="age">{age(q.seen, asOf)}</span>
          </button>
        ))}
        {(lines?.kalshi ?? []).map((k) => (
          <button key={`k${k.line}`} className="chip" aria-pressed={k.line === line} onClick={() => setLine(k.line)}>
            Kalshi {k.line + 0.5}+ {k.yes_ask !== null ? `${Math.round(k.yes_ask * 100)}¢` : "--"}
            <span className="age">{age(k.seen, asOf)}</span>
          </button>
        ))}
      </div>

      <h2>Hit rate ({side})</h2>
      <div className="rates">
        {RATE_LABEL.map(([key, label]) => {
          const r = rates[key];
          return (
            <div key={key} className="card rate">
              <div className="label">{label}</div>
              <div className="pct">{r.pct === null ? "–" : `${Math.round(r.pct * 100)}%`}</div>
              <div className="count">{r.n ? `${r.hits}/${r.n}` : "no games"}</div>
            </div>
          );
        })}
      </div>

      <h2>Filters</h2>
      <div className="filters">
        {(["all", "home", "away"] as Venue[]).map((v) => (
          <button key={v} className="chip" aria-pressed={venue === v} onClick={() => setVenue(v)}>
            {v === "all" ? "All games" : v === "home" ? "Home" : "Away"}
          </button>
        ))}
        {[0, 25, 30].map((m) => (
          <button key={m} className="chip" aria-pressed={minMinutes === m} onClick={() => setMinMinutes(m)}>
            {m === 0 ? "Any minutes" : `${m}+ min`}
          </button>
        ))}
      </div>

      <h2>Last 15 games</h2>
      <div className="card">
        <GameChart games={games} stat={stat} line={line} side={side} before={params.before} />
      </div>
    </>
  );
}

function GameChart({ games, stat, line, side, before }: {
  games: PlayerGame[]; stat: Stat; line: number; side: Side; before: string;
}) {
  const recent = games
    .filter((g) => played(g) && g.game_date < before)
    .sort((a, b) => (a.game_date < b.game_date ? 1 : -1))
    .slice(0, 15)
    .reverse();
  if (!recent.length) return <p className="muted small">No games played yet.</p>;
  const values = recent.map((g) => statValue(g, stat) ?? 0);
  const max = Math.max(line * 1.4, ...values, 1);
  const W = 320, H = 160, pad = 18, bw = (W - pad) / recent.length;
  const y = (v: number) => H - pad - (v / max) * (H - pad * 2);
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
         aria-label={`${stat} in the last ${recent.length} games against a line of ${line}`}>
      {recent.map((g, i) => {
        const v = values[i];
        const hit = v === line ? "push" : (side === "over" ? v > line : v < line) ? "hit" : "miss";
        return (
          <g key={g.game_date}>
            <rect x={pad + i * bw + 2} y={y(v)} width={bw - 4} height={Math.max(1, H - pad - y(v))}
                  rx={3} className={`bar-${hit}`} />
            <text x={pad + i * bw + bw / 2} y={y(v) - 3} textAnchor="middle">{v}</text>
            <text x={pad + i * bw + bw / 2} y={H - 4} textAnchor="middle">{g.opp}</text>
          </g>
        );
      })}
      <line x1={pad} x2={W} y1={y(line)} y2={y(line)} className="line" />
    </svg>
  );
}
