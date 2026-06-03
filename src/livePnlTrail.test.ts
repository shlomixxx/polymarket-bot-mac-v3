import { describe, it, expect } from "vitest";
import { growLiveTrail } from "./livePnlTrail";

describe("growLiveTrail (Fix B — keep the open-position chart moving during server pnl_path gaps)", () => {
  it("appends the first point to an empty trail", () => {
    const out = growLiveTrail([], -1.5, 1000);
    expect(out).toEqual([{ ts: 1000, upnl_pct: -1.5 }]);
  });

  it("appends a new point once the throttle interval has passed", () => {
    const prev = [{ ts: 1000, upnl_pct: -1.5 }];
    const out = growLiveTrail(prev, 2.0, 1001.0); // 1s later
    expect(out).toHaveLength(2);
    expect(out[1]).toEqual({ ts: 1001.0, upnl_pct: 2.0 });
  });

  it("does NOT append within the throttle window (returns the same ref)", () => {
    const prev = [{ ts: 1000, upnl_pct: -1.5 }];
    const out = growLiveTrail(prev, 2.0, 1000.4); // 0.4s later < 1s
    expect(out).toBe(prev); // identity — no growth, no churn
  });

  it("caps the trail length, keeping the most recent points", () => {
    let trail: { ts: number; upnl_pct: number }[] = [];
    for (let i = 0; i < 10; i++) trail = growLiveTrail(trail, i, 1000 + i, { maxLen: 5, minIntervalSec: 0.5 });
    expect(trail).toHaveLength(5);
    expect(trail[trail.length - 1].upnl_pct).toBe(9); // newest kept
    expect(trail[0].upnl_pct).toBe(5); // oldest trimmed
  });

  it("ignores non-finite live pct (returns same ref)", () => {
    const prev = [{ ts: 1000, upnl_pct: -1.5 }];
    expect(growLiveTrail(prev, NaN, 1002)).toBe(prev);
  });
});
