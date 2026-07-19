import type { CSSProperties, ReactNode, RefObject } from "react";
import { useMemo, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
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
import { playEntryChime, playExitChime, resumeStreamAudio } from "./streamAudio";
import type { RoundOutcomeRow } from "./StreamSpectatorLayout";
import { israelTime } from "./timeFormat";

/* ── local types (duplicated to avoid touching the original file) ── */

type Market = {
  title: string;
  window_sec?: number;
  btc_window?: string;
  seconds_left: number;
};

type StrategyCfg = {
  mode?: string;
  bot_run_win_rate_pct?: number | null;
  bot_run_exit_trades_n?: number;
  bot_run_wins_n?: number;
};

type Agg = {
  contracts: number;
  avgEntryCents: number;
  side: "Up" | "Down";
  tokenIds: string[];
};

type OrderbookSummary = {
  up: { mid: number | null };
  down: { mid: number | null };
};

type ChartIdleCopy = { headline: string; sub: string; showSpinner: boolean };
type MoodStyle = { border: string; color: string; bg: string; shadow: string };
type StreamMood = { label: string; hint: string; variant: string };

/* ── helpers ── */

function formatUsdSigned(v: number): string {
  const a = Math.abs(v);
  return `${v >= 0 ? "+" : "-"}$${a.toFixed(2)}`;
}

const RUN_PNL_GREEN = "#34d399";
const RUN_PNL_RED = "#fb7185";

type RunPnlPoint = { t: number; usd: number };

function splitRunPnlSegments(points: RunPnlPoint[]): { stroke: string; fill: string; data: RunPnlPoint[] }[] {
  if (points.length === 0) return [];
  if (points.length === 1) {
    const p = points[0];
    const c = p.usd >= 0 ? RUN_PNL_GREEN : RUN_PNL_RED;
    return [{ stroke: c, fill: c, data: [p, { t: p.t + 1e-6, usd: p.usd }] }];
  }
  const segments: { stroke: string; fill: string; data: RunPnlPoint[] }[] = [];
  let current: RunPnlPoint[] = [{ ...points[0] }];

  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const curr = points[i];
    const prevPos = prev.usd >= 0;
    const currPos = curr.usd >= 0;

    if (prevPos === currPos) {
      current.push({ ...curr });
      continue;
    }
    const u0 = prev.usd;
    const u1 = curr.usd;
    const t0 = prev.t;
    const t1 = curr.t;
    if (Math.abs(u1 - u0) < 1e-12) {
      current.push({ ...curr });
      continue;
    }
    const ratio = u0 / (u0 - u1);
    const tz = t0 + ratio * (t1 - t0);
    const crossing: RunPnlPoint = { t: tz, usd: 0 };
    current.push(crossing);
    segments.push({
      stroke: prevPos ? RUN_PNL_GREEN : RUN_PNL_RED,
      fill: prevPos ? RUN_PNL_GREEN : RUN_PNL_RED,
      data: [...current],
    });
    current = [crossing, { ...curr }];
  }
  const lastPos = points[points.length - 1].usd >= 0;
  segments.push({
    stroke: lastPos ? RUN_PNL_GREEN : RUN_PNL_RED,
    fill: lastPos ? RUN_PNL_GREEN : RUN_PNL_RED,
    data: [...current],
  });
  return segments.filter((s) => s.data.length >= 2);
}

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

function formatBotUptime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const h = Math.floor(sec / 3600);
  const mi = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${mi}m ${String(s).padStart(2, "0")}s`;
  if (mi > 0) return `${mi}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function pxToCentsLabel(px: number | null | undefined): string {
  if (px == null || !Number.isFinite(Number(px))) return "—";
  return `${(Number(px) * 100).toFixed(1)}¢`;
}

/* ── sub-components ── */

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

function ChartIdlePanel({ copy }: { copy: ChartIdleCopy }) {
  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 16,
        padding: "12px 24px",
        textAlign: "center",
        maxWidth: 460,
        margin: "0 auto",
      }}
    >
      {copy.showSpinner ? <div className="pro-idle-spinner" aria-hidden /> : null}
      <div>
        <div style={{ fontSize: 18, fontWeight: 750, lineHeight: 1.35, color: "var(--text)" }}>{copy.headline}</div>
        <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 8, lineHeight: 1.45 }}>{copy.sub}</div>
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
  compact?: boolean;
}) {
  const { label, value, sub, highlight, dirUp, large, valueColor, pulseGold, compact } = props;
  return (
    <div
      className={pulseGold ? "pro-winrate-gold" : undefined}
      style={{
        padding: compact ? "10px 14px" : "14px 16px",
        borderRadius: "var(--radius-md)",
        background: highlight ? "var(--accent-muted)" : "var(--bg-elevated)",
        border: "1px solid var(--border)",
      }}
    >
      <div style={{ fontSize: 11, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--muted)", marginBottom: 4 }}>{label}</div>
      <div
        style={{
          fontSize: large ? 24 : 18,
          fontWeight: 700,
          color: valueColor ?? (dirUp === true ? "var(--up)" : dirUp === false ? "var(--down)" : "var(--text)"),
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.2,
        }}
      >
        {value}
      </div>
      {sub ? <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 4 }}>{sub}</div> : null}
    </div>
  );
}

