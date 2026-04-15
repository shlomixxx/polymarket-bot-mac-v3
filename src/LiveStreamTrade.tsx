import type { ReactNode, RefObject } from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { StreamSpectatorLayout } from "./StreamSpectatorLayout";
import { StreamDashboardLayout } from "./StreamDashboardLayout";
import { StreamProLayout } from "./StreamProLayout";
import { StreamLiveBroadcastLayout } from "./StreamLiveBroadcastLayout";
import { playEntryChime, playExitChime, resumeStreamAudio } from "./streamAudio";
import type { StreamViewerLayout } from "./streamViewerTypes";
import { smoothRunPnlForChart } from "./runPnlSmoothing";
import { smoothRunPnlForProChart } from "./runPnlSmoothingPro";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, isPageHidden } from "./api";

async function safeApi<T>(path: string): Promise<T | null> {
  try {
    return await api<T>(path);
  } catch {
    return null;
  }
}

type Market = {
  title: string;
  window_sec?: number;
  btc_window?: string;
  seconds_left: number;
  /** From engine — used to reset window countdown anchor on new market window */
  epoch?: number;
  slug?: string;
};

type StrategyConfigSlice = {
  /** מאז הפעלת semi/auto — זמן ריצה + baseline ל-PnL בשידור */
  bot_run_started_ts?: number | null;
  bot_run_equity_baseline_usd?: number | null;
  /** גיבוי לזמן/בסיס (מנוע / איפוס) כשאין עדיין bot_run — רק כשהמצב פעיל */
  ui_runtime_started_ts?: number;
  ui_runtime_equity_baseline_usd?: number;
  bot_run_win_rate_pct?: number | null;
  bot_run_exit_trades_n?: number;
  bot_run_wins_n?: number;
  mode?: string;
  min_minutes_for_entry?: number;
  freeze_last_minutes?: number;
  intermediate_block_new_entries?: boolean;
  strategy_status_key?: string;
  side_preference?: string;
};

type DemoLeg = {
  side?: string;
  token_id?: string;
  contracts?: number;
  avg_cost?: number;
  unrealized_pct?: number;
  leg_unrealized?: number;
  pnl_path?: { ts: number; upnl_pct: number }[];
};

type DemoState = {
  balance_usd?: number;
  ui_runtime_equity_baseline_usd?: number;
  bot_run_started_ts?: number | null;
  bot_run_equity_baseline_usd?: number | null;
  bot_run_win_rate_pct?: number | null;
  bot_run_exit_trades_n?: number;
  bot_run_wins_n?: number;
  trades?: {
    ts?: number;
    type?: string;
    token_id?: string;
    realized_pnl?: number;
    settled_epoch?: number;
    epoch?: number;
    settled_window_sec?: number;
    window_sec?: number;
    /** Up / Down — כיוון הפוזיציה ביציאה */
    side?: string;
  }[];
  positions?: {
    side: string;
    contracts: number;
    avg_cost: number;
    token_id: string;
  }[];
  last_mark?: {
    equity?: number;
    unrealized_usd?: number;
    legs?: DemoLeg[];
  };
  /** דגימות (unix sec, equity usd) מהמנוע — נשמר בשרת, מאפשר לשחזר גרף run P&L אחרי רענון דף */
  equity_history?: [number, number][] | number[][];
};

type OrderbookSummary = {
  slug?: string;
  up: { bid: number | null; ask: number | null; mid: number | null };
  down: { bid: number | null; ask: number | null; mid: number | null };
  degraded?: boolean;
};

function windowLabel(m: Market | null): string {
  if (!m) return "—";
  const w = m.btc_window;
  if (w === "5m" || w === "15m") return w;
  const sec = m.window_sec;
  if (sec === 300) return "5m";
  if (sec === 900) return "15m";
  if (typeof sec === "number" && sec > 0) return `${Math.round(sec / 60)}m`;
  return "—";
}

