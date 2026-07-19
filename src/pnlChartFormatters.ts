import { israelTime } from "./timeFormat";

/** PnL chart axis/tooltip time — HH:MM:SS in Israel time (see timeFormat.ts). */
export function formatPnlAxisTime(tsSec: number): string {
  return israelTime(tsSec);
}

export function formatPctAxisTick(v: number): string {
  if (!Number.isFinite(v)) return "";
  const a = Math.abs(v);
  const decimals = a >= 100 || a === 0 ? 0 : a >= 20 ? 0 : 1;
  return `${v.toFixed(decimals)}%`;
}
