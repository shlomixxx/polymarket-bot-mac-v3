/**
 * windowStats — pure helpers for BTC up/down window outcomes.
 * Shared by the Strategy strip, the Stats-tab windows card, and the per-trade panel,
 * so every circle in the app derives from one source of truth. No React, no I/O.
 */

/** A closed 5m/15m window as returned by /api/history/recent. */
export type RecentWindow = {
  epoch: number;
  slug?: string | null;
  side_won: string | null; // "Up" | "Down" | null
  btc_open?: number | null;
  btc_close?: number | null;
  ts_recorded?: number | null;
};

/** A row of /api/history/hourly. */
export type HourlyBucket = { hour: number; total: number; up_wins: number; up_rate: number };

export type WindowOutcome = "up" | "down" | "unknown";

/** A window as consumed by the circle UI. */
export type CircleDatum = {
  epoch: number;
  outcome: WindowOutcome;
  /** the trade's own window (highlight ring) */
  isFocus?: boolean;
  /** the bot's bet for this window (only known for a focus trade) */
  betSide?: "Up" | "Down";
  /** whether the bot won (only known for a focus trade) */
  won?: boolean;
};

const UP_RGB = [74, 155, 126] as const; // --up  #4a9b7e
const DOWN_RGB = [184, 92, 92] as const; // --down #b85c5c

export function outcomeOf(w: RecentWindow): WindowOutcome {
  if (w.side_won === "Up") return "up";
  if (w.side_won === "Down") return "down";
  return "unknown";
}

/** Mirror of windowSecForTrade (App.tsx): 15m → 900, 5m → 300, else 300. */
export function windowSecForSlug(slug?: string | null): number {
  const s = slug ?? "";
  if (s.includes("15m")) return 900;
  if (s.includes("5m")) return 300;
  return 300;
}

/** BTC open→close drift in $ and %. Null when a price is missing or open is 0. */
export function driftOf(w: RecentWindow): { abs: number | null; pct: number | null } {
  const o = w.btc_open;
  const c = w.btc_close;
  if (o == null || c == null || !Number.isFinite(o) || !Number.isFinite(c) || o === 0) {
    return { abs: null, pct: null };
  }
  const abs = c - o;
  return { abs, pct: (abs / o) * 100 };
}