function LiveCentsPill(props: { label: string; mid: number | null; accent: "up" | "down"; flatMarket?: boolean; compact?: boolean }) {
  const { label, mid, accent, flatMarket, compact } = props;
  const isUp = accent === "up";
  const glow = isUp
    ? "0 0 32px rgba(52, 211, 153, 0.65), 0 0 64px rgba(16, 185, 129, 0.28)"
    : "0 0 32px rgba(251, 113, 133, 0.6), 0 0 64px rgba(244, 63, 94, 0.26)";
  const color = isUp ? "#4ade80" : "#fb7185";
  const bg = isUp ? "linear-gradient(145deg, rgba(52, 211, 153, 0.14), rgba(15, 23, 42, 0.9))" : "linear-gradient(145deg, rgba(251, 113, 133, 0.14), rgba(15, 23, 42, 0.9))";
  const border = isUp ? "1px solid rgba(52, 211, 153, 0.5)" : "1px solid rgba(251, 113, 133, 0.5)";
  return (
    <div
      style={{
        flex: "1 1 200px",
        minWidth: 160,
        maxWidth: 380,
        padding: compact ? "12px 18px" : "18px 22px",
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
      <div style={{ marginBottom: compact ? 6 : 10 }}>
        <div
          style={{
            fontSize: 13,
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
            fontSize: 10,
            marginTop: 3,
            fontWeight: 500,
            letterSpacing: "0.04em",
            color: isUp ? "rgba(167, 243, 208, 0.75)" : "rgba(254, 202, 202, 0.75)",
          }}
        >
          Live mid (¢)
        </div>
      </div>
      <div style={{ fontSize: compact ? 34 : 42, fontWeight: 800, fontVariantNumeric: "tabular-nums", color, textShadow: glow, lineHeight: 1.1 }}>
        {pxToCentsLabel(mid ?? undefined)}
      </div>
      {flatMarket ? (
        <div style={{ fontSize: 10, marginTop: compact ? 6 : 10, color: "var(--muted)", fontWeight: 600, letterSpacing: "0.04em" }}>Market only — no open position</div>
      ) : null}
    </div>
  );
}

/* ── props type (same shape as spectator) ── */

export type StreamProLayoutProps = {
  variant?: "v2";
  fitBroadcast: boolean;
  broadcastParentRef: RefObject<HTMLDivElement>;
  broadcastContentRef: RefObject<HTMLDivElement>;
  err: string | null;
  exitBanner: string | null;
  market: Market | null;
  stratCfg: StrategyCfg | null;
  orderbook: OrderbookSummary | null;
  open: boolean;
  agg: Agg | null;
  livePct: number | null;
  pnlColor: string;
  entrySoundOn: boolean;
  setEntrySoundOn: (v: boolean) => void;
  audioUnlocked: boolean;
  setAudioUnlocked: (v: boolean) => void;
  streamMood: StreamMood;
  moodStyle: MoodStyle;
  showHotStreak: boolean;
  winRatePct: number | null;
  winRateExits: number;
  winRateWins: number;
  winRateHot: boolean;
  runPnlUsd: number | null;
  runPnlSeries: { t: number; usd: number }[];
  runUsdYDomain: [number, number];
  runUsdSessionStats: { maxUsd: number; minUsd: number; last: number } | null;
  runUsdChartRefExtremes: { maxUsd: number; minUsd: number } | null;
  botRunUptimeSec: number | null;
  /** Client-interpolated seconds left (ticks every 1s between server polls). */
  windowSecondsLeftDisplay: number | null;
  windowElapsedPct: number;
  roundOutcomes: RoundOutcomeRow[];
  streamPulseSec: number;
  pulseRingRgb: string;
  chartIdleCopy: ChartIdleCopy | null;
};

/* ══════════════════════════════════════════════════════════════════
   StreamProLayout — polished broadcast page (?stream=6&fit=1)
   ══════════════════════════════════════════════════════════════════ */

export function StreamProLayout(props: StreamProLayoutProps) {
  const {
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
    runPnlSeries,
    runUsdYDomain,
    runUsdSessionStats,
    runUsdChartRefExtremes,
    botRunUptimeSec,
    windowSecondsLeftDisplay,
    windowElapsedPct,
    roundOutcomes,
    streamPulseSec,
    pulseRingRgb,
    chartIdleCopy,
  } = props;

  const runPnlSegments = useMemo(() => splitRunPnlSegments(runPnlSeries), [runPnlSeries]);
  const [showPnlBreakdown, setShowPnlBreakdown] = useState(false);

  const fb = fitBroadcast; // shorthand

  return (
    <div
      className={`stream-trade-root stream-trade-root--pro${fb ? " stream-broadcast-fit" : ""}`}
      style={{
        boxSizing: "border-box",
        ...(fb
          ? {
              height: "100dvh",
              maxHeight: "100dvh",
              width: "100%",
              maxWidth: "100%",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
              padding: "4px 8px 6px",
            }
          : {
              minHeight: "100vh",
              padding: "24px 32px 36px",
            }),
        background:
          "radial-gradient(ellipse 900px 420px at 50% -15%, rgba(251, 191, 36, 0.07), transparent 52%), radial-gradient(ellipse 600px 380px at 100% 40%, rgba(129, 140, 248, 0.06), transparent 45%), var(--bg)",
        color: "var(--text)",
        fontFamily: "var(--font-display)",
        maxWidth: fb ? "100%" : 1000,
        margin: fb ? 0 : "0 auto",
        borderLeft: fb ? "none" : "1px solid rgba(251, 191, 36, 0.2)",
        borderRight: fb ? "none" : "1px solid rgba(251, 191, 36, 0.2)",
      }}
    >
      <style>
        {`
          .pro-window-bar-fill--elapsed {
            background: linear-gradient(90deg, rgba(251, 191, 36, 0.35), #fbbf24 55%, rgba(252, 211, 77, 0.95));
            box-shadow: 0 0 14px rgba(251, 191, 36, 0.42);
          }
          .pro-history-round {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 7px 10px;
            border-radius: 9px;
            border: 1px solid rgba(251, 191, 36, 0.38);
            background: linear-gradient(135deg, rgba(15, 23, 42, 0.98), rgba(30, 41, 59, 0.55));
            margin-bottom: 5px;
            font-variant-numeric: tabular-nums;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04), 0 4px 14px rgba(0, 0, 0, 0.35);
          }
          .pro-history-dot {
            width: 11px;
            height: 11px;
            border-radius: 50%;
            flex-shrink: 0;
            border: 2px solid rgba(255, 255, 255, 0.35);
            box-shadow: 0 0 10px currentColor;
          }
          .pro-run-chart-shell {
            border-radius: var(--radius-md);
            border: 1px solid rgba(251, 191, 36, 0.22);
            background:
              radial-gradient(ellipse 120% 80% at 50% 0%, rgba(251, 191, 36, 0.07), transparent 55%),
              linear-gradient(180deg, rgba(15, 23, 42, 0.5), rgba(15, 23, 42, 0.92));
            box-shadow: 0 0 32px rgba(251, 191, 36, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.05);
          }
          .pro-winrate-gold {
            animation: proWinrateGold 1.1s ease-in-out infinite;
            border-color: rgba(251, 191, 36, 0.75) !important;
            background: linear-gradient(135deg, rgba(251, 191, 36, 0.12), rgba(15, 23, 42, 0.95)) !important;
          }
          @keyframes proWinrateGold {
            0%, 100% { filter: brightness(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.45); }
            50% { filter: brightness(1.12); box-shadow: 0 0 28px 4px rgba(251, 191, 36, 0.55); }
          }
          @keyframes proLiveDot {
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
          .pro-live-dot {
            width: 11px;
            height: 11px;
            border-radius: 999px;
            background: linear-gradient(145deg, #6ee7b7, #22c55e);
            flex-shrink: 0;
            animation: proLiveDot 1.4s ease-in-out infinite;
          }
          @keyframes proIdleSpin {
            to { transform: rotate(360deg); }
          }
          .pro-idle-spinner {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            border: 3px solid rgba(110, 231, 183, 0.12);
            border-top-color: #6ee7b7;
            border-right-color: rgba(147, 169, 201, 0.45);
            animation: proIdleSpin 0.72s linear infinite;
            box-shadow: 0 0 18px rgba(52, 211, 153, 0.2);
          }
          .pro-idle-spinner--sm {
            width: 30px;
            height: 30px;
            border-width: 2px;
          }
          @keyframes proPulseRing {
            0%, 100% { transform: scale(1); opacity: 0.38; }
            50% { transform: scale(1.07); opacity: 0.92; }
          }
          .pro-pulse-orb {
            position: relative;
            width: ${fb ? 100 : 140}px;
            height: ${fb ? 100 : 140}px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            flex-shrink: 0;
          }
          .pro-pulse-orb--embedded {
            width: ${fb ? 92 : 120}px;
            height: ${fb ? 92 : 120}px;
          }
          .pro-pulse-orb--embedded::before {
            inset: -6px !important;
          }
          .pro-pulse-orb::before {
            content: "";
            position: absolute;
            inset: ${fb ? "-8px" : "-12px"};
            border-radius: 50%;
            border: 2px solid rgba(var(--pulse-rgb), 0.52);
            animation: proPulseRing var(--pulse-sec, 2.8s) ease-in-out infinite;
            pointer-events: none;
          }
          .pro-window-bar {
            height: 6px;
            border-radius: 999px;
            background: rgba(148, 163, 184, 0.2);
            overflow: hidden;
            margin-top: 8px;
          }
          .pro-window-bar-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, rgba(52, 211, 153, 0.35), #34d399);
            transition: width 0.6s ease-out;
          }
        `}
      </style>

      {/* ── HEADER ── */}
      <header
        style={{
          marginBottom: fb ? 4 : 24,
          flexShrink: 0,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 12,
        }}
      >
        <div>
          <div
            role="status"
            aria-label="Live market data"
            style={{
              fontSize: 12,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <span className="pro-live-dot" title="Online — live updates" />
            <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>Market</span>
            <span
              style={{
                fontSize: 10,
                fontWeight: 800,
                letterSpacing: "0.18em",
                color: "#fde68a",
                padding: "3px 8px",
                borderRadius: 999,
                background: "linear-gradient(135deg, rgba(251, 191, 36, 0.22), rgba(120, 53, 15, 0.35))",
                border: "1px solid rgba(251, 191, 36, 0.55)",
                boxShadow: "0 0 14px rgba(251, 191, 36, 0.25), inset 0 1px 0 rgba(255,255,255,0.08)",
              }}
            >
              LIVE
            </span>
            <span
              style={{
                fontSize: 10,
                fontWeight: 800,
                letterSpacing: "0.12em",
                color: "#fbbf24",
                padding: "3px 8px",
                borderRadius: 999,
                border: "1px solid rgba(251, 191, 36, 0.5)",
                background: "linear-gradient(135deg, rgba(251, 191, 36, 0.12), rgba(15, 23, 42, 0.9))",
              }}
            >
              PRO
            </span>
          </div>
          <h1 style={{ margin: "6px 0 0", fontSize: fb ? 20 : 23, fontWeight: 600, lineHeight: 1.25 }}>{market?.title ?? "Loading…"}</h1>
          {!fb && (
            <p style={{ margin: "6px 0 0", fontSize: 12, color: "var(--muted)", fontWeight: 500, maxWidth: 52 * 16, lineHeight: 1.45 }}>
              Professional broadcast overlay — run P&amp;L, session clock, round history. Use <code style={{ color: "var(--accent-bright)" }}>?stream=6</code> or <code style={{ color: "var(--accent-bright)" }}>/stream/pro</code>.
            </p>
          )}
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
          <label style={{ fontSize: 12, color: "var(--muted)", display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}>
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
            Entry &amp; exit sounds
          </label>
          {entrySoundOn ? (
            <div style={{ textAlign: "right", maxWidth: 280 }}>
              {!audioUnlocked ? (
                <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 4, lineHeight: 1.35 }}>
                  Click or tap anywhere once — browsers block audio until you do.
                </div>
              ) : null}
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, justifyContent: "flex-end" }}>
                <button
                  type="button"
                  onClick={async () => {
                    const ok = await resumeStreamAudio();
                    if (ok) setAudioUnlocked(true);
                    playEntryChime();
                  }}
                  style={{ fontSize: 11, fontWeight: 600, padding: "4px 10px", borderRadius: 7, background: "var(--bg-elevated)", border: "1px solid rgba(52, 211, 153, 0.5)", color: "var(--text)", cursor: "pointer" }}
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
                  style={{ fontSize: 11, fontWeight: 600, padding: "4px 10px", borderRadius: 7, background: "var(--bg-elevated)", border: "1px solid rgba(251, 146, 60, 0.45)", color: "var(--text)", cursor: "pointer" }}
                >
                  Test exit ▼
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </header>

      {/* ── QR hero (fit broadcast): גדול למעלה, בלי "שטח מת" למטה — */}
      {fb ? (
        <div
          style={{
            display: "flex",
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "flex-start",
            gap: 14,
            flexWrap: "wrap",
            marginBottom: 6,
            padding: "10px 12px",
            borderRadius: 12,
            border: "2px solid rgba(251, 191, 36, 0.55)",
            background:
              "linear-gradient(135deg, rgba(251, 191, 36, 0.14), rgba(15, 23, 42, 0.97)), radial-gradient(ellipse 80% 120% at 0% 50%, rgba(251, 191, 36, 0.12), transparent 50%)",
            boxShadow: "0 0 28px rgba(251, 191, 36, 0.18), inset 0 1px 0 rgba(255,255,255,0.08)",
          }}
        >
          <QRCodeSVG
            value="https://t.me/roller000"
            size={92}
            bgColor="#ffffff"
            fgColor="#0f172a"
            level="M"
            style={{ borderRadius: 8, flexShrink: 0, boxShadow: "0 4px 18px rgba(0,0,0,0.35)" }}
            role="img"
            aria-label="QR code — Telegram @roller000"
          />
          <div style={{ flex: "1 1 200px", minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 900, letterSpacing: "0.04em", color: "#fef3c7", textShadow: "0 0 16px rgba(251,191,36,0.4)" }}>Scan · Telegram</div>
            <div style={{ fontSize: 13, fontWeight: 700, marginTop: 2 }}>
              <a
                href="https://t.me/roller000"
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: "#fbbf24", textDecoration: "underline", textUnderlineOffset: 3 }}
              >
                t.me/roller000
              </a>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4, lineHeight: 1.35 }}>
              Suggestions / collaboration — code is sharp; camera grabs it instantly.
            </div>
          </div>
        </div>
      ) : null}

      {/* ── BROADCAST FIT WRAPPER ── */}
      <BroadcastFit enabled={fb} parentRef={broadcastParentRef} contentRef={broadcastContentRef}>
        {err ? (
          <div style={{ padding: 10, borderRadius: 8, background: "var(--down-muted)", color: "var(--text)", marginBottom: fb ? 8 : 20 }}>{err}</div>
        ) : null}

        {exitBanner ? (
          <div
            role="status"
            aria-live="polite"
            style={{
              marginBottom: fb ? 8 : 20,
              padding: "10px 14px",
              borderRadius: 10,
              border: "1px solid rgba(52, 211, 153, 0.55)",
              background: "linear-gradient(135deg, rgba(52, 211, 153, 0.14), rgba(15, 23, 42, 0.96))",
              fontSize: 14,
              fontWeight: 650,
              color: "var(--text)",
              lineHeight: 1.45,
              boxShadow: "0 0 28px rgba(52, 211, 153, 0.22), inset 0 1px 0 rgba(255,255,255,0.06)",
            }}
          >
            {exitBanner}
          </div>
        ) : null}

        {/* ── MODE + RUN WINDOW (עיגול unrealized בתוך כרטיס הזמן — בלי חפיפה לריבוע השכן) ── */}
        <section
          aria-label="Session clock and market window"
          style={{
            marginBottom: fb ? 4 : 22,
            display: "flex",
            flexWrap: "wrap",
            gap: fb ? 6 : 16,
            alignItems: "stretch",
            justifyContent: "space-between",
          }}
        >
          <div style={{ flex: "2 1 260px", minWidth: 0 }}>
            <div
              style={{
                padding: fb ? "10px 12px" : "18px 20px",
                borderRadius: 12,
                border: `1px solid ${moodStyle.border}`,
                background: moodStyle.bg,
                boxShadow: moodStyle.shadow,
              }}
            >
              <div style={{ fontSize: 10, letterSpacing: "0.14em", textTransform: "uppercase", color: "var(--muted)", marginBottom: fb ? 4 : 6 }}>Bot mode</div>
              <div
                style={{
                  fontSize: fb ? "clamp(20px, 3.6vw, 28px)" : "clamp(26px, 4.5vw, 34px)",
                  fontWeight: 900,
                  letterSpacing: "0.08em",
                  lineHeight: 1.1,
                  color: moodStyle.color,
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {streamMood.label}
                {open && agg ? (
                  <span style={{ letterSpacing: "0.06em" }}>
                    {" "}
                    · {agg.side}
                  </span>
                ) : null}
              </div>
              <div style={{ fontSize: fb ? 12 : 13, color: "var(--text-secondary)", marginTop: fb ? 4 : 8, lineHeight: 1.4 }}>{streamMood.hint}</div>
              {showHotStreak ? (
                <div style={{ marginTop: fb ? 6 : 8, fontSize: fb ? 11 : 12, fontWeight: 700, color: "#fbbf24", display: "flex", alignItems: "center", gap: 6 }}>
                  Hot streak — {winRatePct != null ? `${winRatePct.toFixed(0)}%` : "—"} win rate ({winRateWins}/{winRateExits} exits)
                </div>
              ) : null}
            </div>
          </div>

          <div style={{ flex: "1.65 1 280px", minWidth: 0 }}>
            <div
              style={{
                height: "100%",
                minHeight: fb ? 0 : 140,
                padding: fb ? "10px 10px 10px 12px" : "16px 18px",
                borderRadius: 12,
                border: "1px solid rgba(251, 191, 36, 0.42)",
                background:
                  "linear-gradient(160deg, rgba(15, 23, 42, 0.96), rgba(30, 41, 59, 0.4)), radial-gradient(ellipse 180% 90% at 100% 0%, rgba(251, 191, 36, 0.12), transparent 52%)",
                boxShadow: "0 0 24px rgba(251, 191, 36, 0.12), inset 0 1px 0 rgba(255,255,255,0.05)",
                display: "flex",
                flexDirection: "row",
                alignItems: "stretch",
                gap: fb ? 8 : 12,
                overflow: "hidden",
              }}
            >
              <div style={{ flex: "1 1 120px", minWidth: 0, display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
                <div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
                    <span style={{ fontSize: 10, letterSpacing: "0.14em", textTransform: "uppercase", color: "#fde68a", fontWeight: 800 }}>Run time</span>
                    <span
                      title="Wall time since semi/auto was enabled"
                      style={{
                        fontSize: fb ? "clamp(16px, 2.8vw, 22px)" : "clamp(20px, 3.5vw, 26px)",
                        fontWeight: 900,
                        fontVariantNumeric: "tabular-nums",
                        color: "#fbbf24",
                        textShadow: "0 0 18px rgba(251, 191, 36, 0.45)",
                      }}
                    >
                      {stratCfg?.mode === "off" ? "—" : botRunUptimeSec != null ? formatBotUptime(botRunUptimeSec) : "—"}
                    </span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                    <span style={{ fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--muted)", fontWeight: 700 }}>Market window</span>
                    <span style={{ fontSize: fb ? 17 : 20, fontWeight: 800, fontVariantNumeric: "tabular-nums", color: "#6ee7b7" }}>
                      {market && windowSecondsLeftDisplay != null
                        ? formatTimeLeft(windowSecondsLeftDisplay)
                        : "—"}
                    </span>
                  </div>
                  <div style={{ fontSize: fb ? 11 : 12, color: "var(--text-secondary)", marginTop: 4, lineHeight: 1.3 }}>Time left · bar = elapsed %</div>
                </div>
                <div className="pro-window-bar" aria-hidden style={{ marginTop: 6 }}>
                  <div className="pro-window-bar-fill pro-window-bar-fill--elapsed" style={{ width: `${windowElapsedPct}%` }} />
                </div>
                <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 6 }}>
                  {windowLabel(market)} · {windowElapsedPct.toFixed(0)}% elapsed
                </div>
              </div>

              <div
                style={{
                  flex: "0 0 auto",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  paddingInlineStart: 4,
                  borderInlineStart: "1px solid rgba(251, 191, 36, 0.22)",
                  alignSelf: "center",
                }}
              >
                <div
                  className="pro-pulse-orb pro-pulse-orb--embedded"
                  role="img"
                  aria-label="Unrealized PnL"
                  style={{ "--pulse-rgb": pulseRingRgb, "--pulse-sec": `${streamPulseSec}s` } as CSSProperties}
                >
                  <div style={{ position: "relative", zIndex: 1, textAlign: "center", padding: fb ? "4px 4px" : "8px 6px" }}>
                    <div
                      style={{
                        fontSize: fb ? 8 : 10,
                        letterSpacing: "0.04em",
                        textTransform: "uppercase",
                        color: "var(--muted)",
                        fontWeight: 600,
                        marginBottom: 2,
                        lineHeight: 1.2,
                        maxWidth: 88,
                      }}
                    >
                      Unrealized
                    </div>
                    <div
                      style={{
                        fontSize: fb ? 18 : 24,
                        fontWeight: 900,
                        fontVariantNumeric: "tabular-nums",
                        color: livePct == null ? "var(--muted)" : livePct >= 0 ? "var(--up)" : "var(--down)",
                        lineHeight: 1.1,
                      }}
                    >
                      {livePct != null ? `${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)}%` : "—"}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ── STAT GRID ── */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: fb ? "repeat(auto-fill, minmax(148px, 1fr))" : "repeat(auto-fill, minmax(180px, 1fr))",
            gap: fb ? 6 : 16,
            marginBottom: fb ? 4 : 24,
          }}
        >
          <StreamBlock
            label="Avg entry"
            value={open && agg && agg.avgEntryCents > 0 ? `${agg.avgEntryCents.toFixed(1)}¢` : "—"}
            compact={fb}
          />
          <StreamBlock
            label="Unrealized %"
            sub="This market window"
            value={livePct != null ? `${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)}%` : "—"}
            valueColor={pnlColor}
            large
            compact={fb}
          />
          <StreamBlock
            label="Run P&amp;L"
            sub="Net since bot run started"
            value={runPnlUsd != null && Number.isFinite(runPnlUsd) ? formatUsdSigned(runPnlUsd) : "—"}
            valueColor={
              runPnlUsd == null || !Number.isFinite(runPnlUsd)
                ? "var(--muted)"
                : runPnlUsd >= 0
                  ? "var(--up)"
                  : "var(--down)"
            }
            large
            compact={fb}
          />
          <div
            className={winRateHot ? "pro-winrate-gold" : undefined}
            role="status"
            style={{
              width: "100%",
              minWidth: 0,
              minHeight: fb ? 100 : 128,
              boxSizing: "border-box",
              padding: fb ? "10px 10px" : "14px 12px",
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
            <div style={{ fontSize: 10, letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--muted)", fontWeight: 700, lineHeight: 1.2, marginBottom: 4 }}>
              Win rate
              <br />
              <span style={{ fontSize: 9, letterSpacing: "0.04em", fontWeight: 600, opacity: 0.92 }}>this bot run</span>
            </div>
            <div
              style={{
                fontSize: fb ? 22 : 26,
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
              {stratCfg?.mode === "off" ? "—" : winRateExits === 0 || winRatePct == null ? "—" : `${winRatePct.toFixed(1)}%`}
            </div>
            <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 4, lineHeight: 1.3, maxWidth: 180 }}>
              {stratCfg?.mode === "off" ? "Bot off" : winRateExits === 0 ? "No exits yet" : winRatePct != null ? `${winRateWins} / ${winRateExits} winning exits` : "—"}
            </div>
          </div>
        </div>

        {/* ── LIVE PRICE PILLS ── */}
        <div
          role="group"
          aria-label="Market mid prices"
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: fb ? 5 : 14,
            marginBottom: fb ? 4 : 24,
            justifyContent: "center",
            alignItems: "stretch",
          }}
        >
          <LiveCentsPill label="Up" mid={orderbook?.up?.mid ?? null} accent="up" flatMarket={!open} compact={fb} />
          <LiveCentsPill label="Down" mid={orderbook?.down?.mid ?? null} accent="down" flatMarket={!open} compact={fb} />
        </div>

        {/* ── CHART + ROUND HISTORY ── */}
        <section id="stream-stats" aria-label="Run P&amp;L and round history">
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", justifyContent: "space-between", gap: 10, marginBottom: 6 }}>
            <div>
              <div style={{ fontSize: 12, letterSpacing: "0.06em", textTransform: "uppercase", color: "#fde68a", fontWeight: 800 }}>Cumulative run P&amp;L (USD)</div>
              {!fb && (
                <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 3, maxWidth: 480, lineHeight: 1.35 }}>
                  Net equity vs. when semi/auto was enabled — live points append in real time.
                </div>
              )}
            </div>
            {runUsdSessionStats && runPnlSeries.length > 0 ? (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 10,
                  fontSize: 12,
                  fontVariantNumeric: "tabular-nums",
                  color: "var(--text-secondary)",
                  justifyContent: "flex-end",
                }}
              >
                <span>
                  <span style={{ color: "#fbbf24", fontWeight: 800, marginRight: 4 }}>▲ High</span>
                  {formatUsdSigned(runUsdSessionStats.maxUsd)}
                </span>
                <span aria-hidden style={{ color: "var(--border)", userSelect: "none" }}>·</span>
                <span>
                  <span style={{ color: "#f87171", fontWeight: 800, marginRight: 4 }}>▼ Low</span>
                  {formatUsdSigned(runUsdSessionStats.minUsd)}
                </span>
                <span aria-hidden style={{ color: "var(--border)", userSelect: "none" }}>·</span>
                <span>
                  <span style={{ color: runUsdSessionStats.last >= 0 ? RUN_PNL_GREEN : RUN_PNL_RED, fontWeight: 800, marginRight: 4 }}>● Now</span>
                  {formatUsdSigned(runUsdSessionStats.last)}
                </span>
              </div>
            ) : null}
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: fb ? 5 : 12, alignItems: "stretch" }}>
            <div
              className="pro-run-chart-shell"
              style={{
                flex: "3 1 300px",
                minWidth: 0,
                width: "100%",
                height: fb ? 236 : 320,
                overflow: "hidden",
              }}
            >
              {stratCfg?.mode === "off" && chartIdleCopy ? (
                <div style={{ height: "100%", color: "var(--text-secondary)" }}>
                  <ChartIdlePanel copy={chartIdleCopy} />
                </div>
              ) : stratCfg?.mode !== "off" && runPnlSeries.length === 0 ? (
                <div
                  style={{
                    height: "100%",
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 12,
                    padding: 12,
                  }}
                >
                  <div className="pro-idle-spinner pro-idle-spinner--sm" aria-hidden />
                  <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-secondary)" }}>
                    {runPnlUsd == null ? "Waiting for equity baseline…" : "Building the run P&amp;L curve…"}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--muted)" }}>Sampling starts as soon as data flows.</div>
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={runPnlSeries} margin={{ top: 8, right: 6, bottom: 4, left: 4 }}>
                    <CartesianGrid stroke="var(--border)" strokeOpacity={0.3} strokeDasharray="2 8" vertical={false} />
                    <XAxis
                      dataKey="t"
                      type="number"
                      domain={["dataMin", "dataMax"]}
                      tick={{ fill: "var(--muted)", fontSize: 10 }}
                      tickFormatter={(v) => israelTime(Number(v))}
                    />
                    <YAxis
                      dataKey="usd"
                      domain={runUsdYDomain}
                      tick={{ fill: "var(--muted)", fontSize: 10 }}
                      tickFormatter={(v) => formatUsdSigned(Number(v))}
                      width={68}
                      tickCount={7}
                    />
                    <Tooltip
                      cursor={{ stroke: "rgba(248, 250, 252, 0.35)", strokeWidth: 1 }}
                      content={({ active, payload }) => {
                        if (!active || !payload || payload.length === 0) return null;
                        // payload[0].payload is the data point from the segment
                        const pt = payload[0].payload as RunPnlPoint;
                        if (!pt || pt.t == null) return null;
                        const v = pt.usd;
                        const c = v >= 0 ? RUN_PNL_GREEN : RUN_PNL_RED;
                        return (
                          <div
                            style={{
                              background: "var(--chart-tooltip-bg)",
                              border: "1px solid var(--chart-tooltip-border)",
                              borderRadius: 8,
                              fontSize: 12,
                              padding: "6px 10px",
                            }}
                          >
                            <div style={{ color: "var(--muted)", marginBottom: 3 }}>
                              {israelTime(pt.t)}
                            </div>
                            <div style={{ color: c, fontWeight: 700 }}>{formatUsdSigned(v)}</div>
                            <div style={{ color: "var(--muted)", fontSize: 10, marginTop: 3 }}>Run P&amp;L</div>
                          </div>
                        );
                      }}
                    />
                    <ReferenceLine y={0} stroke="var(--border-strong)" strokeDasharray="4 4" />
                    {runUsdChartRefExtremes && runPnlSeries.length > 1 && runUsdChartRefExtremes.maxUsd !== runUsdChartRefExtremes.minUsd ? (
                      <>
                        <ReferenceLine y={runUsdChartRefExtremes.maxUsd} stroke="#fbbf24" strokeDasharray="5 5" strokeOpacity={0.55} />
                        <ReferenceLine y={runUsdChartRefExtremes.minUsd} stroke="#f87171" strokeDasharray="5 5" strokeOpacity={0.55} />
                      </>
                    ) : null}
                    {runPnlSegments.map((seg, i) => (
                      <Area
                        key={`pro-pnl-area-${i}`}
                        type="monotone"
                        data={seg.data}
                        dataKey="usd"
                        stroke="none"
                        fill={seg.fill}
                        fillOpacity={0.2}
                        baseLine={0}
                        isAnimationActive={false}
                      />
                    ))}
                    {runPnlSegments.map((seg, i) => (
                      <Line
                        key={`pro-pnl-line-${i}`}
                        type="monotone"
                        data={seg.data}
                        dataKey="usd"
                        stroke={seg.stroke}
                        strokeWidth={3}
                        dot={false}
                        isAnimationActive={false}
                        activeDot={{ r: 5, strokeWidth: 0, fill: seg.stroke }}
                      />
                    ))}
                  </ComposedChart>
                </ResponsiveContainer>
              )}
            </div>

            {/* Round history sidebar */}
            <aside
              aria-label="Round history"
              style={{
                flex: "1 1 200px",
                minWidth: 170,
                maxWidth: 300,
                maxHeight: fb ? 236 : 320,
                overflow: "auto",
                padding: "10px 12px",
                borderRadius: 10,
                border: "1px solid rgba(251, 191, 36, 0.4)",
                background:
                  "linear-gradient(165deg, rgba(15, 23, 42, 0.98), rgba(30, 27, 46, 0.92)), radial-gradient(ellipse 100% 80% at 0% 0%, rgba(251, 191, 36, 0.14), transparent 55%)",
                boxShadow: "0 0 28px rgba(251, 191, 36, 0.12), inset 0 1px 0 rgba(255,255,255,0.06)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                <div style={{ fontSize: 10, letterSpacing: "0.16em", textTransform: "uppercase", color: "#fde68a", fontWeight: 900 }}>Round history</div>
                <button
                  onClick={() => setShowPnlBreakdown((v) => !v)}
                  style={{
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    color: showPnlBreakdown ? "#fde68a" : "var(--muted)",
                    background: showPnlBreakdown ? "rgba(251,191,36,0.12)" : "rgba(255,255,255,0.05)",
                    border: showPnlBreakdown ? "1px solid rgba(251,191,36,0.45)" : "1px solid rgba(255,255,255,0.1)",
                    borderRadius: 5,
                    padding: "2px 7px",
                    cursor: "pointer",
                    transition: "all 0.18s",
                    lineHeight: 1.6,
                  }}
                  title={showPnlBreakdown ? "Hide P&L per trade" : "Show P&L per trade"}
                >
                  {showPnlBreakdown ? "Hide" : "Show"} P&L
                </button>
              </div>
              <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 8, lineHeight: 1.4 }}>
                One row per exit · this bot run only · newest first.
              </div>
              {roundOutcomes.length === 0 ? (
                <div style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 600 }}>No closed rounds yet.</div>
              ) : (
                roundOutcomes.map((r) => (
                  <div key={r.id} className="pro-history-round">
                    <span
                      className="pro-history-dot"
                      style={{ color: r.win ? "#34d399" : "#fb7185", background: r.win ? "#34d399" : "#fb7185" }}
                      title={r.win ? "Win" : "Loss"}
                      aria-hidden
                    />
                    <span
                      style={{
                        fontSize: 12,
                        fontWeight: 700,
                        color: "var(--text)",
                        flex: 1,
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        flexWrap: "wrap",
                        minWidth: 0,
                      }}
                    >
                      <span>
                        {r.startLabel === r.endLabel ? r.startLabel : `${r.startLabel} – ${r.endLabel}`}
                      </span>
                      {r.side ? (
                        <span
                          style={{
                            fontSize: 10,
                            fontWeight: 900,
                            letterSpacing: "0.1em",
                            color: r.side === "Up" ? "var(--up)" : "var(--down)",
                          }}
                        >
                          {r.side === "Up" ? "UP" : "DOWN"}
                        </span>
                      ) : null}
                    </span>
                    {showPnlBreakdown && (
                      <span
                        style={{
                          fontSize: 11,
                          fontWeight: 800,
                          fontVariantNumeric: "tabular-nums",
                          color: r.pnlUsd == null ? "var(--muted)" : r.win ? "#34d399" : "#fb7185",
                          background: r.pnlUsd == null ? "transparent" : r.win ? "rgba(52,211,153,0.1)" : "rgba(251,113,133,0.1)",
                          borderRadius: 5,
                          padding: "1px 5px",
                          minWidth: 44,
                          textAlign: "right",
                        }}
                      >
                        {r.pnlUsd == null
                          ? "—"
                          : `${r.pnlUsd >= 0 ? "+" : ""}${r.pnlUsd.toFixed(2)}$`}
                      </span>
                    )}
                  </div>
                ))
              )}
            </aside>
          </div>
        </section>

        {/* ── QR (במצב fit כבר למעלה; כאן רק כשלא fit) ── */}
        {!fb ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 14,
              padding: "14px 18px",
              marginTop: 20,
              borderRadius: 10,
              border: "1px solid rgba(251, 191, 36, 0.25)",
              background: "linear-gradient(135deg, rgba(15, 23, 42, 0.96), rgba(30, 41, 59, 0.45))",
              boxShadow: "0 0 18px rgba(251, 191, 36, 0.06), inset 0 1px 0 rgba(255,255,255,0.04)",
            }}
          >
            <QRCodeSVG
              value="https://t.me/roller000"
              size={72}
              bgColor="#ffffff"
              fgColor="#0f172a"
              level="M"
              style={{ borderRadius: 5, flexShrink: 0 }}
              role="img"
              aria-label="QR code — Telegram @roller000"
            />
            <div style={{ flex: 1, minWidth: 120 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 2 }}>
                Suggestions or collaboration?
              </div>
              <div style={{ fontSize: 12, color: "var(--muted)", lineHeight: 1.45 }}>
                Telegram:{" "}
                <a
                  href="https://t.me/roller000"
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: "#fbbf24", fontWeight: 700, textDecoration: "none" }}
                >
                  @roller000
                </a>
              </div>
            </div>
          </div>
        ) : null}
      </BroadcastFit>
    </div>
  );
}
