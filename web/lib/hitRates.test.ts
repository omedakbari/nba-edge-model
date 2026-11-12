// The same golden cases the Python side runs (worker/tests/test_hitrates.py).
import { describe, expect, it } from "vitest";
import cases from "../../worker/tests/fixtures/shared/hit_rates_cases.json";
import { consensusLine, hitRates, implied, noVigOver, type GameLine, type Side, type Stat } from "./hitRates";

const P = cases.params;
const games = cases.games as GameLine[];

describe("hit rates (shared golden cases)", () => {
  for (const c of cases.hit_rates) {
    it(`${c.stat} ${c.side} ${c.line}`, () => {
      const got = hitRates(games, c.stat as Stat, c.line, {
        before: P.before,
        seasonStart: P.season_start,
        opp: P.opp,
        h2hSince: P.h2h_since,
        side: c.side as Side,
      });
      const flat = Object.fromEntries(Object.entries(got).map(([k, r]) => [k, [r.hits, r.n, r.pushes]]));
      expect(flat).toEqual(c.expect);
    });
  }
});

describe("consensus line (shared golden cases)", () => {
  for (const c of cases.consensus) {
    it(JSON.stringify(c.lines), () => {
      expect(consensusLine(c.lines as Record<string, number | undefined>)).toEqual(c.expect);
    });
  }
});

describe("no-vig P(over) (shared golden cases)", () => {
  for (const c of cases.no_vig_over) {
    it(JSON.stringify(c.pairs), () => {
      const got = noVigOver(c.pairs as [number | null, number | null][]);
      if (c.expect === null) expect(got).toBeNull();
      else expect(Number(got!.toFixed(6))).toBe(c.expect);
    });
  }
});

describe("edges", () => {
  it("implied probability", () => {
    expect(implied(-110)).toBeCloseTo(0.5238, 4);
    expect(implied(150)).toBeCloseTo(0.4, 10);
    expect(implied(null)).toBeNull();
  });
  it("rejects unknown stats", () => {
    expect(() =>
      hitRates(games, "blk" as Stat, 1.5, { before: P.before, seasonStart: P.season_start, opp: null, h2hSince: P.h2h_since }),
    ).toThrow();
  });
});
