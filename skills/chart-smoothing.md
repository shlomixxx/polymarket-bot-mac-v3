# Chart Smoothing Skill

Guidelines for tuning the P&L chart smoothing pipeline in this project.

## Existing Pipeline (`src/runPnlSmoothing.ts`)

The standard smoothing applies these passes in order:
1. **Rolling Median** (window 5) x2 — removes single-sample spikes
2. **Hampel Filter** (k=3, scale=1.4826) — statistical outlier detection
3. **Endpoint Smoothing** — stabilizes the first/last few points
4. **3x Spike Detection Passes** — iterative outlier removal
5. **Last-point Snap** — ensures the final value matches reality

## Enhanced Pipeline (`src/runPnlSmoothingPro.ts`)

For broadcast/pro layouts, an additional EMA pass is applied:

```typescript
function exponentialMovingAverage(pts: RunPnlPt[], alpha: number): RunPnlPt[] {
  const out = [{ ...pts[0] }];
  for (let i = 1; i < pts.length; i++) {
    const smoothed = alpha * pts[i].usd + (1 - alpha) * out[i - 1].usd;
    out.push({ t: pts[i].t, usd: smoothed });
  }
  return out;
}
```

## EMA Alpha Tuning

- **alpha = 0.2**: Very smooth, noticeable lag — good for long sessions (1h+)
- **alpha = 0.3**: Balanced smoothness with acceptable lag (default for pro)
- **alpha = 0.5**: Mild smoothing, near-realtime — good for short windows
- **alpha = 0.8**: Almost no smoothing — only removes micro-jitter

## Preserving Tail Accuracy

The last 3 points are kept raw (not EMA'd) so the "Now" value on the chart matches the actual `runPnlUsd`. This prevents the chart from appearing "behind" reality.

## Chart Visual Settings for Professional Look

- Line `strokeWidth`: 3 (bolder than default 2.75)
- Curve type: `"natural"` (smoother splines vs `"monotone"`)
- Grid: `strokeOpacity={0.3}`, `strokeDasharray="2 8"` (subtle)
- Reference lines: `strokeOpacity={0.55}` for high/low guides
