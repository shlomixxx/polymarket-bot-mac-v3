/**
 * חישוב גודל השקעה לתצוגה מקדימה בלוח — משקף 1:1 את לוגיקת המנוע בצד-שרת:
 *   - strategy_runner._investment_base_usd  (fixed → $ ; percent → equity × אחוז/100)
 *   - strategy_runner._effective_investment_usd  (× מכפיל loss-recovery כשהוא פעיל)
 *   - contracts_from_investment + max(min_contracts, ceil(order_min_size))
 *
 * עד התיקון הזה הלוח חישב חוזים תמיד מ-`inv` (הסכום הקבוע), גם במצב "אחוז מגודל
 * התיק" — כך שהתצוגה התעלמה מהאחוז והראתה מספר שגוי לגמרי מול מה שהמנוע באמת סוחר.
 */

export type InvestmentMode = "fixed" | "percent";

function finiteOr(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

/** בסיס ההשקעה ב-$ לפני מכפיל loss-recovery. משקף _investment_base_usd. */
export function investmentBaseUsd(args: {
  mode: InvestmentMode;
  fixedUsd: number;
  pctOfPortfolio: number;
  equityUsd: number;
}): number {
  if (args.mode === "percent") {
    const pct = finiteOr(args.pctOfPortfolio, 0);
    const equity = finiteOr(args.equityUsd, 0);
    if (pct <= 0 || equity <= 0) return 0;
    return Math.max(0, equity) * (pct / 100);
  }
  return Math.max(0, finiteOr(args.fixedUsd, 0));
}

/** הסכום האפקטיבי שהמנוע משתמש בו לכניסה הבאה. משקף _effective_investment_usd. */
export function effectiveInvestmentUsd(args: {
  mode: InvestmentMode;
  fixedUsd: number;
  pctOfPortfolio: number;
  equityUsd: number;
  lossRecoveryEnabled: boolean;
  lossRecoveryMult: number;
}): number {
  const base = investmentBaseUsd(args);
  if (!args.lossRecoveryEnabled) return base;
  let mult = finiteOr(args.lossRecoveryMult, 1);
  if (mult < 1) mult = 1;
  return base * mult;
}

/**
 * מספר חוזים מסכום השקעה במחיר נתון, עם רצפת מינימום. משקף contracts_from_investment:
 * floor(סכום / מחיר), ואם מתחת לרצפה — 0 (לא נכנסים).
 */
export function contractsFromInvestment(args: {
  investmentUsd: number;
  entryCents: number;
  minContracts: number;
}): number {
  const price = finiteOr(args.entryCents, 0) / 100;
  const usd = finiteOr(args.investmentUsd, 0);
  if (price <= 0 || usd <= 0) return 0;
  const n = Math.floor(usd / price);
  return n >= args.minContracts ? n : 0;
}

/**
 * רצפת חוזים אפקטיבית — לפחות מינ' המשתמש ולפחות מינ' הבורסה (עיגול כלפי מעלה).
 * כשהשוק עדיין לא נטען (orderMinSize לא ידוע) — ברירת מחדל בטוחה של 5 (מינ' Polymarket
 * הנפוץ לשווקי BTC Up/Down), כדי לא להציג תצוגה אופטימית מדי לפני שהשוק נטען.
 */
export function computeEffectiveMinContracts(
  minContracts: number,
  orderMinSize: number | undefined,
): number {
  const oms = orderMinSize != null ? Math.ceil(orderMinSize) : 5;
  return Math.max(minContracts, oms);
}
