/**
 * engineStats — מפרק את פיצול ה-`by_engine` שהשרת מחזיר (main.py `_by_engine_breakdown`)
 * למודל-תצוגה. השרת מייחס כל יציאה ממומשת למנוע ש*פתח* את הפוזיציה (אסטרטגיה מול
 * טריגר/מסחר-מהיר), כדי שה-UI יציג את ביצועי-האסטרטגיה האמיתיים בנפרד מתוצאות
 * המסחר-המהיר במקום לערבב אותם ל"אחוז ניצחונות" מטעה אחד.
 *
 * צורת ה-JSON (מובטח שקיימת תמיד עם strategy+trigger כשהמפתחות קיימים):
 *   by_engine.{strategy,trigger} = {
 *     bot_run_win_rate_pct,       // number | null (null ⇔ exit_trades_n === 0)
 *     bot_run_exit_trades_n,      // int
 *     bot_run_wins_n,             // int
 *     bot_run_losses_n,           // int
 *     bot_run_realized_pnl_usd,   // number (USD)
 *   }
 */

export type RawEngineStat = {
  bot_run_win_rate_pct?: number | null;
  bot_run_exit_trades_n?: number;
  bot_run_wins_n?: number;
  bot_run_losses_n?: number;
  bot_run_realized_pnl_usd?: number;
};

export type RawByEngine = {
  strategy?: RawEngineStat;
  trigger?: RawEngineStat;
};

export type EngineStat = {
  /** אחוז ניצחונות; null כשאין יציאות (n === 0). */
  pct: number | null;
  /** מספר יציאות ממומשות שיוחסו למנוע. */
  n: number;
  wins: number;
  losses: number;
  /** רווח/הפסד ממומש מצטבר במנוע (USD). */
  pnl: number;
};

export type EngineStatsView = {
  strategy: EngineStat;
  trigger: EngineStat;
  /** true כשלמנוע-האסטרטגיה 0 יציאות בסשן — הצג הודעת "אין עדיין עסקאות אסטרטגיה". */
  strategyEmpty: boolean;
};

function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function parseEngine(e: RawEngineStat | null | undefined): EngineStat {
  const n = num(e?.bot_run_exit_trades_n);
  const wins = num(e?.bot_run_wins_n);
  const losses = num(e?.bot_run_losses_n, Math.max(0, n - wins));
  const pct =
    typeof e?.bot_run_win_rate_pct === "number" && Number.isFinite(e.bot_run_win_rate_pct)
      ? e.bot_run_win_rate_pct
      : null;
  return { pct, n, wins, losses, pnl: num(e?.bot_run_realized_pnl_usd) };
}

/**
 * ממיר את payload ה-`by_engine` למודל-תצוגה. מחזיר null כשאין payload כלל
 * (לפני תחילת סשן / שרת ישן) — כדי שה-UI ייפול חזרה לתצוגה המאוחדת.
 */
export function deriveEngineStatsView(
  byEngine: RawByEngine | null | undefined,
): EngineStatsView | null {
  if (!byEngine || typeof byEngine !== "object") return null;
  const strategy = parseEngine(byEngine.strategy);
  const trigger = parseEngine(byEngine.trigger);
  return { strategy, trigger, strategyEmpty: strategy.n === 0 };
}
