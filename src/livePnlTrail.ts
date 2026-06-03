/**
 * Fix B (issue: the open-position PnL chart appears frozen during server pnl_path gaps).
 *
 * The engine resets a position's pnl_path to [] on every position change (window rollover /
 * re-entry). When that happens for an open position, the UI used to fall back to a *frozen*
 * retained snapshot, so the inner graph looked stuck even though the live % kept moving.
 *
 * This helper grows a client-side trail from the live unrealized %, so the chart keeps moving
 * whenever the % moves — throttled (so we don't add a point on every 500ms poll) and capped
 * (bounded memory). The server's own pnl_path remains authoritative when present; this only
 * fills the gaps.
 */
export type TrailPoint = { ts: number; upnl_pct: number };

export function growLiveTrail(
  prev: TrailPoint[],
  livePct: number,
  nowSec: number,
  opts?: { minIntervalSec?: number; maxLen?: number },
): TrailPoint[] {
  if (!Number.isFinite(livePct)) return prev;
  const minIntervalSec = opts?.minIntervalSec ?? 1.0;
  const maxLen = opts?.maxLen ?? 600;
  const last = prev[prev.length - 1];
  // Throttle: skip if the last point is newer than the min interval (return same ref — no churn).
  if (last && nowSec - last.ts < minIntervalSec) return prev;
  const grown = [...prev, { ts: nowSec, upnl_pct: livePct }];
  return grown.length > maxLen ? grown.slice(-maxLen) : grown;
}
