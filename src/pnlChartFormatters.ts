export function formatPnlAxisTime(tsSec: number): string {
  return new Date(tsSec * 1000).toLocaleTimeString("he-IL", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatPctAxisTick(v: number): string {
  if (!Number.isFinite(v)) return "";
  const a = Math.abs(v);
  const decimals = a >= 100 || a === 0 ? 0 : a >= 20 ? 0 : 1;
  return `${v.toFixed(decimals)}%`;
}
