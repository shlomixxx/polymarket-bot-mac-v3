/**
 * Enhanced run P&L smoothing for broadcast/pro layout.
 * Uses a stronger pipeline than the base chart smoothing — bigger windows,
 * lower spike thresholds, double EMA — to produce visually clean broadcast curves.
 */

import { type RunPnlPt } from "./runPnlSmoothing";

const EMA_ALPHA = 0.15;  // low alpha = heavy smoothing
const RAW_TAIL = 1;       // only the very last point stays raw ("Now" accuracy)

/* ── helpers ── */

function medianOf(arr: number[]): number {
  const s = [...arr].sort((a, b) => a - b);
  const n = s.length;
  return n % 2 === 1 ? s[(n - 1) / 2]! : (s[n / 2 - 1]! + s[n / 2]!) / 2;
}

/** Rolling median with a wider window (7) to crush multi-point spikes. */
function rollingMedian(pts: RunPnlPt[], win: number): RunPnlPt[] {
  if (pts.length <= 2) return pts;
  const w = win % 2 === 0 ? win + 1 : Math.max(3, win);
  const half = (w - 1) / 2;
  const usd = pts.map((p) => p.usd);
  return pts.map((p, i) => {
    const slice: number[] = [];
    for (let j = -half; j <= half; j++) {
      slice.push(usd[Math.max(0, Math.min(pts.length - 1, i + j))]!);
    }
    return { t: p.t, usd: medianOf(slice) };
  });
}

/** Hampel filter — marks and replaces outliers vs local median. Lower thresh than base. */
function hampel(pts: RunPnlPt[], halfWin: number, sigmaK: number): RunPnlPt[] {
  if (pts.length < halfWin * 2 + 1) return pts;
  const usd = pts.map((p) => p.usd);
  const out = pts.map((p) => ({ ...p }));
  for (let i = halfWin; i < pts.length - halfWin; i++) {
    const win: number[] = [];
    for (let j = -halfWin; j <= halfWin; j++) win.push(usd[i + j]!);
    const med = medianOf(win);
    const mad = Math.max(medianOf(win.map((x) => Math.abs(x - med))), 0.15);
    const thresh = Math.max(1.5, sigmaK * 1.4826 * mad);
    if (Math.abs(usd[i]! - med) > thresh) {
      out[i] = { ...out[i]!, usd: med };
    }
  }
  return out;
}

/** Remove single-point and two-point blips relative to interpolated neighbor baseline. */
function spikePass(pts: RunPnlPt[], devMultiplier: number): RunPnlPt[] {
  if (pts.length < 3) return pts;
  const n = pts.length;
  const out = pts.map((p) => ({ ...p }));
  for (let i = 1; i < n - 1; i++) {
    const a = out[i - 1]!;
    const b = out[i]!;
    const c = out[i + 1]!;
    const tSpan = c.t - a.t;
    const expected = tSpan > 1e-9 ? a.usd + ((b.t - a.t) / tSpan) * (c.usd - a.usd) : (a.usd + c.usd) / 2;
    const dev = Math.abs(b.usd - expected);
    const neighborDelta = Math.abs(c.usd - a.usd);
    if (dev >= 1.2 && (dev > neighborDelta * devMultiplier + 1.0)) {
      out[i] = { t: b.t, usd: expected };
    }
  }
  return out;
}

/** EMA forward pass. */
function ema(pts: RunPnlPt[], alpha: number): RunPnlPt[] {
  if (pts.length <= 1) return pts.map((p) => ({ ...p }));
  const out: RunPnlPt[] = [{ ...pts[0] }];
  for (let i = 1; i < pts.length; i++) {
    out.push({ t: pts[i].t, usd: alpha * pts[i].usd + (1 - alpha) * out[i - 1].usd });
  }
  return out;
}

/** EMA backward pass (forward + reverse to remove phase lag). */
function zeroPhaseEma(pts: RunPnlPt[], alpha: number): RunPnlPt[] {
  const fwd = ema(pts, alpha);
  const rev = ema([...fwd].reverse(), alpha).reverse();
  return rev;
}

/**
 * Pro broadcast smoothing pipeline.
 * Much more aggressive than the base chart pipeline for clean TV-ready curves.
 */
export function smoothRunPnlForProChart(points: RunPnlPt[]): RunPnlPt[] {
  if (points.length === 0) return points;
  if (points.length <= RAW_TAIL + 2) return points;

  // Separate the last point so "Now" value stays accurate
  const head = points.slice(0, -RAW_TAIL);
  const tail = points.slice(-RAW_TAIL);

  let p = head;

  // 1. Wide rolling median (window 7) — kills multi-point spikes
  p = rollingMedian(p, 7);
  // 2. Second pass with window 5 — smooths remaining jaggedness
  p = rollingMedian(p, 5);
  // 3. Hampel with tight sigma (2.0) — catches outliers the median missed
  p = hampel(p, 3, 2.0);
  // 4. Spike removal passes with tight multiplier (0.9)
  p = spikePass(p, 0.9);
  p = spikePass(p, 0.9);
  p = spikePass(p, 0.9);
  // 5. Zero-phase EMA — smooths without phase shift (no lag)
  p = zeroPhaseEma(p, EMA_ALPHA);
  // 6. Final spike cleanup after EMA
  p = spikePass(p, 1.1);

  return [...p, ...tail];
}
