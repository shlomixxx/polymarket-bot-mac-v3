import type { CSSProperties } from "react";

/**
 * ברירות מחדל מאוחדות ל־Recharts — הרמוניה: Tooltip, צירים, אותו מקור כמו index.css
 * מסלול תשואה: monotone (חלק). PnL מצטבר / BTC: natural כשיש מספיק נקודות.
 */
export const chartTooltipStyle: CSSProperties = {
  backgroundColor: "var(--chart-tooltip-bg)",
  border: "1px solid var(--chart-tooltip-border)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--text)",
  boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
};

export const chartAxisTick = { fill: "var(--chart-axis)", fontSize: 11 };

/** עקומת מסלול תשואה % — חלקה (הרמוניה), לא linear */
export const pnlCurveKind = "monotone" as const;

export const chartStroke = {
  width: 2.5,
  linecap: "round" as const,
  linejoin: "round" as const,
};

/** PnL מצטבר / גרפים כלליים: spline כשיש מספיק נקודות */
export function smoothCurveType(pointCount: number): "natural" | "monotone" {
  return pointCount >= 3 ? "natural" : "monotone";
}

/**
 * גרף מחיר BTC בדשבורד — תמיד monotone (לא natural).
 * natural (spline) על דגימות תכופות יוצר לעיתים קו "מדרגות" / קפיצות חזותיות;
 * monotone שומר על עקומה חלקה בלי אוברשוט בין נקודות.
 */
export const btcPriceLineCurveType = "monotone" as const;

/**
 * טווח ציר Y לגרף מחיר BTC — מונע «זום מיקרוסקופי» כשכל הדגימות באותו טווח סנטים
 * (אז כל רעש עיגול נראה כמו קו מסרק / קפיצות ענקיות).
 */
export function computeBtcPriceChartYDomain(
  prices: number[],
  referencePrice: number | null | undefined,
): [number, number] | undefined {
  const valid = prices.filter((p) => typeof p === "number" && Number.isFinite(p));
  let lo = valid.length ? Math.min(...valid) : Number.NaN;
  let hi = valid.length ? Math.max(...valid) : Number.NaN;
  if (referencePrice != null && Number.isFinite(referencePrice)) {
    if (!Number.isFinite(lo)) lo = referencePrice;
    else lo = Math.min(lo, referencePrice);
    if (!Number.isFinite(hi)) hi = referencePrice;
    else hi = Math.max(hi, referencePrice);
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return undefined;
  const mid = (lo + hi) / 2;
  let span = Math.max(hi - lo, 0);
  // לפחות ~45$ או ~0.06% סביב המחיר — תמיד יש טווח קריא
  const minSpan = Math.max(45, mid * 0.0006);
  if (span < minSpan) {
    const half = minSpan / 2;
    lo = mid - half;
    hi = mid + half;
  } else {
    const pad = Math.max(span * 0.06, 10);
    lo -= pad;
    hi += pad;
  }
  return [lo, hi];
}
