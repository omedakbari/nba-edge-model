// Shape of the data the player page reads. Phase 3 serves it from fixtures/sample-slate.json
// (synthetic); later the same shape comes from the database (rows from prop_worker.slate --json).
import sample from "@/fixtures/sample-slate.json";
import type { GameLine, Stat } from "./hitRates";

export interface BookQuote {
  line: number;
  over: number | null;
  under: number | null;
  seen: string; // ISO timestamp the price was last confirmed
}

export interface KalshiStrike {
  line: number;
  yes_ask: number | null;
  no_ask: number | null;
  seen: string;
}

export interface StatLines {
  books: Record<string, BookQuote>;
  kalshi: KalshiStrike[];
}

export interface Injury {
  code: string;
  status: string;
  source: "official" | "espn";
  reason?: string | null;
}

export interface PlayerGame extends GameLine {
  is_home: boolean;
}

export interface SlatePlayer {
  player_id: number;
  name: string;
  team: string;
  opp: string;
  is_home: boolean;
  tip: string;
  injury: Injury | null;
  lines: Partial<Record<Stat, StatLines>>;
  games: PlayerGame[];
}

export interface Slate {
  generated_at: string;
  params: { before: string; season_start: string; h2h_since: string };
  players: SlatePlayer[];
}

export function loadSlate(): Slate {
  return sample as unknown as Slate;
}

export function findPlayer(slate: Slate, id: number): SlatePlayer | undefined {
  return slate.players.find((p) => p.player_id === id);
}

/** "4m", "2h", "3d" between a price's last confirmation and the slate's as-of time. */
export function age(seen: string, asOf: string): string {
  const mins = Math.max(0, Math.round((Date.parse(asOf) - Date.parse(seen)) / 60000));
  if (mins < 60) return `${mins}m`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}
