import { describe, it, expect } from "vitest";
import {
  outcomeOf,
  windowSecForSlug,
  driftOf,
  deriveWindowStats,
  upRateTint,
  deriveTradeWindowView,
  clockHms,
  type RecentWindow,
} from "./windowStats";

const mk = (epoch: number, side_won: string | null, extra: Partial<RecentWindow> = {}): RecentWindow => ({
  epoch,
  side_won,
  ...extra,
});

describe("outcomeOf", () => {
  it("ממפה Up/Down/אחר לתוצאה", () => {
    expect(outcomeOf(mk(1, "Up"))).toBe("up");
    expect(outcomeOf(mk(1, "Down"))).toBe("down");
    expect(outcomeOf(mk(1, null))).toBe("unknown");
    expect(outcomeOf(mk(1, "weird"))).toBe("unknown");
  });
});

describe("windowSecForSlug", () => {
  it("15m → 900, 5m → 300, ברירת מחדל 300", () => {
    expect(windowSecForSlug("btc-updown-15m-1782938400")).toBe(900);
    expect(windowSecForSlug("btc-updown-5m-1782938400")).toBe(300);
    expect(windowSecForSlug(undefined)).toBe(300);
    expect(windowSecForSlug(null)).toBe(300);
    expect(windowSecForSlug("")).toBe(300);
  });
});

describe("driftOf", () => {
  it("מחשב תזוזה ב-$ ו-% כשיש open+close", () => {
    expect(driftOf(mk(1, "Up", { btc_open: 100, btc_close: 110 }))).toEqual({ abs: 10, pct: 10 });
    expect(driftOf(mk(1, "Down", { btc_open: 200, btc_close: 190 }))).toEqual({ abs: -10, pct: -5 });
  });
  it("מחזיר null כשחסר מחיר או open=0 (בלי חלוקה באפס)", () => {
    expect(driftOf(mk(1, "Up"))).toEqual({ abs: null, pct: null });
    expect(driftOf(mk(1, "Up", { btc_open: 100 }))).toEqual({ abs: null, pct: null });
    expect(driftOf(mk(1, "Up", { btc_open: 0, btc_close: 5 }))).toEqual({ abs: null, pct: null });
  });
});

describe("deriveWindowStats", () => {
  it("רצף מגמה: Up,Up,Up,Up", () => {
    const s = deriveWindowStats([mk(10, "Up"), mk(20, "Up"), mk(30, "Up"), mk(40, "Up")]);
    expect(s.upCount).toBe(4);
    expect(s.downCount).toBe(0);
    expect(s.upRate).toBe(1);
    expect(s.currentStreak).toBe(4);
    expect(s.currentStreakDir).toBe("up");
    expect(s.longestStreak).toBe(4);
    expect(s.alternations).toBe(0);
    expect(s.chopScore).toBe(0);
  });

  it("דשדוש מלא: Up,Down,Up,Down → chopScore=1, רצף נוכחי 1", () => {
    const s = deriveWindowStats([mk(10, "Up"), mk(20, "Down"), mk(30, "Up"), mk(40, "Down")]);
    expect(s.upCount).toBe(2);
    expect(s.downCount).toBe(2);
    expect(s.upRate).toBe(0.5);
    expect(s.alternations).toBe(3);
    expect(s.maxAlternations).toBe(3);
    expect(s.chopScore).toBe(1);
    expect(s.currentStreak).toBe(1);
    expect(s.currentStreakDir).toBe("down");
    expect(s.longestStreak).toBe(1);
  });

  it("חלונות לא ידועים לא נספרים ולא שוברים רצף/דשדוש", () => {
    const s = deriveWindowStats([mk(10, "Up"), mk(20, null), mk(30, "Down")]);
    expect(s.knownN).toBe(2);
    expect(s.unknownN).toBe(1);
    expect(s.alternations).toBe(1);
    expect(s.maxAlternations).toBe(1);
    expect(s.chopScore).toBe(1);
  });

  it("לא תלוי בסדר הקלט — ממיין לפי epoch (הרצף הנוכחי לפי החדש ביותר)", () => {
    // newest epoch (40) is Down → current dir must be down regardless of input order
    const s = deriveWindowStats([mk(40, "Down"), mk(10, "Up"), mk(30, "Up"), mk(20, "Up")]);
    expect(s.currentStreakDir).toBe("down");
    expect(s.currentStreak).toBe(1);
    expect(s.longestStreak).toBe(3); // Up,Up,Up in the middle
  });

  it("מערך ריק → אפסים בטוחים", () => {
    const s = deriveWindowStats([]);
    expect(s.total).toBe(0);
    expect(s.knownN).toBe(0);
    expect(s.upRate).toBeNull();
    expect(s.currentStreak).toBe(0);
    expect(s.currentStreakDir).toBeNull();
    expect(s.chopScore).toBe(0);
  });
});

describe("upRateTint", () => {
  it("total=0 → ניטרלי", () => {
    expect(upRateTint(0.5, 0)).toEqual({ bg: "var(--card-hover)", fg: "var(--muted)" });
  });
  it("upRate גבוה נוטה לירוק (--up rgb), נמוך לאדום (--down rgb)", () => {
    expect(upRateTint(1, 5).bg).toContain("74,155,126");
    expect(upRateTint(0, 5).bg).toContain("184,92,92");
  });
});

describe("clockHms", () => {
  it("מחזיר — לזמן לא תקין ומחרוזת לא ריקה לזמן תקין", () => {
    expect(clockHms(0)).toBe("—");
    expect(clockHms(Number.NaN)).toBe("—");
    expect(clockHms(1782938400)).not.toBe("—");
    expect(clockHms(1782938400).length).toBeGreaterThan(0);
  });
});

describe("deriveTradeWindowView", () => {
  const recent = [mk(100, "Up"), mk(200, "Down"), mk(300, "Up"), mk(400, "Down"), mk(500, "Up")];

  it("צבע החלון של העסקה נגזר ממחירי ההתחשבנות שלה (מקור אמת)", () => {
    const v = deriveTradeWindowView({
      epoch: 300, windowSec: 300, btcStart: 100, btcEnd: 120,
      side: "Up", recentWindows: recent,
    });
    expect(v.focus?.outcome).toBe("up");
    expect(v.focus?.won).toBe(true);
    expect(v.driftUsd).toBe(20);
    expect(v.focus?.isFocus).toBe(true);
  });

  it("הימור מנוגד לתוצאה → הפסד (won=false)", () => {
    const v = deriveTradeWindowView({
      epoch: 300, windowSec: 300, btcStart: 100, btcEnd: 90,
      side: "Up", recentWindows: recent,
    });
    expect(v.focus?.outcome).toBe("down");
    expect(v.focus?.won).toBe(false);
  });

  it("בונה רצועת הקשר סביב החלון, עם החלון של העסקה מסומן", () => {
    const v = deriveTradeWindowView({
      epoch: 300, windowSec: 300, btcStart: 100, btcEnd: 120,
      side: "Up", recentWindows: recent, contextRadius: 1,
    });
    expect(v.strip.map((d) => d.epoch)).toEqual([200, 300, 400]);
    expect(v.strip.find((d) => d.epoch === 300)?.isFocus).toBe(true);
  });

  it("epoch=null → אין focus", () => {
    const v = deriveTradeWindowView({ epoch: null, windowSec: 300, recentWindows: recent });
    expect(v.focus).toBeNull();
    expect(v.timeStart).toBe("—");
  });
});
