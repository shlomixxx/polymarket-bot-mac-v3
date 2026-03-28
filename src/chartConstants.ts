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
