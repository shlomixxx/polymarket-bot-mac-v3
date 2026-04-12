/**
 * החלקת סדרת run P&L לתצוגה בגרף צופים — מסירה דגימות חריגות (ספיקים) בלי לשנות את ה-API של LiveStreamTrade.
 */

export type RunPnlPt = { t: number; usd: number };

const MEDIAN_WIN = 5;
const HAMPEL_K = 3;
const HAMPEL_SCALE = 1.4826;

function medianSorted(sorted: number[]): number {
  const n = sorted.length;
  if (n === 0) return 0;
  return n % 2 === 1 ? sorted[(n - 1) / 2]! : (sorted[n / 2 - 1]! + sorted[n / 2]!) / 2;
}

/** חציון חלון ממורכז (קצוות עם חיווי לאינדקס) — מסיר קפיצות צרות של דגימה אחת או שתיים */
export function rollingMedianUsd(points: RunPnlPt[], windowSize: number): RunPnlPt[] {
  if (points.length <= 2) return points;
  let w = windowSize % 2 === 0 ? windowSize + 1 : windowSize;
  w = Math.max(3, w);
  const half = (w - 1) / 2;
  const usd = points.map((p) => p.usd);
  const out: RunPnlPt[] = [];
  for (let i = 0; i < points.length; i++) {
    const slice: number[] = [];
    for (let j = -half; j <= half; j++) {
      const idx = Math.max(0, Math.min(points.length - 1, i + j));
      slice.push(usd[idx]!);
    }
    slice.sort((a, b) => a - b);
    const m = medianSorted(slice);
    out.push({ t: points[i]!.t, usd: m });
  }
  return out;
}

/**
 * Hampel חלון 5 על ערכי usd — מזהה חריגים ביחס לחציון המקומי (מתאים לרצף נקודות שגויות קצר).
 */
export function hampelMedian5(points: RunPnlPt[]): RunPnlPt[] {
  if (points.length < 5) return points;
  const usd = points.map((p) => p.usd);
  const out = points.map((p) => ({ ...p }));
  for (let i = 2; i < points.length - 2; i++) {
    const win = [usd[i - 2]!, usd[i - 1]!, usd[i]!, usd[i + 1]!, usd[i + 2]!];
    const sorted = [...win].sort((a, b) => a - b);
    const med = medianSorted(sorted);
    const absDevs = win.map((x) => Math.abs(x - med)).sort((a, b) => a - b);
    const mad = Math.max(medianSorted(absDevs), 0.25);
    const thresh = Math.max(2.5, HAMPEL_K * HAMPEL_SCALE * mad);
    if (Math.abs(usd[i]! - med) > thresh) {
      out[i] = { ...out[i]!, usd: med };
    }
  }
  return out;
}

/** קצה שקופץ מול שתי נקודות פנימיות (מגמה שטוחה) */
export function smoothRunPnlEndpoints(points: RunPnlPt[]): RunPnlPt[] {
  if (points.length < 2) return points;
  const out = points.map((p) => ({ ...p }));
  const n = out.length;

  const fixOneEnd = (atLast: boolean) => {
    if (n < 3) return;
    const i = atLast ? n - 1 : 0;
    const p = atLast ? n - 2 : 1;
    const q = atLast ? n - 3 : 2;
    const uq = out[q]!.usd;
    const up = out[p]!.usd;
    const uEdge = out[i]!.usd;
    const baseline = Math.abs(up - uq);
    const jump = Math.abs(uEdge - up);
    if (jump >= 3 && jump > baseline * 1.75 + 2 && baseline < 22) {
      out[i] = { ...out[i]!, usd: up };
    }
  };

  fixOneEnd(true);
  fixOneEnd(false);
  return out;
}

/**
 * נקודה פנימית שחורגת מהמיתאר בין שכנים — קפיץ וחזרה.
 * הקצוות מועתקים; קריאות נפרדות מטפלות בקצה.
 */
export function smoothRunPnlSpikesOnce(points: RunPnlPt[]): RunPnlPt[] {
  if (points.length < 3) return points;
  const n = points.length;
  const out: RunPnlPt[] = new Array(n);
  out[0] = points[0]!;
  out[n - 1] = points[n - 1]!;
  for (let i = 1; i < n - 1; i++) {
    const a = points[i - 1]!;
    const b = points[i]!;
    const c = points[i + 1]!;
    const tSpan = c.t - a.t;
    const expected = tSpan > 1e-9 ? a.usd + ((b.t - a.t) / tSpan) * (c.usd - a.usd) : (a.usd + c.usd) / 2;
    const dev = Math.abs(b.usd - expected);
    const neighborDelta = Math.abs(c.usd - a.usd);
    const flatNeighbors = neighborDelta < 12;
    const isolatedBlip =
      dev >= 1.8 &&
      (dev > neighborDelta * 1.08 + 1.5 || (flatNeighbors && dev > 3)) &&
      (neighborDelta < 18 || dev > neighborDelta * 1.5);
    out[i] = isolatedBlip ? { t: b.t, usd: expected } : b;
  }
  return out;
}

/**
 * קצה אחרון אם הוא חריג חזק ביחס לחציון של עד 9 נקודות אחרונות (מונע "High" מזויף בסוף הגרף).
 */
export function snapOutlierLastPoint(points: RunPnlPt[]): RunPnlPt[] {
  if (points.length < 4) return points;
  const n = points.length;
  const k = Math.min(9, n);
  const tail = points.slice(n - k);
  const vals = tail.map((p) => p.usd).sort((a, b) => a - b);
  const med = medianSorted(vals);
  const absDevs = tail.map((p) => Math.abs(p.usd - med)).sort((a, b) => a - b);
  const mad = Math.max(medianSorted(absDevs), 0.35);
  const last = points[n - 1]!.usd;
  const thresh = Math.max(6, 3.5 * HAMPEL_SCALE * mad);
  if (Math.abs(last - med) > thresh) {
    const out = points.map((p) => ({ ...p }));
    out[n - 1] = { ...out[n - 1]!, usd: med };
    return out;
  }
  return points;
}

/** צינור מלא לתצוגת גרף */
export function smoothRunPnlForChart(points: RunPnlPt[]): RunPnlPt[] {
  if (points.length === 0) return points;
  let p = rollingMedianUsd(points, MEDIAN_WIN);
  p = rollingMedianUsd(p, MEDIAN_WIN);
  p = hampelMedian5(p);
  p = smoothRunPnlEndpoints(p);
  p = smoothRunPnlSpikesOnce(p);
  p = smoothRunPnlSpikesOnce(p);
  p = smoothRunPnlSpikesOnce(p);
  p = smoothRunPnlEndpoints(p);
  p = snapOutlierLastPoint(p);
  return p;
}
