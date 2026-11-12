// Hit-rate and line maths: a line-for-line port of worker/prop_worker/hitrates.py (used by
// `prop_worker.slate`). Both are checked against worker/tests/fixtures/shared/hit_rates_cases.json.
// Change a rule in one, change it in the other, and add a case. Rules are listed in hitrates.py.

export const STATS = ["pts", "reb", "ast", "fg3m", "pra", "ra"] as const;
export type Stat = (typeof STATS)[number];
export type Side = "over" | "under";
export const CONSENSUS_BOOKS = ["draftkings", "fanduel", "betmgm", "pinnacle"] as const;

export interface GameLine {
  game_date: string; // ISO date, ET
  opp: string;
  minutes: number | null;
  did_not_play: boolean;
  pts: number | null;
  reb: number | null;
  ast: number | null;
  fg3m: number | null;
}

export interface Rate {
  hits: number;
  n: number;
  pushes: number;
  pct: number | null;
}

export type RateKey = "l5" | "l10" | "l15" | "season" | "h2h";
export type Rates = Record<RateKey, Rate>;

const WINDOWS: [RateKey, number][] = [
  ["l5", 5],
  ["l10", 10],
  ["l15", 15],
];

export function played(g: GameLine): boolean {
  return !g.did_not_play && (g.minutes ?? 0) > 0;
}

export function statValue(g: GameLine, stat: Stat): number | null {
  const parts: (keyof GameLine)[] =
    stat === "pra" ? ["pts", "reb", "ast"] : stat === "ra" ? ["reb", "ast"] : [stat];
  let sum = 0;
  for (const p of parts) {
    const v = g[p];
    if (v === null || v === undefined) return null;
    sum += v as number;
  }
  return sum;
}

function rate(games: GameLine[], stat: Stat, line: number, side: Side): Rate {
  let hits = 0;
  let pushes = 0;
  let n = 0;
  for (const g of games) {
    const v = statValue(g, stat);
    if (v === null) continue;
    n += 1;
    if (v === line) pushes += 1;
    else if (side === "over" ? v > line : v < line) hits += 1;
  }
  return { hits, n, pushes, pct: n ? hits / n : null };
}

export interface HitRateOptions {
  before: string; // slate date: only games before it count
  seasonStart: string;
  opp: string | null;
  h2hSince: string;
  side?: Side;
}

export function hitRates(games: GameLine[], stat: Stat, line: number, o: HitRateOptions): Rates {
  const side = o.side ?? "over";
  if (side !== "over" && side !== "under") throw new Error("side must be 'over' or 'under'");
  if (!STATS.includes(stat)) throw new Error(`stat must be one of ${STATS.join(", ")}`);
  // ISO dates compare correctly as strings
  const past = games
    .filter((g) => played(g) && g.game_date < o.before)
    .sort((a, b) => (a.game_date < b.game_date ? 1 : a.game_date > b.game_date ? -1 : 0));
  const out = {} as Rates;
  for (const [key, n] of WINDOWS) out[key] = rate(past.slice(0, n), stat, line, side);
  out.season = rate(past.filter((g) => g.game_date >= o.seasonStart), stat, line, side);
  out.h2h = rate(
    past.filter((g) => o.opp !== null && g.opp === o.opp && g.game_date >= o.h2hSince),
    stat,
    line,
    side,
  );
  return out;
}

/** [line, number of consensus books offering it]; tie -> Pinnacle's line if tied, else the lower. */
export function consensusLine(lines: Record<string, number | null | undefined>): [number | null, number] {
  const offered = new Map<string, number>();
  for (const [book, v] of Object.entries(lines)) {
    if ((CONSENSUS_BOOKS as readonly string[]).includes(book) && v !== null && v !== undefined) {
      offered.set(book, v);
    }
  }
  if (offered.size === 0) return [null, 0];
  const counts = new Map<number, number>();
  for (const v of offered.values()) counts.set(v, (counts.get(v) ?? 0) + 1);
  const top = Math.max(...counts.values());
  const tied = [...counts.entries()].filter(([, c]) => c === top).map(([v]) => v).sort((a, b) => a - b);
  const pin = offered.get("pinnacle");
  return [pin !== undefined && tied.includes(pin) ? pin : tied[0], top];
}

export function implied(american: number | null | undefined): number | null {
  if (american === null || american === undefined || american === 0) return null;
  return american > 0 ? 100 / (american + 100) : -american / (-american + 100);
}

/** Mean no-vig P(over) over books, given [over, under] American prices at the same line. */
export function noVigOver(pairs: [number | null, number | null][]): number | null {
  const ps: number[] = [];
  for (const [over, under] of pairs) {
    const po = implied(over);
    const pu = implied(under);
    if (po !== null && pu !== null && po + pu > 0) ps.push(po / (po + pu));
  }
  return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : null;
}
