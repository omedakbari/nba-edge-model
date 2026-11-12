import { describe, expect, it } from "vitest";
import { age, findPlayer, loadSlate } from "./slate";

describe("fixture", () => {
  it("is synthetic and loads", () => {
    const s = loadSlate();
    expect(s.players.length).toBeGreaterThan(0);
    expect(findPlayer(s, 1)?.name).toBe("Sample Guard");
  });
  it("formats ages", () => {
    expect(age("2026-11-12T22:56:00Z", "2026-11-12T23:00:00Z")).toBe("4m");
    expect(age("2026-11-12T20:00:00Z", "2026-11-12T23:00:00Z")).toBe("3h");
    expect(age("2026-11-09T23:00:00Z", "2026-11-12T23:00:00Z")).toBe("3d");
  });
});
