import { describe, expect, it } from "vitest";
import {
  hampelMedian5,
  rollingMedianUsd,
  smoothRunPnlForChart,
  smoothRunPnlSpikesOnce,
  snapOutlierLastPoint,
  type RunPnlPt,
} from "./runPnlSmoothing";

function linSeries(
  n: number,
  startT: number,
  dt: number,
  usdFn: (i: number) => number,
): RunPnlPt[] {
  const out: RunPnlPt[] = [];
  for (let i = 0; i < n; i++) {
    out.push({ t: startT + i * dt, usd: usdFn(i) });
  }
  return out;
}

describe("smoothRunPnlForChart", () => {
  it("מסיר ספיק צר באמצע (קפיץ וחזרה)", () => {
    const raw = linSeries(20, 1_700_000_000, 1, (i) => {
      if (i === 10) return 130;
      return -75;
    });
    const s = smoothRunPnlForChart(raw);
    const mid = s.find((p) => Math.abs(p.t - raw[10]!.t) < 1e-6);
    expect(mid).toBeDefined();
    expect(Math.abs(mid!.usd - -75)).toBeLessThan(25);
  });

  it("מוריד קצה אחרון חריג מול זנב שטוח (כמו High מזויף בסוף)", () => {
    const raw = linSeries(30, 1_700_000_000, 1, (i) => (i < 29 ? 8 + (i % 3) * 0.1 : 296));
    const s = smoothRunPnlForChart(raw);
    const last = s[s.length - 1]!;
    expect(last.usd).toBeLessThan(80);
    expect(Math.abs(last.usd - 8)).toBeLessThan(15);
  });

  it("שומה על מגמת עליה לגיטימית ללא ספיק בודד", () => {
    const raw = linSeries(25, 1_700_000_000, 1, (i) => -50 + i * 4);
    const s = smoothRunPnlForChart(raw);
    expect(s[s.length - 1]!.usd).toBeGreaterThan(40);
    expect(s[0]!.usd).toBeLessThan(-40);
  });

  it("סדרה קצרה לא נופלת", () => {
    const raw: RunPnlPt[] = [
      { t: 1, usd: 1 },
      { t: 2, usd: 99 },
    ];
    expect(smoothRunPnlForChart(raw)).toEqual(raw);
  });
});

describe("rollingMedianUsd", () => {
  it("מחליף נקודה בודדת חריגה", () => {
    const raw = linSeries(7, 100, 1, (i) => (i === 3 ? 200 : 10));
    const m = rollingMedianUsd(raw, 5);
    expect(m[3]!.usd).toBe(10);
  });
});

describe("hampelMedian5", () => {
  it("מזהה חריג ביחס לחלון", () => {
    const raw = linSeries(9, 0, 1, (i) => (i === 4 ? 500 : 2));
    const h = hampelMedian5(raw);
    expect(h[4]!.usd).toBeLessThan(50);
  });
});

describe("smoothRunPnlSpikesOnce", () => {
  it("מתקן נקודת אמצע על קו בין שכנים", () => {
    const pts: RunPnlPt[] = [
      { t: 0, usd: 10 },
      { t: 1, usd: 200 },
      { t: 2, usd: 10 },
    ];
    const o = smoothRunPnlSpikesOnce(pts);
    expect(o[1]!.usd).toBeCloseTo(10, 0);
  });
});

describe("snapOutlierLastPoint", () => {
  it("מחליף רק את הנקודה האחרונה כשהיא חריגה", () => {
    const pts = linSeries(12, 0, 1, (i) => (i < 11 ? 5 : 400));
    const s = snapOutlierLastPoint(pts);
    expect(s[11]!.usd).toBeLessThan(50);
  });
});
