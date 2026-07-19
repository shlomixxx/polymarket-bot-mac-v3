import { describe, it, expect } from "vitest";
import { deriveEngineStatsView } from "./engineStats";

describe("deriveEngineStatsView", () => {
  it("מחזיר null כשאין by_engine (סשן לא התחיל / שרת ישן)", () => {
    expect(deriveEngineStatsView(null)).toBeNull();
    expect(deriveEngineStatsView(undefined)).toBeNull();
  });

  it("מפרק את שני המנועים לפי צורת ה-JSON של השרת", () => {
    const v = deriveEngineStatsView({
      strategy: {
        bot_run_win_rate_pct: 0,
        bot_run_exit_trades_n: 1,
        bot_run_wins_n: 0,
        bot_run_losses_n: 1,
        bot_run_realized_pnl_usd: -3,
      },
      trigger: {
        bot_run_win_rate_pct: 100,
        bot_run_exit_trades_n: 1,
        bot_run_wins_n: 1,
        bot_run_losses_n: 0,
        bot_run_realized_pnl_usd: 5,
      },
    })!;
    expect(v.strategy).toEqual({ pct: 0, n: 1, wins: 0, losses: 1, pnl: -3 });
    expect(v.trigger).toEqual({ pct: 100, n: 1, wins: 1, losses: 0, pnl: 5 });
    expect(v.strategyEmpty).toBe(false);
  });

  it("מסמן strategyEmpty כשלמנוע האסטרטגיה 0 יציאות (כל הפעילות היא טריגר)", () => {
    const v = deriveEngineStatsView({
      strategy: {
        bot_run_win_rate_pct: null,
        bot_run_exit_trades_n: 0,
        bot_run_wins_n: 0,
        bot_run_losses_n: 0,
        bot_run_realized_pnl_usd: 0,
      },
      trigger: {
        bot_run_win_rate_pct: 50,
        bot_run_exit_trades_n: 72,
        bot_run_wins_n: 36,
        bot_run_losses_n: 36,
        bot_run_realized_pnl_usd: 12.5,
      },
    })!;
    expect(v.strategyEmpty).toBe(true);
    expect(v.strategy.pct).toBeNull();
    expect(v.trigger.n).toBe(72);
  });

  it("ברירת מחדל בטוחה (אפסים) לשדות חסרים/פגומים", () => {
    const v = deriveEngineStatsView({ strategy: {}, trigger: {} })!;
    expect(v.strategy).toEqual({ pct: null, n: 0, wins: 0, losses: 0, pnl: 0 });
    expect(v.trigger).toEqual({ pct: null, n: 0, wins: 0, losses: 0, pnl: 0 });
    expect(v.strategyEmpty).toBe(true);
  });
});