/** Wall-clock HH:MM:SS (he-IL), or "—" for an invalid/zero epoch. */
export function clockHms(unixSec: number): string {
  if (!Number.isFinite(unixSec) || unixSec <= 0) return "—";
  return new Date(unixSec * 1000).toLocaleTimeString("he-IL", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Aggregate stats derived purely from the windows array. Order-independent (sorts by epoch). */
export function deriveWindowStats(windows: RecentWindow[]) {
  const sorted = windows
    .filter((w) => Number.isFinite(w.epoch))
    .slice()
    .sort((a, b) => a.epoch - b.epoch);

  // known (up/down) outcomes, oldest → newest
  const known: Array<"up" | "down"> = [];
  for (const w of sorted) {
    const o = outcomeOf(w);
    if (o === "up" || o === "down") known.push(o);
  }

  const upCount = known.filter((o) => o === "up").length;
  const downCount = known.length - upCount;
  const knownN = known.length;
  const unknownN = sorted.length - knownN;
  const upRate = knownN > 0 ? upCount / knownN : null;

  // current streak: identical outcomes from the newest backwards
  let currentStreak = 0;
  let currentStreakDir: "up" | "down" | null = null;
  for (let i = known.length - 1; i >= 0; i--) {
    if (currentStreakDir === null) {
      currentStreakDir = known[i];
      currentStreak = 1;
    } else if (known[i] === currentStreakDir) {
      currentStreak++;
    } else {
      break;
    }
  }

  // longest streak anywhere
  let longestStreak = 0;
  let run = 0;
  let prev: "up" | "down" | null = null;
  for (const o of known) {
    run = o === prev ? run + 1 : 1;
    if (run > longestStreak) longestStreak = run;
    prev = o;
  }

  // alternations = adjacent differing pairs (chop signal)
  let alternations = 0;
  for (let i = 1; i < known.length; i++) {
    if (known[i] !== known[i - 1]) alternations++;
  }
  const maxAlternations = Math.max(0, known.length - 1);
  const chopScore = maxAlternations > 0 ? alternations / maxAlternations : 0;

  return {
    total: sorted.length,
    knownN,
    unknownN,
    upCount,
    downCount,
    upRate,
    currentStreak,
    currentStreakDir,
    longestStreak,
    alternations,
    maxAlternations,
    chopScore,
  };
}

/** Green↔red tint for a heatmap cell given up_rate 0..1 (manual RGB lerp — Safari-safe). */
export function upRateTint(upRate: number, total: number): { bg: string; fg: string } {
  if (total === 0) return { bg: "var(--card-hover)", fg: "var(--muted)" };
  const mix = UP_RGB.map((u, i) => Math.round(DOWN_RGB[i] + (u - DOWN_RGB[i]) * upRate));
  // stronger tint the further up_rate sits from a coin-flip
  const alpha = Math.min(0.7, 0.2 + 0.5 * Math.abs(upRate - 0.5) * 2);
  return { bg: `rgba(${mix[0]},${mix[1]},${mix[2]},${alpha.toFixed(2)})`, fg: "var(--text)" };
}

/**
 * Derive a trade's own window circle + a surrounding context strip.
 * The trade's OWN outcome comes from its settlement prices (source of truth) —
 * it works even when the trade predates the recent-windows buffer.
 */
export function deriveTradeWindowView(args: {
  epoch: number | null;
  windowSec: number;
  btcStart?: number | null;
  btcEnd?: number | null;
  side?: string;
  resolvedOutcome?: string;
  settleWon?: boolean;
  recentWindows: RecentWindow[];
  contextRadius?: number;
}): {
  focus: CircleDatum | null;
  timeStart: string;
  timeEnd: string;
  driftUsd: number | null;
  driftPct: number | null;
  strip: CircleDatum[];
} {
  const {
    epoch,
    windowSec,
    btcStart,
    btcEnd,
    side,
    resolvedOutcome,
    settleWon,
    recentWindows,
    contextRadius = 3,
  } = args;

  const hasBtc =
    btcStart != null && Number.isFinite(btcStart) && btcEnd != null && Number.isFinite(btcEnd);

  const outcome: WindowOutcome = hasBtc
    ? (btcEnd as number) >= (btcStart as number)
      ? "up"
      : "down"
    : resolvedOutcome === "Up"
    ? "up"
    : resolvedOutcome === "Down"
    ? "down"
    : "unknown";

  const betSide = side === "Up" || side === "Down" ? side : undefined;
  const won =
    typeof settleWon === "boolean"
      ? settleWon
      : betSide && outcome !== "unknown"
      ? (betSide === "Up" ? "up" : "down") === outcome
      : undefined;

  const driftUsd = hasBtc ? (btcEnd as number) - (btcStart as number) : null;
  const driftPct =
    hasBtc && (btcStart as number) !== 0
      ? (((btcEnd as number) - (btcStart as number)) / (btcStart as number)) * 100
      : null;

  const timeStart = epoch != null ? clockHms(epoch) : "—";
  const timeEnd = epoch != null ? clockHms(epoch + windowSec) : "—";

  const focus: CircleDatum | null =
    epoch != null ? { epoch, outcome, isFocus: true, betSide, won } : null;

  const sorted = recentWindows
    .filter((w) => Number.isFinite(w.epoch))
    .slice()
    .sort((a, b) => a.epoch - b.epoch);
  const toDatum = (w: RecentWindow): CircleDatum => ({
    epoch: w.epoch,
    outcome: outcomeOf(w),
    isFocus: epoch != null && w.epoch === epoch,
  });

  let strip: CircleDatum[] = [];
  const focusIdx = epoch != null ? sorted.findIndex((w) => w.epoch === epoch) : -1;
  if (focusIdx >= 0) {
    strip = sorted.slice(Math.max(0, focusIdx - contextRadius), focusIdx + contextRadius + 1).map(toDatum);
  } else {
    strip = sorted.slice(-(contextRadius * 2 + 1)).map(toDatum);
  }

  return { focus, timeStart, timeEnd, driftUsd, driftPct, strip };
}
