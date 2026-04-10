import { useCallback, useEffect, useMemo, useState } from "react";
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "./api";

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
  trades?: { ts?: number; type?: string; token_id?: string }[];
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

/** Baseline equity ל-PnL בשידור: קודם bot_run, אחרת ui_runtime (מצב לא off). */
function pickBotRunEquityBaseline(cfg: StrategyConfigSlice | null, demo: DemoState | null): number | null {
  const br = toFiniteNumber(cfg?.bot_run_equity_baseline_usd ?? demo?.bot_run_equity_baseline_usd);
  if (br != null) return br;
  if (cfg?.mode === "off") return null;
  return toFiniteNumber(cfg?.ui_runtime_equity_baseline_usd ?? demo?.ui_runtime_equity_baseline_usd);
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

export default function LiveStreamTrade() {
  const [market, setMarket] = useState<Market | null>(null);
  const [demo, setDemo] = useState<DemoState | null>(null);
  const [stratCfg, setStratCfg] = useState<StrategyConfigSlice | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{ action?: string } | null>(null);
  const [orderbook, setOrderbook] = useState<OrderbookSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [clock, setClock] = useState(0);
  const [showUsd, setShowUsd] = useState(() =>
    typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("usd") !== "0" : true
  );

  const refresh = useCallback(async () => {
    try {
      setErr(null);
      const [m, st, cfg, pend, ob] = await Promise.all([
        api<Market>("/api/market/current"),
        api<DemoState>("/api/demo/state"),
        api<StrategyConfigSlice>("/api/strategy/config"),
        api<{ pending: { action?: string } | null }>("/api/strategy/pending"),
        safeApi<OrderbookSummary>("/api/market/orderbook-summary"),
      ]);
      setMarket(m);
      setDemo(st);
      setStratCfg(cfg);
      setPendingApproval(pend?.pending ?? null);
      setOrderbook(ob);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    document.documentElement.lang = "en";
    document.documentElement.dir = "ltr";
    document.title = "Live trade — stream";
    return () => {
      document.documentElement.lang = "he";
      document.documentElement.dir = "rtl";
    };
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 1000);
    return () => window.clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    const id = window.setInterval(() => setClock((c) => c + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const positions = demo?.positions ?? [];
  const open = positions.length > 0;
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

  const liveUsd = open && typeof demo?.last_mark?.unrealized_usd === "number" ? demo.last_mark.unrealized_usd : null;

  const equityNow = useMemo(() => {
    const eq = demo?.last_mark?.equity;
    if (typeof eq === "number" && Number.isFinite(eq)) return eq;
    return Number(demo?.balance_usd ?? 0);
  }, [demo?.last_mark?.equity, demo?.balance_usd]);

  const equityBaselineUsd = useMemo(() => pickBotRunEquityBaseline(stratCfg, demo), [stratCfg, demo]);

  const runPnlUsd = useMemo(() => {
    if (stratCfg?.mode === "off") return null;
    const base = equityBaselineUsd;
    if (base == null) return null;
    return equityNow - base;
  }, [stratCfg?.mode, equityBaselineUsd, equityNow]);

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

  const runPnlColor =
    runPnlUsd == null ? "var(--muted)" : runPnlUsd >= 0 ? "var(--up)" : "var(--down)";

  const chartRows = useMemo(() => {
    const path = leg?.pnl_path;
    if (!path?.length) return [];
    return path.map((p) => ({
      t: p.ts,
      pct: p.upnl_pct,
    }));
  }, [leg?.pnl_path]);

  const pnlColor =
    livePct == null ? "var(--muted)" : livePct >= 0 ? "var(--up)" : "var(--down)";
  const lineColor = chartRows.length ? pnlColor : "var(--muted)";

  const chartIdleCopy = useMemo(() => {
    if (open) return null;
    return resolveChartIdleCopy({
      mode: stratCfg?.mode,
      pending: pendingApproval,
      statusKey: stratCfg?.strategy_status_key ?? "",
      secondsLeft: market?.seconds_left,
      minMinutesForEntry: Number(stratCfg?.min_minutes_for_entry ?? 3),
      freezeLastMinutes: Number(stratCfg?.freeze_last_minutes ?? 1),
      intermediateBlock: !!stratCfg?.intermediate_block_new_entries,
      sidePreference: stratCfg?.side_preference,
    });
  }, [open, stratCfg, market?.seconds_left, pendingApproval]);

  return (
    <div
      className="stream-trade-root"
      style={{
        minHeight: "100vh",
        boxSizing: "border-box",
        padding: "28px 36px",
        background: "var(--bg)",
        color: "var(--text)",
        fontFamily: "var(--font-display)",
        maxWidth: 920,
        margin: "0 auto",
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
        `}
      </style>
      <header style={{ marginBottom: 28, display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
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
          </div>
          <h1 style={{ margin: "8px 0 0", fontSize: 22, fontWeight: 600, lineHeight: 1.25 }}>{market?.title ?? "Loading…"}</h1>
        </div>
        <label style={{ fontSize: 13, color: "var(--muted)", display: "flex", alignItems: "center", gap: 8, cursor: "pointer", userSelect: "none" }}>
          <input type="checkbox" checked={showUsd} onChange={(e) => setShowUsd(e.target.checked)} />
          PnL $
        </label>
      </header>

      {err && (
        <div style={{ padding: 12, borderRadius: 8, background: "var(--down-muted)", color: "var(--text)", marginBottom: 20 }}>
          {err}
        </div>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
          gap: 20,
          marginBottom: 28,
        }}
      >
        <StreamBlock label="Trade status" value={open ? "Open" : "Exited"} highlight={open} />
        <StreamBlock label="Direction" value={open ? agg!.side : "—"} dirUp={open && agg!.side === "Up"} />
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
        <StreamBlock
          label="Live PnL %"
          value={livePct != null ? `${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)}%` : "—"}
          valueColor={pnlColor}
          large
        />
        {showUsd && (
          <StreamBlock
            label="Live PnL $"
            value={liveUsd != null && Number.isFinite(Number(liveUsd)) ? formatUsdSigned(Number(liveUsd)) : "—"}
            valueColor={pnlColor}
            large
          />
        )}
        <StreamBlock label="Window" value={windowLabel(market)} />
        <StreamBlock label="Time left" value={market ? formatTimeLeft(market.seconds_left) : "—"} />
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
      </div>

      <div style={{ marginBottom: 24 }}>
        <StreamBlock
          label="Win rate (this bot run)"
          value={
            stratCfg?.mode === "off"
              ? "—"
              : winRateExits === 0 || winRatePct == null
                ? "No closed trades yet"
                : `${winRatePct.toFixed(1)}%`
          }
          sub={
            stratCfg?.mode !== "off" && winRateExits > 0 && winRatePct != null
              ? `${winRateWins} / ${winRateExits} winning exits (realized)`
              : stratCfg?.mode !== "off"
                ? "TP / settle / expire — same rules as Stats tab"
                : undefined
          }
          large
          valueColor={
            stratCfg?.mode === "off" || winRateExits === 0
              ? "var(--muted)"
              : winRateHot
                ? "#fbbf24"
                : winRatePct != null && winRatePct >= 50
                  ? "var(--up)"
                  : "var(--down)"
          }
          pulseGold={!!winRateHot}
        />
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 16,
          marginBottom: 28,
          justifyContent: "center",
          alignItems: "stretch",
        }}
      >
        <LiveCentsPill label="Up" mid={orderbook?.up?.mid ?? null} accent="up" />
        <LiveCentsPill label="Down" mid={orderbook?.down?.mid ?? null} accent="down" />
      </div>

      <section>
        <div style={{ fontSize: 13, letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--muted)", marginBottom: 12 }}>
          PnL % vs. cost (session)
        </div>
        <div style={{ width: "100%", height: 260, background: "var(--card)", borderRadius: "var(--radius-md)", border: "1px solid var(--border)" }}>
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
              <LineChart data={chartRows} margin={{ top: 16, right: 16, bottom: 8, left: 8 }}>
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
                  domain={["auto", "auto"]}
                  tick={{ fill: "var(--muted)", fontSize: 11 }}
                  tickFormatter={(v) => `${v}%`}
                  width={52}
                />
                <Tooltip
                  contentStyle={{
                    background: "var(--chart-tooltip-bg)",
                    border: "1px solid var(--chart-tooltip-border)",
                    borderRadius: 8,
                    fontSize: 13,
                  }}
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
                <Line type="monotone" dataKey="pct" stroke={lineColor} strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </section>

      <footer style={{ marginTop: 24, fontSize: 12, color: "var(--muted)" }}>
        Add <code style={{ color: "var(--accent-bright)" }}>?stream=1</code> to this app URL for OBS. Hide dollar PnL with{" "}
        <code style={{ color: "var(--accent-bright)" }}>&usd=0</code>.
      </footer>
    </div>
  );
}

function LiveCentsPill(props: { label: string; mid: number | null; accent: "up" | "down" }) {
  const { label, mid, accent } = props;
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
      }}
    >
      <div
        style={{
          fontSize: 13,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: isUp ? "rgba(167, 243, 208, 0.95)" : "rgba(254, 202, 202, 0.95)",
          marginBottom: 10,
          fontWeight: 600,
        }}
      >
        {label} · mid
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