function formatTimeLeft(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Elapsed wall time since bot mode was enabled (semi/auto). */
function formatBotUptime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m ${String(s).padStart(2, "0")}s`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

type PnlRow = { t: number; pct: number };

function pnlSessionExtremes(rows: PnlRow[]): { maxPct: number; minPct: number; maxIdx: number; minIdx: number } | null {
  if (!rows.length) return null;
  let maxIdx = 0;
  let minIdx = 0;
  let maxPct = rows[0].pct;
  let minPct = rows[0].pct;
  for (let i = 1; i < rows.length; i++) {
    const p = rows[i].pct;
    if (p > maxPct) {
      maxPct = p;
      maxIdx = i;
    }
    if (p < minPct) {
      minPct = p;
      minIdx = i;
    }
  }
  return { maxPct, minPct, maxIdx, minIdx };
}

type ChartIdleCopy = { headline: string; sub: string; showSpinner: boolean };

/** Stream-friendly copy: fun for viewers, not dry strategy jargon. */
function resolveChartIdleCopy(p: {
  mode?: string;
  pending: { action?: string } | null;
  statusKey: string;
  secondsLeft: number | undefined;
  minMinutesForEntry: number;
  freezeLastMinutes: number;
  intermediateBlock: boolean;
  sidePreference?: string;
}): ChartIdleCopy {
  const mode = p.mode;
  if (mode === "off") {
    return {
      headline: "Bot's on standby",
      sub: "Flip semi or auto — then the hunt begins.",
      showSpinner: false,
    };
  }

  if (p.pending?.action === "buy") {
    return {
      headline: "Your move, captain",
      sub: "Entry lined up — approve to go live.",
      showSpinner: true,
    };
  }
  if (p.pending?.action === "hedge") {
    return {
      headline: "Hedge on deck",
      sub: "Needs your OK to balance the book.",
      showSpinner: true,
    };
  }

  const key = p.statusKey || "";
  const sl = p.secondsLeft;
  const minLeft = typeof sl === "number" && Number.isFinite(sl) ? sl / 60 : null;
  const fr = p.freezeLastMinutes;
  const mm = p.minMinutesForEntry;

  if (key === "freeze" || (minLeft != null && minLeft <= fr + 1e-6)) {
    return {
      headline: "Final stretch of this window",
      sub: "Next round loads right after the bell — stay tuned.",
      showSpinner: true,
    };
  }
  if (
    key === "intermediate" ||
    (p.intermediateBlock && minLeft != null && minLeft < mm && minLeft > fr)
  ) {
    return {
      headline: "Hunting A+ setups only",
      sub: "Skipping noisy entries here — we want odds that feel unfair (in our favor).",
      showSpinner: true,
    };
  }
  if (key === "reenter_cooldown") {
    return {
      headline: "Cooldown after a win",
      sub: "Catching breath — next entry unlocks in a moment.",
      showSpinner: true,
    };
  }
  if (key === "reenter_disabled") {
    return {
      headline: "Waiting for a fresh window",
      sub: "Re-entry after TP is off — next chapter starts soon.",
      showSpinner: true,
    };
  }
  if (key.startsWith("book_missing")) {
    return {
      headline: "Syncing with the order book",
      sub: "Up/Down quotes are on the way…",
      showSpinner: true,
    };
  }
  if (key.startsWith("limit_")) {
    return {
      headline: "Safety guardrail tripped",
      sub: "Entries pause — protecting the stack.",
      showSpinner: true,
    };
  }
  if (key === "hedge_book_missing" || key === "hedge_exchange_min") {
    return {
      headline: "Lining up the hedge",
      sub: "Almost there — book or size is catching up.",
      showSpinner: true,
    };
  }

  if (p.sidePreference === "signal") {
    return {
      headline: "Signal hunt: Up vs Down",
      sub: "When the model picks a side, we move — patience pays.",
      showSpinner: true,
    };
  }

  return {
    headline: "Scanning for an edge",
    sub: "No open trade yet — we're stalking a fat pitch, not swinging at junk.",
    showSpinner: true,
  };
}

function ChartIdlePanel({ copy }: { copy: ChartIdleCopy }) {
  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 20,
        padding: "16px 28px",
        textAlign: "center",
        maxWidth: 460,
        margin: "0 auto",
      }}
    >
      {copy.showSpinner ? <div className="stream-idle-spinner" aria-hidden /> : null}
      <div>
        <div
          style={{
            fontSize: 20,
            fontWeight: 750,
            lineHeight: 1.35,
            color: "var(--text)",
            letterSpacing: "-0.02em",
          }}
        >
          {copy.headline}
        </div>
        <div style={{ fontSize: 14, color: "var(--muted)", marginTop: 10, lineHeight: 1.55 }}>{copy.sub}</div>
      </div>
    </div>
  );
}

/** Mid price 0–1 → cents label */
function pxToCentsLabel(px: number | null | undefined): string {
  if (px == null || !Number.isFinite(Number(px))) return "—";
  return `${(Number(px) * 100).toFixed(1)}¢`;
}

function formatUsdSigned(v: number): string {
  const a = Math.abs(v);
  return `${v >= 0 ? "+" : "-"}$${a.toFixed(2)}`;
}

function toFiniteNumber(v: unknown): number | null {
  if (v == null) return null;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** Min/max after trimming tails — softens a single bad sample so chart scale & ref lines don’t stick on a spike. */
function trimmedUsdMinMax(usds: number[], trimFraction = 0.06): { lo: number; hi: number } {
  if (usds.length === 0) return { lo: 0, hi: 1 };
  if (usds.length < 10) {
    return { lo: Math.min(...usds), hi: Math.max(...usds) };
  }
  const s = [...usds].sort((a, b) => a - b);
  const trim = Math.max(1, Math.floor(s.length * trimFraction));
  const slice = s.slice(trim, s.length - trim);
  if (slice.length === 0) return { lo: s[0], hi: s[s.length - 1] };
  return { lo: Math.min(...slice), hi: Math.max(...slice) };
}

/** הודעת שידור קצרה לפי עסקת היציאה האחרונה (לפי ts). */
function describeStreamExit(trades: DemoState["trades"]): string {
  if (!trades?.length) return "✅ Position closed — flat until next entry.";
  const sorted = [...trades].sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0));
  const t = String(sorted[0]?.type ?? "");
  if (t === "SELL_TP") return "🎯 Take profit hit — bot exited. Flat until next entry.";
  if (t === "SETTLE_WIN") return "🏆 Settlement (win) — position closed. Waiting for next setup.";
  if (t === "SETTLE_LOSS") return "📉 Settlement (loss) — position closed. Waiting for next setup.";
  if (t === "SETTLE_UNKNOWN") return "🧾 Market settled — position closed. Waiting for next setup.";
  if (t === "EXPIRE_0" || t.includes("EXPIRE")) return "⏱️ Position expired — flat until next entry.";
  if (t.startsWith("SELL")) return "💨 Sell exit — flat until next entry.";
  return "✅ Position closed — flat until next entry.";
}

type RoundOutcome = {
  id: string;
  startLabel: string;
  endLabel: string;
  win: boolean;
  pnlUsd: number | null;
  side: "Up" | "Down" | null;
};

function tradeExitSide(t: { side?: unknown }): "Up" | "Down" | null {
  const s = String(t.side ?? "").trim();
  if (s === "Up") return "Up";
  if (s === "Down") return "Down";
  return null;
}

/** Per-exit history for spectator overlay — win/loss only, no dollar amounts. Newest first; one row per exit (multiple rows in the same clock minute if multiple trades). */
function buildRoundOutcomes(
  trades: DemoState["trades"] | undefined,
  maxItems: number,
  /** רק יציאות מאז תחילת ריצת הבוט (epoch sec), כמו win rate בשרת — בלי יציאות ישנות מה־DB */
  minExitTsSec: number | null,
): RoundOutcome[] {
  if (!trades?.length) return [];
  const exits = trades.filter((t) => {
    const typ = String(t.type ?? "");
    if (t.realized_pnl == null || Number.isNaN(Number(t.realized_pnl))) return false;
    if (
      !(
        typ === "EXPIRE_0" ||
        typ === "SETTLE_WIN" ||
        typ === "SETTLE_LOSS" ||
        typ === "SETTLE_UNKNOWN" ||
        typ === "SELL_TP" ||
        typ.startsWith("SELL")
      )
    ) {
      return false;
    }
    const tsSec = Number(t.ts);
    const fallbackEpoch = t.settled_epoch ?? t.epoch;
    const sec = Number.isFinite(tsSec) ? tsSec : Number(fallbackEpoch);
    if (!Number.isFinite(sec)) return false;
    if (minExitTsSec != null && sec < minExitTsSec) return false;
    return true;
  });
  const sorted = [...exits].sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0));
  const out: RoundOutcome[] = [];
  const fmt = (d: Date) =>
    d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  /** פירוק בסוף חלון — לעיתים ה-ts מגיע שניה אחרי הקצה; מציגים דקה עגולה (שניות 00). */
  const exitTimeLabel = (sec: number, typ: string) => {
    const d = new Date(sec * 1000);
    const isWindowEndSettlement =
      typ === "SETTLE_WIN" || typ === "SETTLE_LOSS" || typ === "SETTLE_UNKNOWN";
    if (isWindowEndSettlement) {
      const floored = new Date(d);
      floored.setSeconds(0, 0);
      return fmt(floored);
    }
    return fmt(d);
  };
  let idx = 0;
  for (const t of sorted) {
    const tsSec = Number(t.ts);
    const fallbackEpoch = t.settled_epoch ?? t.epoch;
    const sec = Number.isFinite(tsSec) ? tsSec : Number(fallbackEpoch);
    if (!Number.isFinite(sec)) continue;
    const win = Number(t.realized_pnl) > 0;
    const typ = String(t.type ?? "");
    const timeLabel = exitTimeLabel(sec, typ);
    const id = `${sec}-${idx}-${typ}`;
    idx += 1;
    const pnlRaw = Number(t.realized_pnl);
    const pnlUsd = Number.isFinite(pnlRaw) ? pnlRaw : null;
    const side = tradeExitSide(t);
    out.push({ id, startLabel: timeLabel, endLabel: timeLabel, win, pnlUsd, side });
    if (out.length >= maxItems) break;
  }
  return out;
}

type StreamMood = {
  label: string;
  hint: string;
  variant: "hunt" | "trade" | "cool" | "gold" | "freeze" | "standby";
};

/** מצב גדול לשידור — מסונכן עם לוגיקת idle של הגרף. */
function resolveStreamMood(p: {
  mode?: string;
  open: boolean;
  pending: { action?: string } | null;
  statusKey: string;
  secondsLeft: number | undefined;
  minMinutesForEntry: number;
  freezeLastMinutes: number;
  intermediateBlock: boolean;
  sidePreference?: string;
}): StreamMood {
  if (p.mode === "off") {
    return { label: "STANDBY", hint: "Bot off — enable semi/auto to run", variant: "standby" };
  }
  if (p.open) {
    return { label: "IN TRADE", hint: "Live position — risk on", variant: "trade" };
  }
  if (p.pending?.action === "buy") {
    return { label: "YOUR MOVE", hint: "Entry ready — approve to go live", variant: "gold" };
  }
  if (p.pending?.action === "hedge") {
    return { label: "HEDGE READY", hint: "Approve to balance the book", variant: "gold" };
  }
  const key = p.statusKey || "";
  const sl = p.secondsLeft;
  const minLeft = typeof sl === "number" && Number.isFinite(sl) ? sl / 60 : null;
  const fr = p.freezeLastMinutes;
  const mm = p.minMinutesForEntry;

  if (key === "freeze" || (minLeft != null && minLeft <= fr + 1e-6)) {
    return { label: "FINAL STRETCH", hint: "Last minutes of this window", variant: "freeze" };
  }
  if (key === "intermediate" || (p.intermediateBlock && minLeft != null && minLeft < mm && minLeft > fr)) {
    return { label: "HUNTING", hint: "A+ setups only — no junk entries", variant: "hunt" };
  }
  if (key === "reenter_cooldown") {
    return { label: "BREATHING", hint: "Cooldown after a win", variant: "cool" };
  }
  if (key === "reenter_disabled") {
    return { label: "RESET", hint: "Waiting for a fresh window", variant: "cool" };
  }
  if (key.startsWith("book_missing")) {
    return { label: "SYNCING", hint: "Order book loading", variant: "cool" };
  }
  if (key.startsWith("limit_")) {
    return { label: "PAUSED", hint: "Safety guardrail tripped", variant: "freeze" };
  }
  if (key === "hedge_book_missing" || key === "hedge_exchange_min") {
    return { label: "HEDGE SETUP", hint: "Lining up the hedge", variant: "gold" };
  }
  if (p.sidePreference === "signal") {
    return { label: "HUNTING", hint: "Signal mode — stalking Up vs Down", variant: "hunt" };
  }
  return { label: "HUNTING", hint: "Scanning for the next edge", variant: "hunt" };
}

function streamMoodAccent(v: StreamMood["variant"]): { border: string; color: string; bg: string; shadow: string } {
  switch (v) {
    case "trade":
      return {
        border: "rgba(52, 211, 153, 0.55)",
        color: "#6ee7b7",
        bg: "linear-gradient(145deg, rgba(52, 211, 153, 0.14), rgba(15, 23, 42, 0.92))",
        shadow: "0 0 44px rgba(52, 211, 153, 0.28)",
      };
    case "hunt":
      return {
        border: "rgba(129, 140, 248, 0.5)",
        color: "#a5b4fc",
        bg: "linear-gradient(145deg, rgba(99, 102, 241, 0.14), rgba(15, 23, 42, 0.92))",
        shadow: "0 0 36px rgba(129, 140, 248, 0.22)",
      };
    case "gold":
      return {
        border: "rgba(251, 191, 36, 0.55)",
        color: "#fbbf24",
        bg: "linear-gradient(145deg, rgba(251, 191, 36, 0.12), rgba(15, 23, 42, 0.92))",
        shadow: "0 0 40px rgba(251, 191, 36, 0.3)",
      };
    case "freeze":
      return {
        border: "rgba(251, 146, 60, 0.5)",
        color: "#fb923c",
        bg: "linear-gradient(145deg, rgba(251, 146, 60, 0.12), rgba(15, 23, 42, 0.92))",
        shadow: "0 0 32px rgba(251, 146, 60, 0.22)",
      };
    case "standby":
      return {
        border: "rgba(148, 163, 184, 0.4)",
        color: "var(--muted)",
        bg: "var(--bg-elevated)",
        shadow: "none",
      };
    default:
      return {
        border: "rgba(148, 163, 184, 0.45)",
        color: "var(--text-secondary)",
        bg: "linear-gradient(145deg, rgba(52, 211, 153, 0.06), rgba(15, 23, 42, 0.9))",
        shadow: "0 0 24px rgba(52, 211, 153, 0.12)",
      };
  }
}

/** Baseline equity ל-PnL בשידור: קודם bot_run, אחרת ui_runtime (מצב לא off). */
function pickBotRunEquityBaseline(cfg: StrategyConfigSlice | null, demo: DemoState | null): number | null {
  const br = toFiniteNumber(cfg?.bot_run_equity_baseline_usd ?? demo?.bot_run_equity_baseline_usd);
  if (br != null) return br;
  if (cfg?.mode === "off") return null;
  return toFiniteNumber(cfg?.ui_runtime_equity_baseline_usd ?? demo?.ui_runtime_equity_baseline_usd);
}

const RUN_PNL_SERIES_MAX_POINTS = 1800;

function downsampleRunPnlPoints(pts: { t: number; usd: number }[], maxN: number): { t: number; usd: number }[] {
  if (pts.length <= maxN) return pts;
  const n = pts.length;
  const out: { t: number; usd: number }[] = [];
  for (let k = 0; k < maxN; k++) {
    const idx = Math.min(n - 1, Math.round((k / Math.max(1, maxN - 1)) * (n - 1)));
    out.push(pts[idx]);
  }
  out[out.length - 1] = { ...pts[n - 1] };
  return out;
}

/** בונה סדרת run P&L מהיסטוריית equity בשרת — אותה לוגיקה כמו runPnlUsd (baseline מול bot run). */
function buildRunPnlSeriesFromEquityHistory(
  cfg: StrategyConfigSlice | null,
  demo: DemoState | null,
): { t: number; usd: number }[] {
  if (cfg?.mode === "off") return [];
  const base = pickBotRunEquityBaseline(cfg, demo);
  const t0 = toFiniteNumber(cfg?.bot_run_started_ts ?? demo?.bot_run_started_ts);
  if (base == null || t0 == null) return [];
  const hist = demo?.equity_history;
  if (!hist?.length) return [];
  const pts: { t: number; usd: number }[] = [];
  for (const row of hist) {
    if (!Array.isArray(row) || row.length < 2) continue;
    const ts = Number(row[0]);
    const eq = Number(row[1]);
    if (!Number.isFinite(ts) || !Number.isFinite(eq)) continue;
    if (ts < t0 - 1e-6) continue;
    pts.push({ t: ts, usd: eq - base });
  }
  if (pts.length === 0) return [];
  pts.sort((a, b) => a.t - b.t);
  const deduped: { t: number; usd: number }[] = [];
  for (const p of pts) {
    const last = deduped[deduped.length - 1];
    if (last && Math.abs(last.t - p.t) < 1e-6) {
      deduped[deduped.length - 1] = p;
    } else {
      deduped.push(p);
    }
  }
  return downsampleRunPnlPoints(deduped, RUN_PNL_SERIES_MAX_POINTS);
}

function aggregateEntry(positions: NonNullable<DemoState["positions"]>) {
  let contracts = 0;
  let costSum = 0;
  for (const p of positions) {
    const c = Number(p.contracts) || 0;
    contracts += c;
    costSum += c * (Number(p.avg_cost) || 0);
  }
  const avgUsd = contracts > 0 ? costSum / contracts : 0;
  return {
    contracts,
    avgEntryCents: avgUsd * 100,
    side: (positions[0]?.side === "Down" ? "Down" : "Up") as "Up" | "Down",
    tokenIds: positions.map((p) => p.token_id),
  };
}

export type { StreamViewerLayout } from "./streamViewerTypes";

/** עטיפה לשידור: התוכן מוקטן ב־scale כדי להיכנס ל־100dvh בלי גלילה (מופעל עם ?fit=1). */
function BroadcastFit(props: {
  enabled: boolean;
  parentRef: RefObject<HTMLDivElement>;
  contentRef: RefObject<HTMLDivElement>;
  children: ReactNode;
}) {
  const { enabled, parentRef, contentRef, children } = props;
  if (!enabled) return <>{children}</>;
  return (
    <div
      ref={parentRef}
      style={{ flex: 1, minHeight: 0, overflow: "hidden", position: "relative", width: "100%" }}
    >
      <div
        ref={contentRef}
        style={{ position: "absolute", top: 0, left: 0, right: 0, width: "100%", transformOrigin: "top center" }}
      >
        {children}
      </div>
    </div>
  );
}

type LivePortfolio = {
  ok: boolean;
  balance_usd: number | null;
  equity_usd: number | null;
  positions: {
    token_id: string;
    side: string;
    size: number;
    avg_price: number | null;
    mark_price: number | null;
    value_usd: number | null;
  }[];
  address: string | null;
  funder_address?: string | null;
  is_proxy?: boolean;
  ts: number | null;
  error?: string;
  hint?: string;
};

export default function LiveStreamTrade({ layout = "classic" }: { layout?: StreamViewerLayout }) {
  const [market, setMarket] = useState<Market | null>(null);
  const [demo, setDemo] = useState<DemoState | null>(null);
  const [stratCfg, setStratCfg] = useState<StrategyConfigSlice | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{ action?: string } | null>(null);
  const [orderbook, setOrderbook] = useState<OrderbookSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [liveModeEffective, setLiveModeEffective] = useState(false);
  const [livePortfolio, setLivePortfolio] = useState<LivePortfolio | null>(null);
  const [clock, setClock] = useState(0);
  const [exitBanner, setExitBanner] = useState<string | null>(null);
  const [showUsd, setShowUsd] = useState(() =>
    typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("usd") !== "0" : true
  );
  const [entrySoundOn, setEntrySoundOn] = useState(() =>
    typeof window !== "undefined" ? localStorage.getItem("streamEntrySound") !== "0" : true
  );
  const [pnlTickDelta, setPnlTickDelta] = useState(0);
  /** Sampled run P&L ($) — spectator overlay only (client-side series). */
  const [runPnlSeries, setRunPnlSeries] = useState<{ t: number; usd: number }[]>([]);
  const [audioUnlocked, setAudioUnlocked] = useState(false);
  const prevLivePctRef = useRef<number | null>(null);
  const prevEquityRef = useRef<number | null>(null);
  const equitySpikeCountRef = useRef<number>(0);

  /** ניסיון ראשון (למשל Electron עם autoplayPolicy) — אחרת נדרשת מחווה. */
  useEffect(() => {
    void resumeStreamAudio().then((ok) => {
      if (ok) setAudioUnlocked(true);
    });
  }, []);

  /** דפדפנים רגילים חוסמים אודיו עד מחוות — פותחים את AudioContext בלחיצה ראשונה. */
  useEffect(() => {
    const unlock = () => {
      void resumeStreamAudio().then((ok) => {
        if (ok) setAudioUnlocked(true);
      });
    };
    window.addEventListener("pointerdown", unlock, { once: true, capture: true });
    return () => window.removeEventListener("pointerdown", unlock, { capture: true });
  }, []);

  /** מונע יישום snapshot ישן כש־refresh נקרא שוב לפני שהבקשה הקודמת הסתיימה (גורם לקפיצות PnL / equity). */
  const refreshGeneration = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++refreshGeneration.current;
    try {
      setErr(null);
      const [m, st, cfg, pend, ob, lm] = await Promise.all([
        api<Market>("/api/market/current"),
        api<DemoState>("/api/demo/state"),
        api<StrategyConfigSlice>("/api/strategy/config"),
        api<{ pending: { action?: string } | null }>("/api/strategy/pending"),
        safeApi<OrderbookSummary>("/api/market/orderbook-summary"),
        safeApi<{ effective?: boolean }>("/api/live/mode"),
      ]);
      if (gen !== refreshGeneration.current) return;
      setMarket(m);
      setDemo(st);
      setStratCfg(cfg);
      setPendingApproval(pend?.pending ?? null);
      setOrderbook(ob);
      setLiveModeEffective(Boolean(lm?.effective));
    } catch (e) {
      if (gen !== refreshGeneration.current) return;
      const msg = e instanceof Error ? e.message : String(e);
      if (e instanceof DOMException && e.name === "AbortError") {
        setErr("Server slow — retrying…");
      } else if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) {
        setErr("Connection lost — retrying…");
      } else {
        setErr(msg);
      }
    }
  }, []);

  const fitBroadcast = useMemo(() => {
    if (typeof window === "undefined") return false;
    const q = new URLSearchParams(window.location.search);
    return q.get("fit") === "1" || q.get("broadcast") === "1";
  }, []);

  const broadcastParentRef = useRef<HTMLDivElement>(null);
  const broadcastContentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!fitBroadcast) return;
    const html = document.documentElement;
    const body = document.body;
    const root = document.getElementById("root");
    const prev = {
      htmlOverflow: html.style.overflow,
      bodyOverflow: body.style.overflow,
      htmlHeight: html.style.height,
      bodyMinHeight: body.style.minHeight,
      rootHeight: root?.style.height ?? "",
      rootOverflow: root?.style.overflow ?? "",
    };
    html.style.overflow = "hidden";
    body.style.overflow = "hidden";
    html.style.height = "100%";
    body.style.minHeight = "100%";
    if (root) {
      root.style.height = "100%";
      root.style.overflow = "hidden";
    }
    return () => {
      html.style.overflow = prev.htmlOverflow;
      body.style.overflow = prev.bodyOverflow;
      html.style.height = prev.htmlHeight;
      body.style.minHeight = prev.bodyMinHeight;
      if (root) {
        root.style.height = prev.rootHeight;
        root.style.overflow = prev.rootOverflow;
      }
    };
  }, [fitBroadcast]);

  useLayoutEffect(() => {
    if (!fitBroadcast) return;
    const parent = broadcastParentRef.current;
    const content = broadcastContentRef.current;
    if (!parent || !content) return;
    const update = () => {
      const ph = parent.clientHeight;
      if (ph < 4) return;
      const ch = content.scrollHeight;
      if (ch < 1) return;
      const s = Math.min(1, (ph * 0.992) / ch);
      content.style.transform = s >= 0.998 ? "none" : `scale(${s})`;
      content.style.transformOrigin = "top center";
    };
    update();
    const ro = new ResizeObserver(() => requestAnimationFrame(update));
    ro.observe(parent);
    ro.observe(content);
    window.addEventListener("resize", update);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [fitBroadcast]);

  useEffect(() => {
    document.documentElement.lang = "en";
    document.documentElement.dir = "ltr";
    const base =
      layout === "broadcast"
        ? "Live trade — cinematic broadcast"
        : layout === "pro"
          ? "Live trade — pro broadcast"
          : layout === "spectator-v2"
            ? "Live trade — spectator v2"
            : layout === "dashboard"
              ? "Live trade — dashboard"
              : layout === "spectator"
                ? "Live trade — spectator overlay"
                : layout === "showcase"
                  ? "Live trade — viewer showcase"
                  : "Live trade — stream";
    document.title = fitBroadcast ? `${base} (fit)` : base;
    return () => {
      document.documentElement.lang = "he";
      document.documentElement.dir = "rtl";
    };
  }, [layout, fitBroadcast]);

  useEffect(() => {
    let cancelled = false;
    void refresh();
    const id = window.setInterval(() => {
      if (!cancelled && !isPageHidden()) void refresh();
    }, 2000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [refresh]);

  /** Fast 500ms snapshot poll — רק balance + last_mark + positions לעדכון P&L מהיר */
  useEffect(() => {
    let cancelled = false;
    const pollSnapshot = async () => {
      if (cancelled || isPageHidden()) return;
      try {
        const snap = await api<DemoState>("/api/demo/snapshot");
        if (!cancelled) {
          setDemo((prev) =>
            prev
              ? {
                  ...prev,
                  balance_usd: snap.balance_usd,
                  positions: snap.positions ?? prev.positions,
                  last_mark: snap.last_mark ?? prev.last_mark,
                  bot_run_started_ts: snap.bot_run_started_ts,
                  bot_run_equity_baseline_usd: snap.bot_run_equity_baseline_usd,
                  ui_runtime_equity_baseline_usd: snap.ui_runtime_equity_baseline_usd,
                }
              : snap,
          );
        }
      } catch {
        // silent — full refresh will recover
      }
    };
    const id = window.setInterval(pollSnapshot, 500);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => setClock((c) => c + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  /** Polling תיק חי מ-Polymarket: רק כש-live mode effective */
  useEffect(() => {
    if (!liveModeEffective) {
      setLivePortfolio(null);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      if (isPageHidden()) return;
      const p = await safeApi<LivePortfolio>("/api/live/portfolio");
      if (!cancelled && p) setLivePortfolio(p);
    };
    void poll();
    const id = window.setInterval(poll, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [liveModeEffective]);

  /** סלקטור: בלייב — פוזיציות אמיתיות; בדמו — פוזיציות הספר הפנימי. */
  const isLive = liveModeEffective && livePortfolio?.ok === true;

  const positions = useMemo(() => {
    if (isLive && livePortfolio?.positions?.length) {
      return livePortfolio.positions.map((p) => ({
        side: p.side,
        contracts: p.size,
        avg_cost: p.avg_price ?? 0,
        token_id: p.token_id,
      }));
    }
    return demo?.positions ?? [];
  }, [isLive, livePortfolio?.positions, demo?.positions]);

  const open = positions.length > 0;

  /** חתימת פוזיציה — לא מסתמך רק על open: אם יש סגירה+כניסה באותו tick, open נשאר true ואז אין צלילים. */
  const positionSig = useMemo(() => {
    const ps = demo?.positions ?? [];
    if (!ps.length) return "";
    return [...ps]
      .map((p) => String(p.token_id ?? ""))
      .filter(Boolean)
      .sort()
      .join("|");
  }, [demo?.positions]);

  const demoTradesRef = useRef(demo?.trades);
  demoTradesRef.current = demo?.trades;
  const exitBannerTimerRef = useRef<number | null>(null);
  const prevPositionSigRef = useRef<string | null>(null);

  useEffect(() => {
    const clearBannerTimer = () => {
      if (exitBannerTimerRef.current != null) {
        window.clearTimeout(exitBannerTimerRef.current);
        exitBannerTimerRef.current = null;
      }
    };
    if (prevPositionSigRef.current === null) {
      prevPositionSigRef.current = positionSig;
      return;
    }
    const prev = prevPositionSigRef.current;
    const next = positionSig;
    if (prev === next) return;

    const wasOpen = prev !== "";
    const isOpen = next !== "";

    if (wasOpen && !isOpen) {
      clearBannerTimer();
      setExitBanner(describeStreamExit(demoTradesRef.current));
      if (entrySoundOn) playExitChime();
      exitBannerTimerRef.current = window.setTimeout(() => {
        exitBannerTimerRef.current = null;
        setExitBanner(null);
      }, 10_000);
      prevPositionSigRef.current = next;
      return;
    }
    if (!wasOpen && isOpen) {
      clearBannerTimer();
      setExitBanner(null);
      if (entrySoundOn) playEntryChime();
      prevPositionSigRef.current = next;
      return;
    }
    if (wasOpen && isOpen && prev !== next) {
      clearBannerTimer();
      setExitBanner(describeStreamExit(demoTradesRef.current));
      if (entrySoundOn) {
        playExitChime();
        window.setTimeout(() => {
          if (entrySoundOn) playEntryChime();
        }, 240);
      }
      exitBannerTimerRef.current = window.setTimeout(() => {
        exitBannerTimerRef.current = null;
        setExitBanner(null);
      }, 10_000);
      prevPositionSigRef.current = next;
      return;
    }
    prevPositionSigRef.current = next;
  }, [positionSig, entrySoundOn]);

  const agg = open ? aggregateEntry(positions) : null;

  const leg = useMemo(() => {
    const legs = demo?.last_mark?.legs ?? [];
    if (!agg?.tokenIds.length) return null;
    for (const tid of agg.tokenIds) {
      const found = legs.find((l) => l.token_id === tid);
      if (found) return found;
    }
    return legs[0] ?? null;
  }, [demo?.last_mark?.legs, agg?.tokenIds]);

  const totalCostUsd = useMemo(() => {
    if (!positions.length) return 0;
    const FEE = 1.002;
    return positions.reduce((s, p) => s + (Number(p.contracts) || 0) * (Number(p.avg_cost) || 0) * FEE, 0);
  }, [positions]);

  const livePct = useMemo(() => {
    if (!open) return null;
    if (positions.length === 1 && typeof leg?.unrealized_pct === "number" && Number.isFinite(leg.unrealized_pct)) {
      return leg.unrealized_pct;
    }
    const u = demo?.last_mark?.unrealized_usd;
    if (typeof u === "number" && totalCostUsd > 0) return (u / totalCostUsd) * 100;
    if (typeof leg?.unrealized_pct === "number" && Number.isFinite(leg.unrealized_pct)) return leg.unrealized_pct;
    return null;
  }, [open, positions.length, leg, demo?.last_mark?.unrealized_usd, totalCostUsd]);

  const liveUsd = useMemo(() => {
    if (!open) return null;
    if (isLive && livePortfolio?.positions?.length) {
      // חישוב unrealized מול פוזיציות אמיתיות (value - cost)
      let total = 0;
      for (const p of livePortfolio.positions) {
        const v = p.value_usd ?? 0;
        const cost = (p.avg_price ?? 0) * p.size;
        total += v - cost;
      }
      return total;
    }
    if (typeof demo?.last_mark?.unrealized_usd === "number") return demo.last_mark.unrealized_usd;
    return null;
  }, [open, isLive, livePortfolio?.positions, demo?.last_mark?.unrealized_usd]);

  useEffect(() => {
    if (livePct == null) {
      prevLivePctRef.current = null;
      return;
    }
    const prev = prevLivePctRef.current;
    prevLivePctRef.current = livePct;
    if (prev == null) return;
    setPnlTickDelta(Math.abs(livePct - prev));
  }, [livePct]);

  /** מיושר ל־DemoEngine.equity_snapshot_usd — מניעת קפיצות כש־last_mark נשאר עם equity ישן בזמן throttle.
   *  בלייב: שווי נטו אמיתי מ-Polymarket; בדמו: equity מהספר הפנימי.
   */
  const equityNowRaw = useMemo(() => {
    if (isLive) {
      const liveEq = livePortfolio?.equity_usd ?? livePortfolio?.balance_usd;
      if (typeof liveEq === "number" && Number.isFinite(liveEq) && liveEq >= 0) return liveEq;
    }
    const bal = Number(demo?.balance_usd ?? 0);
    const safeBal = Number.isFinite(bal) ? bal : 0;
    const eq = demo?.last_mark?.equity;
    if (typeof eq === "number" && Number.isFinite(eq) && eq >= 0) return eq;
    return safeBal;
  }, [isLive, livePortfolio?.equity_usd, livePortfolio?.balance_usd, demo?.last_mark?.equity, demo?.balance_usd]);

  /** סינון ספיקים: אם הערך החדש קופץ ביותר מ-15% מהערך הקודם — מתעלמים ממנו.
   *  אם הערך הגדול נמשך 2+ פולים ברצף — מקבלים אותו (שינוי אמיתי). */
  const equityNow = (() => {
    const prev = prevEquityRef.current;
    if (prev !== null && prev > 0) {
      const changePct = Math.abs(equityNowRaw - prev) / prev;
      if (changePct > 0.15) {
        equitySpikeCountRef.current += 1;
        // אם הספייק נמשך 2+ פולים ברצף — כנראה שינוי אמיתי, מקבלים אותו
        if (equitySpikeCountRef.current < 2) {
          return prev;
        }
      } else {
        equitySpikeCountRef.current = 0;
      }
    }
    prevEquityRef.current = equityNowRaw;
    return equityNowRaw;
  })();

  const equityBaselineUsd = useMemo(() => pickBotRunEquityBaseline(stratCfg, demo), [stratCfg, demo]);

  /** Baseline נפרד ללייב — equityNow בלייב מגיע מ-Polymarket CLOB (USDC אמיתי), אבל השרת
   *  לוכד baseline מ-demo.equity_snapshot בתחילת bot_run. התוצאה: runPnl=equityNow_live - baseline_demo
   *  חסר כל משמעות. במקום זה: שומרים את ה-equity הראשונה שנצפתה מ-livePortfolio אחרי תחילת bot_run,
   *  ומשתמשים בה כ-baseline לחישוב ה-PnL בלייב. מתאפס עם כל מעבר off→semi/auto (כש-bot_run_started_ts משתנה).
   */
  const [liveBaselineUsd, setLiveBaselineUsd] = useState<number | null>(null);
  const liveBaselineKeyRef = useRef<string>("");
  useEffect(() => {
    if (!isLive || stratCfg?.mode === "off") {
      setLiveBaselineUsd(null);
      liveBaselineKeyRef.current = "";
      return;
    }
    const key = String(stratCfg?.bot_run_started_ts ?? "");
    if (!key || key === "null") return;
    const eq = livePortfolio?.equity_usd ?? livePortfolio?.balance_usd;
    if (typeof eq !== "number" || !Number.isFinite(eq) || eq < 0) return;
    if (key !== liveBaselineKeyRef.current) {
      liveBaselineKeyRef.current = key;
      setLiveBaselineUsd(eq);
    }
  }, [isLive, livePortfolio?.equity_usd, livePortfolio?.balance_usd, stratCfg?.bot_run_started_ts, stratCfg?.mode]);

  const runPnlUsd = useMemo(() => {
    if (stratCfg?.mode === "off") return null;
    if (isLive) {
      if (liveBaselineUsd == null) return null;
      return equityNow - liveBaselineUsd;
    }
    const base = equityBaselineUsd;
    if (base == null) return null;
    return equityNow - base;
  }, [stratCfg?.mode, isLive, liveBaselineUsd, equityBaselineUsd, equityNow]);

  /** Wall-clock since enabling semi/auto; fallback ל-ui_runtime אם אין עדיין bot_run בשרת. */
  const botRunUptimeSec = useMemo(() => {
    void clock;
    if (stratCfg?.mode === "off") return null;
    const t0 =
      toFiniteNumber(stratCfg?.bot_run_started_ts ?? demo?.bot_run_started_ts) ??
      toFiniteNumber(stratCfg?.ui_runtime_started_ts);
    if (t0 == null) return null;
    return Math.max(0, Date.now() / 1000 - t0);
  }, [stratCfg?.mode, stratCfg?.bot_run_started_ts, demo?.bot_run_started_ts, stratCfg?.ui_runtime_started_ts, clock]);

  const winRatePct = stratCfg?.bot_run_win_rate_pct ?? demo?.bot_run_win_rate_pct ?? null;
  const winRateExits = stratCfg?.bot_run_exit_trades_n ?? demo?.bot_run_exit_trades_n ?? 0;
  const winRateWins = stratCfg?.bot_run_wins_n ?? demo?.bot_run_wins_n ?? 0;
  const winRateHot = winRatePct != null && winRatePct > 90;

  const windowTotalSec = useMemo(() => {
    if (!market) return 300;
    if (typeof market.window_sec === "number" && market.window_sec > 0) return market.window_sec;
    if (market.btc_window === "15m") return 900;
    return 300;
  }, [market]);

  /** Server sends seconds_left only on refresh (~2s); anchor + clock tick so UI counts down every second. */
  const windowSecondsLeftAnchorRef = useRef<{ left: number; atMs: number } | null>(null);
  useEffect(() => {
    if (!market) {
      windowSecondsLeftAnchorRef.current = null;
      return;
    }
    windowSecondsLeftAnchorRef.current = {
      left: market.seconds_left,
      atMs: Date.now(),
    };
  }, [market?.seconds_left, market?.epoch, market?.slug]);

  const effectiveWindowSecondsLeft = useMemo(() => {
    void clock;
    if (!market) return null;
    const a = windowSecondsLeftAnchorRef.current;
    if (!a) return market.seconds_left;
    const driftSec = Math.floor((Date.now() - a.atMs) / 1000);
    return Math.max(0, a.left - driftSec);
  }, [market, clock]);

  const windowProgressPct = useMemo(() => {
    if (!market || effectiveWindowSecondsLeft == null) return 0;
    const s = Math.max(0, effectiveWindowSecondsLeft);
    return windowTotalSec > 0 ? Math.min(100, (s / windowTotalSec) * 100) : 0;
  }, [market, windowTotalSec, effectiveWindowSecondsLeft]);

  /** % of market window elapsed (spectator overlay). */
  const windowElapsedPct = useMemo(() => {
    if (!market || windowTotalSec <= 0 || effectiveWindowSecondsLeft == null) return 0;
    const left = Math.max(0, effectiveWindowSecondsLeft);
    const elapsed = windowTotalSec - left;
    return Math.min(100, Math.max(0, (elapsed / windowTotalSec) * 100));
  }, [market, windowTotalSec, effectiveWindowSecondsLeft]);

  const streamMood = useMemo(
    () =>
      resolveStreamMood({
        mode: stratCfg?.mode,
        open,
        pending: pendingApproval,
        statusKey: stratCfg?.strategy_status_key ?? "",
        secondsLeft: effectiveWindowSecondsLeft ?? undefined,
        minMinutesForEntry: Number(stratCfg?.min_minutes_for_entry ?? 3),
        freezeLastMinutes: Number(stratCfg?.freeze_last_minutes ?? 1),
        intermediateBlock: !!stratCfg?.intermediate_block_new_entries,
        sidePreference: stratCfg?.side_preference,
      }),
    [
      stratCfg?.mode,
      stratCfg?.strategy_status_key,
      stratCfg?.min_minutes_for_entry,
      stratCfg?.freeze_last_minutes,
      stratCfg?.intermediate_block_new_entries,
      stratCfg?.side_preference,
      open,
      pendingApproval,
      effectiveWindowSecondsLeft,
    ]
  );

  const botRunSessionKey = `${stratCfg?.bot_run_started_ts ?? ""}|${demo?.bot_run_started_ts ?? ""}`;
  const botRunSessionRef = useRef<string>("");
  const isRunPnlLayout = layout === "spectator" || layout === "pro" || layout === "broadcast";

  /** סשן בוט השתנה — מאתחלים מהיסטוריה בשרת (לא רק state ריק) כדי שהגרף ישרוד רענון דף */
  useEffect(() => {
    if (!isRunPnlLayout) return;
    if (botRunSessionKey !== botRunSessionRef.current) {
      botRunSessionRef.current = botRunSessionKey;
      setRunPnlSeries(buildRunPnlSeriesFromEquityHistory(stratCfg, demo));
    }
  }, [botRunSessionKey, layout, stratCfg, demo]);

  /** אם אחרי רענון עדיין ריק אבל המנוע כבר שלח equity_history — ממלא פעם אחת */
  useEffect(() => {
    if (!isRunPnlLayout) return;
    if (stratCfg?.mode === "off") return;
    setRunPnlSeries((prev) => {
      if (prev.length > 0) return prev;
      const seeded = buildRunPnlSeriesFromEquityHistory(stratCfg, demo);
      return seeded.length > 0 ? seeded : prev;
    });
  }, [layout, stratCfg?.mode, stratCfg, demo]);

  useEffect(() => {
    void clock;
    if (!isRunPnlLayout) return;
    if (stratCfg?.mode === "off") {
      setRunPnlSeries([]);
      return;
    }
    const usd = runPnlUsd;
    if (usd == null || !Number.isFinite(usd)) return;
    const now = Date.now() / 1000;
    setRunPnlSeries((prev) => {
      const last = prev[prev.length - 1];
      if (last && Math.abs(last.usd - usd) < 1e-9 && now - last.t < 0.9) return prev;
      const next = [...prev, { t: now, usd }];
      return next.length > RUN_PNL_SERIES_MAX_POINTS ? next.slice(-RUN_PNL_SERIES_MAX_POINTS) : next;
    });
  }, [runPnlUsd, stratCfg?.mode, clock, layout]);

  /** סדרה לתצוגה בלבד — מסיר ספיקים בודדים שלא משקפים מגמה אמיתית */
  const runPnlSeriesDisplay = useMemo(() => {
    if (!isRunPnlLayout || !runPnlSeries.length) return runPnlSeries;
    if (layout === "pro" || layout === "broadcast") return smoothRunPnlForProChart(runPnlSeries);
    return smoothRunPnlForChart(runPnlSeries);
  }, [runPnlSeries, layout, isRunPnlLayout]);

  const runUsdYDomain = useMemo((): [number, number] => {
    if (!isRunPnlLayout || !runPnlSeriesDisplay.length) return [0, 1];
    const vals = runPnlSeriesDisplay.map((x) => x.usd);
    const { lo, hi } = trimmedUsdMinMax(vals);
    const mid = (lo + hi) / 2;
    const span = Math.max(hi - lo, 0);
    const minSpan = Math.max(6, Math.abs(mid) * 0.025, 1);
    if (span < minSpan) {
      const h = minSpan / 2;
      return [mid - h, mid + h];
    }
    const pad = Math.max(span * 0.1, 2);
    return [lo - pad, hi + pad];
  }, [runPnlSeriesDisplay, layout]);

  /** Ref lines only — trimmed extrema so one glitch doesn’t keep the yellow/red guides forever. */
  const runUsdChartRefExtremes = useMemo(() => {
    if (!isRunPnlLayout || !runPnlSeriesDisplay.length) return null;
    const vals = runPnlSeriesDisplay.map((x) => x.usd);
    const { lo, hi } = trimmedUsdMinMax(vals);
    return { minUsd: lo, maxUsd: hi };
  }, [runPnlSeriesDisplay, layout]);

  const roundOutcomes = useMemo(() => {
    if (!isRunPnlLayout) return [];
    if (stratCfg?.mode === "off") return [];
    const minTs = toFiniteNumber(stratCfg?.bot_run_started_ts ?? demo?.bot_run_started_ts);
    if (minTs == null) return [];
    return buildRoundOutcomes(demo?.trades, 120, minTs);
  }, [demo?.trades, layout, stratCfg?.mode, stratCfg?.bot_run_started_ts, demo?.bot_run_started_ts]);

  const runUsdSessionStats = useMemo(() => {
    if (!isRunPnlLayout || !runPnlSeriesDisplay.length) return null;
    let maxUsd = runPnlSeriesDisplay[0].usd;
    let minUsd = runPnlSeriesDisplay[0].usd;
    for (const p of runPnlSeriesDisplay) {
      if (p.usd > maxUsd) maxUsd = p.usd;
      if (p.usd < minUsd) minUsd = p.usd;
    }
    const lastFromSeries = runPnlSeriesDisplay[runPnlSeriesDisplay.length - 1].usd;
    const last =
      runPnlUsd != null && Number.isFinite(runPnlUsd) ? runPnlUsd : lastFromSeries;
    return { maxUsd, minUsd, last };
  }, [runPnlSeriesDisplay, layout, runPnlUsd]);

  const streamPulseSec = useMemo(() => {
    if (!open || livePct == null) return 3.15;
    return Math.max(0.72, 3.55 - Math.min(pnlTickDelta * 6.5, 2.88));
  }, [open, livePct, pnlTickDelta]);

  const pulseRingRgb =
    livePct == null ? "148, 163, 184" : livePct >= 0 ? "52, 211, 153" : "251, 113, 133";

  const showHotStreak = stratCfg?.mode !== "off" && winRateExits >= 5 && winRatePct != null && winRatePct >= 72;

  const moodStyle = useMemo(() => streamMoodAccent(streamMood.variant), [streamMood.variant]);

  const runPnlColor =
    runPnlUsd == null ? "var(--muted)" : runPnlUsd >= 0 ? "var(--up)" : "var(--down)";

  const [stablePnlPath, setStablePnlPath] = useState<{ ts: number; upnl_pct: number }[] | null>(null);

  useEffect(() => {
    if (!open) {
      setStablePnlPath(null);
      return;
    }
    const next = leg?.pnl_path;
    if (!next?.length) return;

    setStablePnlPath((prev) => {
      if (!prev?.length) return next;
      const prevLast = prev[prev.length - 1];
      const nextLast = next[next.length - 1];
      if (prev.length === next.length && prevLast?.ts === nextLast?.ts) return prev;
      return next;
    });
  }, [open, leg?.pnl_path]);

  const chartRows: PnlRow[] = useMemo(() => {
    const livePath = leg?.pnl_path;
    const path = livePath?.length ? livePath : open ? stablePnlPath : null;
    if (!path?.length) return [];
    return path.map((p) => ({
      t: p.ts,
      pct: p.upnl_pct,
    }));
  }, [leg?.pnl_path, open, stablePnlPath]);

  const chartExtremes = useMemo(() => pnlSessionExtremes(chartRows), [chartRows]);
  const pnlYDomain = useMemo((): [number, number] => {
    if (!chartRows.length || !chartExtremes) return [0, 1];
    const { maxPct, minPct } = chartExtremes;
    const span = Math.max(1e-6, maxPct - minPct);
    const pad = Math.max(1, span * 0.1, 0.6);
    return [minPct - pad, maxPct + pad];
  }, [chartRows, chartExtremes]);

  const pnlColor =
    livePct == null ? "var(--muted)" : livePct >= 0 ? "var(--up)" : "var(--down)";
  const lineColor = chartRows.length ? pnlColor : "var(--muted)";
  const chartTintHex =
    chartRows.length === 0 ? "#64748b" : livePct != null && livePct >= 0 ? "#34d399" : "#fb7185";

  const chartIdleCopy = useMemo(() => {
    if (open) return null;
    return resolveChartIdleCopy({
      mode: stratCfg?.mode,
      pending: pendingApproval,
      statusKey: stratCfg?.strategy_status_key ?? "",
      secondsLeft: effectiveWindowSecondsLeft ?? undefined,
      minMinutesForEntry: Number(stratCfg?.min_minutes_for_entry ?? 3),
      freezeLastMinutes: Number(stratCfg?.freeze_last_minutes ?? 1),
      intermediateBlock: !!stratCfg?.intermediate_block_new_entries,
      sidePreference: stratCfg?.side_preference,
    });
  }, [open, stratCfg, effectiveWindowSecondsLeft, pendingApproval]);

  /** Recharts passes `key: "dot-N"` on props; custom dot functions must put it on the returned node. */
  const renderPnlDot = useCallback(
    (props: { cx?: number; cy?: number; index?: number; key?: string | number }) => {
      const { cx = 0, cy = 0, index = -1, key: rechartsKey } = props;
      const dotKey = rechartsKey != null ? String(rechartsKey) : `pnl-dot-${index}-${cx}-${cy}`;
      const n = chartRows.length;
      if (!chartExtremes || n < 1) return <g key={dotKey} />;
      const { maxIdx, minIdx } = chartExtremes;
      const last = n - 1;
      const isLast = index === last;
      const samePoint = maxIdx === minIdx;
      const showPeak = !samePoint && index === maxIdx;
      const showTrough = !samePoint && index === minIdx;
      if (isLast) {
        return (
          <g key={dotKey}>
            <circle key={`${dotKey}-ring`} cx={cx} cy={cy} r={11} fill="none" stroke={lineColor} strokeWidth={2} opacity={0.9} />
            <circle key={`${dotKey}-core`} cx={cx} cy={cy} r={5} fill="#f8fafc" stroke={lineColor} strokeWidth={2} />
          </g>
        );
      }
      if (showPeak) {
        const s = 7;
        return (
          <polygon
            key={dotKey}
            points={`${cx},${cy - s} ${cx - s},${cy + s * 0.55} ${cx + s},${cy + s * 0.55}`}
            fill="#fbbf24"
            stroke="#fffbeb"
            strokeWidth={1.2}
          />
        );
      }
      if (showTrough) {
        const s = 7;
        return (
          <polygon
            key={dotKey}
            points={`${cx},${cy + s} ${cx - s},${cy - s * 0.55} ${cx + s},${cy - s * 0.55}`}
            fill="#f87171"
            stroke="#fef2f2"
            strokeWidth={1.2}
          />
        );
      }
      return <g key={dotKey} />;
    },
    [chartExtremes, chartRows.length, lineColor]
  );

  const isBroadcast = layout === "broadcast";
  const isPro = layout === "pro";
  const isSpectatorV2 = layout === "spectator-v2";
  const isDashboard = layout === "dashboard";
  const isSpectator = layout === "spectator";
  const isShowcase = layout === "showcase";

  const spectatorProps = {
    fitBroadcast,
    broadcastParentRef,
    broadcastContentRef,
    err,
    exitBanner,
    market,
    stratCfg,
    orderbook,
    open,
    agg,
    livePct,
    pnlColor,
    entrySoundOn,
    setEntrySoundOn,
    audioUnlocked,
    setAudioUnlocked,
    streamMood,
    moodStyle,
    showHotStreak,
    winRatePct,
    winRateExits,
    winRateWins,
    winRateHot,
    runPnlUsd,
    runPnlSeries: runPnlSeriesDisplay,
    runUsdYDomain,
    runUsdSessionStats,
    runUsdChartRefExtremes,
    botRunUptimeSec,
    windowSecondsLeftDisplay: effectiveWindowSecondsLeft,
    windowElapsedPct,
    roundOutcomes,
    streamPulseSec,
    pulseRingRgb,
    chartIdleCopy,
    isLive,
    liveAccountUsd: isLive
      ? (typeof livePortfolio?.equity_usd === "number"
          ? livePortfolio.equity_usd
          : (typeof livePortfolio?.balance_usd === "number" ? livePortfolio.balance_usd : null))
      : null,
    demoBalanceUsd: typeof demo?.balance_usd === "number" ? demo.balance_usd : null,
  };

  if (isBroadcast) {
    return <StreamLiveBroadcastLayout {...spectatorProps} />;
  }

  if (isPro) {
    return <StreamProLayout {...spectatorProps} />;
  }

  if (isSpectatorV2) {
    return <StreamSpectatorLayout {...spectatorProps} variant="v2" />;
  }

  if (isDashboard) {
    return <StreamDashboardLayout {...spectatorProps} />;
  }

  if (isSpectator) {
    return <StreamSpectatorLayout {...spectatorProps} />;
  }

  return (
    <div
      className={`stream-trade-root ${isShowcase ? "stream-trade-root--showcase" : "stream-trade-root--classic"}${fitBroadcast ? " stream-broadcast-fit" : ""}`}
      style={{
        boxSizing: "border-box",
        ...(fitBroadcast
          ? {
              height: "100dvh",
              maxHeight: "100dvh",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
              padding: isShowcase ? "10px 18px 12px" : "8px 16px 10px",
            }
          : {
              minHeight: "100vh",
              padding: isShowcase ? "28px 36px 40px" : "22px 24px 36px",
            }),
        background: isShowcase
          ? "radial-gradient(ellipse 900px 420px at 50% -15%, rgba(52, 211, 153, 0.09), transparent 52%), radial-gradient(ellipse 600px 380px at 100% 40%, rgba(129, 140, 248, 0.06), transparent 45%), var(--bg)"
          : "var(--bg)",
        color: "var(--text)",
        fontFamily: "var(--font-display)",
        maxWidth: isShowcase ? 1000 : 860,
        margin: "0 auto",
        borderLeft: isShowcase ? "1px solid rgba(52, 211, 153, 0.18)" : "none",
        borderRight: isShowcase ? "1px solid rgba(52, 211, 153, 0.18)" : "none",
      }}
    >
      <style>
        {`
          @keyframes streamWinrateGold {
            0%, 100% { filter: brightness(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.45); }
            50% { filter: brightness(1.12); box-shadow: 0 0 28px 4px rgba(251, 191, 36, 0.55); }
          }
          .stream-winrate-gold {
            animation: streamWinrateGold 1.1s ease-in-out infinite;
            border-color: rgba(251, 191, 36, 0.75) !important;
            background: linear-gradient(135deg, rgba(251, 191, 36, 0.12), rgba(15, 23, 42, 0.95)) !important;
          }
          @keyframes streamLiveDot {
            0%, 100% {
              transform: scale(1);
              box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.65), 0 0 8px rgba(52, 211, 153, 0.5);
              opacity: 1;
            }
            50% {
              transform: scale(1.2);
              box-shadow: 0 0 0 6px rgba(52, 211, 153, 0), 0 0 16px rgba(52, 211, 153, 0.65);
              opacity: 0.95;
            }
          }
          .stream-live-dot {
            width: 11px;
            height: 11px;
            border-radius: 999px;
            background: linear-gradient(145deg, #6ee7b7, #22c55e);
            flex-shrink: 0;
            animation: streamLiveDot 1.4s ease-in-out infinite;
          }
          @keyframes streamIdleSpin {
            to { transform: rotate(360deg); }
          }
          .stream-idle-spinner {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            border: 3px solid rgba(110, 231, 183, 0.12);
            border-top-color: #6ee7b7;
            border-right-color: rgba(147, 169, 201, 0.45);
            animation: streamIdleSpin 0.72s linear infinite;
            box-shadow: 0 0 18px rgba(52, 211, 153, 0.2);
          }
          .stream-idle-spinner--sm {
            width: 34px;
            height: 34px;
            border-width: 2px;
          }
          @keyframes streamPulseRing {
            0%, 100% { transform: scale(1); opacity: 0.38; }
            50% { transform: scale(1.07); opacity: 0.92; }
          }
          .stream-pulse-orb {
            position: relative;
            width: 164px;
            height: 164px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            flex-shrink: 0;
          }
          .stream-pulse-orb::before {
            content: "";
            position: absolute;
            inset: -14px;
            border-radius: 50%;
            border: 2px solid rgba(var(--pulse-rgb), 0.52);
            animation: streamPulseRing var(--pulse-sec, 2.8s) ease-in-out infinite;
            pointer-events: none;
          }
          .stream-window-bar {
            height: 8px;
            border-radius: 999px;
            background: rgba(148, 163, 184, 0.2);
            overflow: hidden;
            margin-top: 10px;
          }
          .stream-window-bar-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, rgba(52, 211, 153, 0.35), #34d399);
            transition: width 0.6s ease-out;
          }
          .stream-broadcast-fit footer {
            display: none;
          }
        `}
      </style>
      <header
        style={{
          marginBottom: fitBroadcast ? 10 : 28,
          flexShrink: 0,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 16,
        }}
      >
        <div>
          <div
            role="status"
            aria-label="Live market data"
            style={{
              fontSize: 13,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
            }}
          >
            <span className="stream-live-dot" title="Online — live updates" />
            <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>Market</span>
            <span
              style={{
                fontSize: 10,
                fontWeight: 800,
                letterSpacing: "0.18em",
                color: "#6ee7b7",
                padding: "4px 9px",
                borderRadius: 999,
                background: "linear-gradient(135deg, rgba(52, 211, 153, 0.2), rgba(22, 101, 52, 0.35))",
                border: "1px solid rgba(52, 211, 153, 0.5)",
                boxShadow: "0 0 14px rgba(52, 211, 153, 0.35), inset 0 1px 0 rgba(255,255,255,0.12)",
              }}
            >
              LIVE
            </span>
            {isLive ? (
              <span
                style={{
                  fontSize: 10,
                  fontWeight: 800,
                  letterSpacing: "0.14em",
                  color: "#fbbf24",
                  padding: "4px 9px",
                  borderRadius: 999,
                  border: "1px solid rgba(251, 191, 36, 0.5)",
                  background: "linear-gradient(135deg, rgba(251, 191, 36, 0.12), rgba(15, 23, 42, 0.9))",
                  boxShadow: "0 0 10px rgba(251, 191, 36, 0.15)",
                }}
                title="Balance, positions & equity from Polymarket (real money)"
              >
                REAL MONEY
              </span>
            ) : null}
            {!isShowcase ? (
              <span
                style={{
                  fontSize: 10,
                  fontWeight: 800,
                  letterSpacing: "0.14em",
                  color: "var(--muted)",
                  padding: "4px 9px",
                  borderRadius: 999,
                  border: "1px solid rgba(148, 163, 184, 0.45)",
                  background: "rgba(15, 23, 42, 0.85)",
                }}
              >
                COMPACT
              </span>
            ) : null}
            {isShowcase ? (
              <span
                title="Experimental viewer layout — compare with ?stream=1"
                style={{
                  fontSize: 10,
                  fontWeight: 800,
                  letterSpacing: "0.12em",
                  color: "#fbbf24",
                  padding: "4px 9px",
                  borderRadius: 999,
                  border: "1px solid rgba(251, 191, 36, 0.5)",
                  background: "linear-gradient(135deg, rgba(251, 191, 36, 0.12), rgba(15, 23, 42, 0.9))",
                  boxShadow: "0 0 14px rgba(251, 191, 36, 0.2)",
                }}
              >
                VIEWER SHOWCASE
              </span>
            ) : null}
          </div>
          <h1 style={{ margin: "8px 0 0", fontSize: isShowcase ? 23 : 20, fontWeight: 600, lineHeight: 1.25 }}>
            {market?.title ?? "Loading…"}
          </h1>
          <p
            style={{
              margin: "8px 0 0",
              fontSize: 13,
              color: "var(--muted)",
              fontWeight: 500,
              maxWidth: 52 * 16,
              lineHeight: 1.45,
              display: fitBroadcast ? "none" : undefined,
            }}
          >
            {isShowcase
              ? "Full viewer layout — bot mood, pulse ring & market window bar."
              : "Compact layout for OBS — stat grid, prices & chart (no hero strip)."}
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 8 }}>
          <label style={{ fontSize: 13, color: "var(--muted)", display: "flex", alignItems: "center", gap: 8, cursor: "pointer", userSelect: "none" }}>
            <input type="checkbox" checked={showUsd} onChange={(e) => setShowUsd(e.target.checked)} />
            PnL $
          </label>
          <label style={{ fontSize: 13, color: "var(--muted)", display: "flex", alignItems: "center", gap: 8, cursor: "pointer", userSelect: "none" }}>
            <input
              type="checkbox"
              checked={entrySoundOn}
              onChange={(e) => {
                const on = e.target.checked;
                setEntrySoundOn(on);
                try {
                  localStorage.setItem("streamEntrySound", on ? "1" : "0");
                } catch {
                  /* private mode */
                }
                if (on) {
                  void resumeStreamAudio().then((ok) => {
                    if (ok) setAudioUnlocked(true);
                  });
                }
              }}
            />
            🔊 Entry & exit sounds
          </label>
          {entrySoundOn ? (
            <div style={{ textAlign: "right", maxWidth: 300 }}>
              {!audioUnlocked ? (
                <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 6, lineHeight: 1.35 }}>
                  Click or tap anywhere on this page once — browsers block audio until you do.
                </div>
              ) : null}
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "flex-end" }}>
                <button
                  type="button"
                  onClick={async () => {
                    const ok = await resumeStreamAudio();
                    if (ok) setAudioUnlocked(true);
                    playEntryChime();
                  }}
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    padding: "6px 12px",
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid rgba(52, 211, 153, 0.5)",
                    color: "var(--text)",
                    cursor: "pointer",
                  }}
                >
                  Test entry ▲
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    const ok = await resumeStreamAudio();
                    if (ok) setAudioUnlocked(true);
                    playExitChime();
                  }}
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    padding: "6px 12px",
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid rgba(251, 146, 60, 0.45)",
                    color: "var(--text)",
                    cursor: "pointer",
                  }}
                >
                  Test exit ▼
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </header>

      <BroadcastFit enabled={fitBroadcast} parentRef={broadcastParentRef} contentRef={broadcastContentRef}>
      {err && (
        <div style={{ padding: 12, borderRadius: 8, background: "var(--down-muted)", color: "var(--text)", marginBottom: 20 }}>
          {err}
        </div>
      )}

      {exitBanner ? (
        <div
          role="status"
          aria-live="polite"
          style={{
            marginBottom: 20,
            padding: "14px 18px",
            borderRadius: 12,
            border: "1px solid rgba(52, 211, 153, 0.55)",
            background: "linear-gradient(135deg, rgba(52, 211, 153, 0.14), rgba(15, 23, 42, 0.96))",
            fontSize: 16,
            fontWeight: 650,
            color: "var(--text)",
            lineHeight: 1.45,
            boxShadow: "0 0 28px rgba(52, 211, 153, 0.22), inset 0 1px 0 rgba(255,255,255,0.06)",
          }}
        >
          {exitBanner}
        </div>
      ) : null}

      {isShowcase ? (
        <section
          aria-label="Stream mood and session clock"
          style={{
            marginBottom: fitBroadcast ? 12 : 26,
            display: "flex",
            flexWrap: "wrap",
            gap: fitBroadcast ? 12 : 18,
            alignItems: "stretch",
            justifyContent: "space-between",
          }}
        >
          <div style={{ flex: "2 1 280px", minWidth: 0 }}>
            <div
              style={{
                padding: "18px 20px",
                borderRadius: 14,
                border: `1px solid ${moodStyle.border}`,
                background: moodStyle.bg,
                boxShadow: moodStyle.shadow,
              }}
            >
              <div style={{ fontSize: 11, letterSpacing: "0.14em", textTransform: "uppercase", color: "var(--muted)", marginBottom: 8 }}>
                Bot mode
              </div>
              <div
                style={{
                  fontSize: "clamp(26px, 4.5vw, 34px)",
                  fontWeight: 900,
                  letterSpacing: "0.08em",
                  lineHeight: 1.1,
                  color: moodStyle.color,
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {streamMood.label}
              </div>
              <div style={{ fontSize: 14, color: "var(--text-secondary)", marginTop: 10, lineHeight: 1.45 }}>{streamMood.hint}</div>
              {showHotStreak ? (
                <div
                  style={{
                    marginTop: 12,
                    fontSize: 13,
                    fontWeight: 700,
                    color: "#fbbf24",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <span aria-hidden>🔥</span>
                  Hot streak — {winRatePct != null ? `${winRatePct.toFixed(0)}%` : "—"} win rate ({winRateWins}/{winRateExits} exits)
                </div>
              ) : null}
            </div>
          </div>

          <div style={{ flex: "1.4 1 240px", minWidth: 0 }}>
            <div
              style={{
                height: "100%",
                minHeight: 120,
                padding: "16px 18px",
                borderRadius: 14,
                border: "1px solid rgba(52, 211, 153, 0.35)",
                background: "linear-gradient(160deg, rgba(15, 23, 42, 0.95), rgba(30, 41, 59, 0.35))",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
                <span style={{ fontSize: 12, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--muted)", fontWeight: 700 }}>
                  Market window
                </span>
                <span style={{ fontSize: 22, fontWeight: 800, fontVariantNumeric: "tabular-nums", color: "#6ee7b7" }}>
                  {market && effectiveWindowSecondsLeft != null
                    ? formatTimeLeft(effectiveWindowSecondsLeft)
                    : "—"}
                </span>
              </div>
              <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 6 }}>Time left until this round resets</div>
              <div className="stream-window-bar" aria-hidden>
                <div className="stream-window-bar-fill" style={{ width: `${windowProgressPct}%` }} />
              </div>
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8 }}>{windowLabel(market)} · bar = time remaining in window</div>
            </div>
          </div>

          <div style={{ flex: "0 0 auto", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
            <div
              className="stream-pulse-orb"
              role="img"
              aria-label={[
                livePct != null
                  ? `Unrealized PnL for this market window and open trade: ${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)} percent`
                  : "Unrealized PnL for this window — no data",
                showUsd && liveUsd != null && Number.isFinite(Number(liveUsd))
                  ? `, ${formatUsdSigned(Number(liveUsd))} unrealized USD (this window only, not account total)`
                  : "",
              ].join("")}
              style={
                {
                  "--pulse-rgb": pulseRingRgb,
                  "--pulse-sec": `${streamPulseSec}s`,
                } as React.CSSProperties
              }
            >
              <div style={{ position: "relative", zIndex: 1, textAlign: "center", padding: "10px 10px 8px" }}>
                <div
                  style={{
                    fontSize: 11,
                    letterSpacing: "0.05em",
                    textTransform: "uppercase",
                    color: "var(--muted)",
                    fontWeight: 600,
                    marginBottom: 5,
                    lineHeight: 1.25,
                    maxWidth: 148,
                  }}
                >
                  Unrealized · {windowLabel(market)} window
                </div>
                <div
                  style={{
                    fontSize: 28,
                    fontWeight: 900,
                    fontVariantNumeric: "tabular-nums",
                    color: livePct == null ? "var(--muted)" : livePct >= 0 ? "var(--up)" : "var(--down)",
                    lineHeight: 1.1,
                  }}
                >
                  {livePct != null ? `${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)}%` : "—"}
                </div>
                {showUsd ? (
                  <>
                    <div
                      style={{
                        fontSize: 11,
                        letterSpacing: "0.08em",
                        textTransform: "uppercase",
                        color: "var(--muted)",
                        marginTop: 8,
                        marginBottom: 3,
                        fontWeight: 700,
                      }}
                    >
                      Unrealized $
                    </div>
                    <div
                      style={{
                        fontSize: 9,
                        letterSpacing: "0.04em",
                        textTransform: "uppercase",
                        color: "var(--muted)",
                        marginBottom: 3,
                        fontWeight: 600,
                        lineHeight: 1.3,
                        maxWidth: 148,
                      }}
                    >
                      Open trade · not account total
                    </div>
                    <div
                      style={{
                        fontSize: 17,
                        fontWeight: 800,
                        fontVariantNumeric: "tabular-nums",
                        color: pnlColor,
                        lineHeight: 1.15,
                      }}
                    >
                      {liveUsd != null && Number.isFinite(Number(liveUsd)) ? formatUsdSigned(Number(liveUsd)) : "—"}
                    </div>
                  </>
                ) : null}
              </div>
            </div>
          </div>
        </section>
      ) : null}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
          gap: fitBroadcast ? 12 : 20,
          marginBottom: fitBroadcast ? 14 : 28,
        }}
      >
        <StreamBlock
          label="Trade status"
          value={open ? "In trade" : "Flat"}
          sub={open ? `${agg!.side} — live position (TP not hit yet)` : "No open position — between trades"}
          highlight={open}
        />
        <StreamBlock
          label="Direction"
          value={open ? agg!.side : "—"}
          sub={open ? "Contract side" : "No side (flat)"}
          dirUp={open && agg!.side === "Up"}
        />
        <StreamBlock
          label="Bot runtime"
          value={
            stratCfg?.mode === "off"
              ? "Bot off"
              : botRunUptimeSec != null
                ? formatBotUptime(botRunUptimeSec)
                : "—"
          }
          sub="Wall time since semi/auto was enabled"
        />
        <StreamBlock
          label="Avg entry"
          value={open && agg!.avgEntryCents > 0 ? `${agg!.avgEntryCents.toFixed(1)}¢` : "—"}
        />
        {!isShowcase ? (
          <>
            <StreamBlock
              label="Unrealized %"
              sub="This market window · open trade (not account P&L)"
              value={livePct != null ? `${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)}%` : "—"}
              valueColor={pnlColor}
              large
            />
            {showUsd ? (
              <StreamBlock
                label="Unrealized $"
                sub="This window only — not account total"
                value={liveUsd != null && Number.isFinite(Number(liveUsd)) ? formatUsdSigned(Number(liveUsd)) : "—"}
                valueColor={pnlColor}
                large
              />
            ) : null}
          </>
        ) : null}
        <StreamBlock label="Window" value={windowLabel(market)} />
        <StreamBlock
          label="Time left"
          value={
            market && effectiveWindowSecondsLeft != null
              ? formatTimeLeft(effectiveWindowSecondsLeft)
              : "—"
          }
        />
        <StreamBlock
          label="P&L since bot on"
          value={
            stratCfg?.mode === "off"
              ? "—"
              : runPnlUsd != null && Number.isFinite(runPnlUsd)
                ? formatUsdSigned(runPnlUsd)
                : "—"
          }
          valueColor={runPnlColor}
          large
          sub="Net equity vs. when you enabled semi/auto"
        />
        <div
          className={winRateHot ? "stream-winrate-gold" : undefined}
          role="status"
          title={
            stratCfg?.mode === "off"
              ? "Win rate (this bot run) — bot is off"
              : winRateExits === 0 || winRatePct == null
                ? "Win rate (this bot run) — no closed trades yet"
                : `Win rate (this bot run): ${winRatePct!.toFixed(1)}% — ${winRateWins} / ${winRateExits} winning exits (realized)`
          }
          aria-label={
            stratCfg?.mode === "off"
              ? "Win rate this bot run: unavailable, bot off"
              : winRateExits === 0 || winRatePct == null
                ? "Win rate this bot run: no closed trades yet"
                : `Win rate this bot run ${winRatePct!.toFixed(1)} percent, ${winRateWins} of ${winRateExits} winning exits`
          }
          style={{
            width: "100%",
            minWidth: 0,
            minHeight: 138,
            boxSizing: "border-box",
            padding: "14px 12px",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--border)",
            background: "var(--bg-elevated)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            textAlign: "center",
          }}
        >
          <div
            style={{
              fontSize: 11,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              color: "var(--muted)",
              fontWeight: 700,
              lineHeight: 1.2,
              marginBottom: 6,
            }}
          >
            Win rate
            <br />
            <span style={{ fontSize: 10, letterSpacing: "0.04em", fontWeight: 600, opacity: 0.92 }}>this bot run</span>
          </div>
          <div
            style={{
              fontSize: 26,
              fontWeight: 800,
              fontVariantNumeric: "tabular-nums",
              lineHeight: 1.1,
              color:
                stratCfg?.mode === "off" || winRateExits === 0
                  ? "var(--muted)"
                  : winRateHot
                    ? "#fbbf24"
                    : winRatePct != null && winRatePct >= 50
                      ? "var(--up)"
                      : "var(--down)",
            }}
          >
            {stratCfg?.mode === "off"
              ? "—"
              : winRateExits === 0 || winRatePct == null
                ? "—"
                : `${winRatePct.toFixed(1)}%`}
          </div>
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6, lineHeight: 1.3, maxWidth: 200 }}>
            {stratCfg?.mode === "off"
              ? "Bot off"
              : winRateExits === 0
                ? "No exits yet"
                : winRatePct != null
                  ? `${winRateWins} / ${winRateExits} winning exits`
                  : "—"}
          </div>
        </div>
      </div>

      <div
        role="group"
        aria-label={open ? "Market mid prices (in trade)" : "Market mid prices — no position"}
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: fitBroadcast ? 10 : 16,
          marginBottom: fitBroadcast ? 12 : 28,
          justifyContent: "center",
          alignItems: "stretch",
        }}
      >
        <LiveCentsPill label="Up" mid={orderbook?.up?.mid ?? null} accent="up" flatMarket={!open} />
        <LiveCentsPill label="Down" mid={orderbook?.down?.mid ?? null} accent="down" flatMarket={!open} />
      </div>

      <section>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: 12,
            marginBottom: 10,
          }}
        >
          <div style={{ fontSize: 13, letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--muted)" }}>
            PnL % vs. cost (session)
          </div>
          {open && chartExtremes && chartRows.length > 0 ? (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 12,
                fontSize: 13,
                fontVariantNumeric: "tabular-nums",
                color: "var(--text-secondary)",
                justifyContent: "flex-end",
              }}
            >
              <span>
                <span style={{ color: "#fbbf24", fontWeight: 800, marginRight: 6 }}>▲ Peak</span>
                {chartExtremes.maxPct.toFixed(2)}%
              </span>
              <span aria-hidden style={{ color: "var(--border)", userSelect: "none" }}>
                ·
              </span>
              <span>
                <span style={{ color: "#f87171", fontWeight: 800, marginRight: 6 }}>▼ Trough</span>
                {chartExtremes.minPct.toFixed(2)}%
              </span>
              <span aria-hidden style={{ color: "var(--border)", userSelect: "none" }}>
                ·
              </span>
              <span>
                <span style={{ color: lineColor, fontWeight: 800, marginRight: 6 }}>● Now</span>
                {chartRows[chartRows.length - 1].pct.toFixed(2)}%
              </span>
              {chartRows.length > 1 ? (
                <span style={{ color: "var(--muted)", fontSize: 12 }}>
                  swing {Math.abs(chartExtremes.maxPct - chartExtremes.minPct).toFixed(2)}%
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
        <div
          style={{
            width: "100%",
            height: fitBroadcast ? (isShowcase ? 240 : 200) : isShowcase ? 320 : 260,
            background: "var(--card)",
            borderRadius: "var(--radius-md)",
            border: `1px solid ${isShowcase ? "rgba(52, 211, 153, 0.22)" : "var(--border)"}`,
          }}
        >
          {!open ? (
            <div style={{ height: "100%", color: "var(--text-secondary)" }}>
              {chartIdleCopy ? <ChartIdlePanel copy={chartIdleCopy} /> : null}
            </div>
          ) : chartRows.length === 0 ? (
            <div
              style={{
                height: "100%",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                gap: 14,
                padding: 16,
              }}
            >
              <div className="stream-idle-spinner stream-idle-spinner--sm" aria-hidden />
              <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-secondary)" }}>Drawing your PnL line…</div>
              <div style={{ fontSize: 13, color: "var(--muted)" }}>First ticks incoming — almost there.</div>
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartRows} margin={{ top: 10, right: 8, bottom: 6, left: 4 }}>
                <defs>
                  <linearGradient id="pnlGradientStream" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={chartTintHex} stopOpacity={0.55} />
                    <stop offset="50%" stopColor={chartTintHex} stopOpacity={0.14} />
                    <stop offset="100%" stopColor={chartTintHex} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="var(--border)" strokeOpacity={0.5} strokeDasharray="3 6" vertical={false} />
                <XAxis
                  dataKey="t"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickFormatter={(v) =>
                    new Date(Number(v) * 1000).toLocaleTimeString("en-US", {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                      hour12: false,
                    })
                  }
                />
                <YAxis
                  dataKey="pct"
                  domain={pnlYDomain}
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickFormatter={(v) => `${Number(v).toFixed(1)}%`}
                  width={56}
                  tickCount={7}
                />
                <Tooltip
                  contentStyle={{
                    background: "var(--chart-tooltip-bg)",
                    border: "1px solid var(--chart-tooltip-border)",
                    borderRadius: 8,
                    fontSize: 13,
                  }}
                  cursor={{ stroke: "rgba(248, 250, 252, 0.35)", strokeWidth: 1 }}
                  labelFormatter={(v) =>
                    new Date(Number(v) * 1000).toLocaleString("en-US", {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                      hour12: false,
                    })
                  }
                  formatter={(value: number) => [`${Number(value).toFixed(2)}%`, "PnL %"]}
                />
                <ReferenceLine y={0} stroke="var(--border-strong)" strokeDasharray="4 4" />
                {chartExtremes && chartRows.length > 1 && chartExtremes.maxPct !== chartExtremes.minPct ? (
                  <>
                    <ReferenceLine
                      y={chartExtremes.maxPct}
                      stroke="#fbbf24"
                      strokeDasharray="5 5"
                      strokeOpacity={0.6}
                    />
                    <ReferenceLine
                      y={chartExtremes.minPct}
                      stroke="#f87171"
                      strokeDasharray="5 5"
                      strokeOpacity={0.6}
                    />
                  </>
                ) : null}
                <Area type="monotone" dataKey="pct" stroke="none" fill="url(#pnlGradientStream)" isAnimationActive={false} />
                <Line
                  type="monotone"
                  dataKey="pct"
                  stroke={lineColor}
                  strokeWidth={2.5}
                  dot={renderPnlDot}
                  isAnimationActive={false}
                  activeDot={{ r: 5, strokeWidth: 0, fill: lineColor }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          )}
        </div>
      </section>

      <footer style={{ marginTop: 24, fontSize: 12, color: "var(--muted)" }}>
        <strong style={{ color: "var(--text-secondary)" }}>Compact</strong> (<code style={{ color: "var(--accent-bright)" }}>?stream=1</code>): stat grid + prices + chart — no mood/pulse strip.{" "}
        <strong style={{ color: "var(--text-secondary)" }}>Showcase</strong> (<code style={{ color: "var(--accent-bright)" }}>?stream=2</code>,{" "}
        <code style={{ color: "var(--accent-bright)" }}>&layout=showcase</code>, <code style={{ color: "var(--accent-bright)" }}>/stream/showcase</code>): full hero (mood, pulse, window bar). Hide $:{" "}
        <code style={{ color: "var(--accent-bright)" }}>&usd=0</code>.
      </footer>
      </BroadcastFit>
    </div>
  );
}

function LiveCentsPill(props: { label: string; mid: number | null; accent: "up" | "down"; flatMarket?: boolean }) {
  const { label, mid, accent, flatMarket } = props;
  const isUp = accent === "up";
  const glow = isUp
    ? "0 0 32px rgba(52, 211, 153, 0.65), 0 0 64px rgba(16, 185, 129, 0.28)"
    : "0 0 32px rgba(251, 113, 133, 0.6), 0 0 64px rgba(244, 63, 94, 0.26)";
  const color = isUp ? "#4ade80" : "#fb7185";
  const bg = isUp ? "linear-gradient(145deg, rgba(52, 211, 153, 0.14), rgba(15, 23, 42, 0.9))" : "linear-gradient(145deg, rgba(251, 113, 133, 0.14), rgba(15, 23, 42, 0.9))";
  const border = isUp ? "1px solid rgba(52, 211, 153, 0.5)" : "1px solid rgba(251, 113, 133, 0.5)";
  const display = pxToCentsLabel(mid ?? undefined);
  return (
    <div
      style={{
        flex: "1 1 220px",
        minWidth: 180,
        maxWidth: 420,
        padding: "18px 22px",
        borderRadius: 14,
        background: bg,
        border,
        boxShadow: `${glow}, inset 0 1px 0 rgba(255,255,255,0.06)`,
        textAlign: "center",
        opacity: flatMarket ? 0.52 : 1,
        filter: flatMarket ? "grayscale(0.25)" : undefined,
        transition: "opacity 0.35s ease, filter 0.35s ease",
      }}
    >
      <div style={{ marginBottom: 10 }}>
        <div
          style={{
            fontSize: 14,
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            color: isUp ? "rgba(167, 243, 208, 0.98)" : "rgba(254, 202, 202, 0.98)",
            fontWeight: 800,
          }}
        >
          {label}
        </div>
        <div
          style={{
            fontSize: 11,
            marginTop: 5,
            fontWeight: 500,
            letterSpacing: "0.04em",
            color: isUp ? "rgba(167, 243, 208, 0.75)" : "rgba(254, 202, 202, 0.75)",
          }}
        >
          Live mid (¢)
        </div>
      </div>
      <div
        style={{
          fontSize: 42,
          fontWeight: 800,
          fontVariantNumeric: "tabular-nums",
          color,
          textShadow: glow,
          lineHeight: 1.1,
        }}
      >
        {display}
      </div>
      {flatMarket ? (
        <div style={{ fontSize: 10, marginTop: 10, color: "var(--muted)", fontWeight: 600, letterSpacing: "0.04em" }}>
          Market only — no open position
        </div>
      ) : null}
    </div>
  );
}

function StreamBlock(props: {
  label: string;
  value: string;
  sub?: string;
  highlight?: boolean;
  dirUp?: boolean;
  large?: boolean;
  valueColor?: string;
  pulseGold?: boolean;
}) {
  const { label, value, sub, highlight, dirUp, large, valueColor, pulseGold } = props;
  return (
    <div
      className={pulseGold ? "stream-winrate-gold" : undefined}
      style={{
        padding: "14px 16px",
        borderRadius: "var(--radius-md)",
        background: highlight ? "var(--accent-muted)" : "var(--bg-elevated)",
        border: "1px solid var(--border)",
      }}
    >
      <div style={{ fontSize: 12, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>{label}</div>
      <div
        style={{
          fontSize: large ? 28 : 20,
          fontWeight: 700,
          color: valueColor ?? (dirUp === true ? "var(--up)" : dirUp === false ? "var(--down)" : "var(--text)"),
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.2,
        }}
      >
        {value}
      </div>
      {sub ? <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>{sub}</div> : null}
    </div>
  );
}
