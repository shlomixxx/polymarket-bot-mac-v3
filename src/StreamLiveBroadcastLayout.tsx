import type { CSSProperties, RefObject, ReactNode } from "react";
import { useMemo, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { playEntryChime, playExitChime, resumeStreamAudio } from "./streamAudio";
import type { RoundOutcomeRow } from "./StreamSpectatorLayout";
import { israelDate, israelHM, israelTime, ISRAEL_TZ_LABEL } from "./timeFormat";

/* ── local types ── */

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

/* ── trigger/bot activity (broadcast-only subset of /api/trigger/state) ── */
export type StreamTriggerMode = "off" | "momentum" | "signal" | "dca_pulse";
export type StreamTriggerState = {
  active: boolean;
  mode: StreamTriggerMode;
  status: string;
  status_log: { ts: number; msg: string }[];
  cooldown_remaining: number | null;
  dca_running?: boolean;
  config?: {
    dca_pulse_slices?: number;
    dca_pulse_direction?: "Up" | "Down";
    momentum_direction?: "Up" | "Down" | "auto";
    signal_direction?: "Up" | "Down" | "auto";
  };
};

/* ── helpers ── */

function formatUsdSigned(v: number): string {
  const a = Math.abs(v);
  return `${v >= 0 ? "+" : "-"}$${a.toFixed(2)}`;
}

const RUN_PNL_GREEN = "#34d399";
const RUN_PNL_RED = "#fb7185";

type RunPnlPoint = { t: number; usd: number };

function splitRunPnlSegments(
  points: RunPnlPoint[]
): { stroke: string; fill: string; data: RunPnlPoint[] }[] {
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

function formatTimeLeft(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** זמן ריצה מאז הפעלת semi/auto — כמו ב־StreamProLayout */
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

function nowDateLabel(): string {
  return israelDate(Date.now() / 1000);
}

function nowTimeWindowLabel(market: Market | null): string {
  if (!market) return "";
  const windowSec = market.window_sec ?? 300;
  const secsLeft = market.seconds_left ?? 0;
  const endTs = Date.now() + secsLeft * 1000;
  const startTs = endTs - windowSec * 1000;
  return `${israelHM(startTs / 1000)} – ${israelHM(endTs / 1000)} (${ISRAEL_TZ_LABEL})`;
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
      style={{
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
        position: "relative",
        width: "100%",
      }}
    >
      <div
        ref={contentRef}
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          minHeight: 0,
          transformOrigin: "top center",
        }}
      >
        {children}
      </div>
    </div>
  );
}

/* ── props type ── */

export type StreamLiveBroadcastLayoutProps = {
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
  windowSecondsLeftDisplay: number | null;
  windowElapsedPct: number;
  roundOutcomes: RoundOutcomeRow[];
  streamPulseSec: number;
  pulseRingRgb: string;
  chartIdleCopy: ChartIdleCopy | null;
  /** האם הבוט רץ על כסף אמיתי (Polymarket CLOB) — מחליף את כל המספרים בנתוני החשבון האמיתי */
  isLive?: boolean;
  /** יתרת USDC לייב ב-Polymarket (equity כולל פוזיציות פתוחות) — זמין רק כ-isLive=true */
  liveAccountUsd?: number | null;
  /** יתרת דמו — מוצג כש-isLive=false */
  demoBalanceUsd?: number | null;
  /** מצב בוט הטריגר/DCA — מוזן ע"י polling של /api/trigger/state */
  triggerState?: StreamTriggerState | null;
};

/* ══════════════════════════════════════════════════════════════════
   StreamLiveBroadcastLayout — cinematic broadcast overlay (?stream=7)
   ══════════════════════════════════════════════════════════════════ */

export function StreamLiveBroadcastLayout(
  props: StreamLiveBroadcastLayoutProps
) {
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
    winRatePct,
    winRateExits,
    winRateWins,
    runPnlUsd,
    runPnlSeries,
    runUsdYDomain,
    runUsdSessionStats,
    runUsdChartRefExtremes,
    windowSecondsLeftDisplay,
    botRunUptimeSec,
    roundOutcomes,
    chartIdleCopy,
    isLive = false,
    liveAccountUsd = null,
    demoBalanceUsd = null,
    triggerState = null,
  } = props;

  const botEngineOn =
    !!triggerState &&
    (triggerState.active === true ||
      (triggerState.mode && triggerState.mode !== "off"));
  const botRunning = stratCfg?.mode !== "off" || botEngineOn;

  /** מקפיא את הסדרה בזמן hover כדי שהעיגול/tooltip לא יקפצו כשמתווסף דגימה כל שנייה */
  const [frozenRunPnlSeries, setFrozenRunPnlSeries] = useState<RunPnlPoint[] | null>(null);
  const effectiveRunPnlSeries = frozenRunPnlSeries ?? runPnlSeries;

  const runPnlSegments = useMemo(
    () => splitRunPnlSegments(effectiveRunPnlSeries),
    [effectiveRunPnlSeries]
  );

  const [showPnl, setShowPnl] = useState(true);
  const [showBalance, setShowBalance] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return localStorage.getItem("streamShowBalance") === "1";
    } catch {
      return false;
    }
  });

  const fb = fitBroadcast;

  const pnlVal = runPnlUsd ?? 0;
  const pnlPositive = pnlVal >= 0;
  const wrDisplay =
    winRatePct != null && winRateExits > 0
      ? `${winRatePct.toFixed(0)}%`
      : "—";
  const timeDisplay =
    windowSecondsLeftDisplay != null
      ? formatTimeLeft(windowSecondsLeftDisplay)
      : "—";

  const botRunDisplay =
    botRunning && botRunUptimeSec != null
      ? formatBotUptime(botRunUptimeSec)
      : botRunning
        ? "—"
        : "OFFLINE";

  const lastTrades = useMemo(() => {
    return [...roundOutcomes];
  }, [roundOutcomes]);

  return (
    <div
      className="stream-trade-root stream-trade-root--broadcast"
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
              padding: "0",
            }
          : {
              minHeight: "100vh",
              padding: "0",
            }),
        background:
          "radial-gradient(ellipse 120% 60% at 50% 0%, rgba(251, 140, 0, 0.06), transparent 50%), " +
          "radial-gradient(ellipse 80% 100% at 0% 100%, rgba(251, 191, 36, 0.04), transparent 60%), " +
          "#060812",
        color: "#e8e6f0",
        fontFamily:
          "'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        maxWidth: fb ? "100%" : 1100,
        margin: fb ? 0 : "0 auto",
      }}
    >
      <style>{`
        @keyframes lbLivePulse {
          0%, 100% { opacity: 1; box-shadow: 0 0 8px 2px rgba(239, 68, 68, 0.7); }
          50% { opacity: 0.6; box-shadow: 0 0 16px 6px rgba(239, 68, 68, 0.4); }
        }
        @keyframes lbGlowPulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 0.8; }
        }
        @keyframes lbSlideIn {
          from { transform: translateY(8px); opacity: 0; }
          to { transform: translateY(0); opacity: 1; }
        }
        .lb-live-dot {
          width: 12px; height: 12px; border-radius: 50%;
          background: #ef4444; flex-shrink: 0;
          animation: lbLivePulse 1.5s ease-in-out infinite;
        }
        .lb-stat-box {
          padding: 12px 16px; border-radius: 10px; text-align: center;
          border: 1px solid rgba(251, 191, 36, 0.45);
          background: linear-gradient(160deg, rgba(15, 18, 30, 0.95), rgba(20, 25, 40, 0.85));
          box-shadow: 0 0 20px rgba(251, 191, 36, 0.12), inset 0 1px 0 rgba(255,255,255,0.04);
          min-width: 120px;
        }
        .lb-stat-label {
          font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase;
          color: rgba(251, 191, 36, 0.85); font-weight: 800; margin-bottom: 6px;
        }
        .lb-stat-value {
          font-size: 28px; font-weight: 900; font-variant-numeric: tabular-nums; line-height: 1.1;
        }
        .lb-side-card {
          flex: 1 1 160px; padding: 16px 20px; border-radius: 14px; text-align: center;
          font-weight: 900; min-width: 140px; max-width: 300px;
          transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
        .lb-side-card:hover { transform: scale(1.02); }
        .lb-trade-row {
          display: flex; align-items: center; gap: 10px;
          padding: 8px 12px; border-radius: 8px;
          border: 1px solid rgba(251, 191, 36, 0.2);
          background: linear-gradient(135deg, rgba(15, 18, 30, 0.95), rgba(25, 30, 50, 0.7));
          margin-bottom: 5px; font-variant-numeric: tabular-nums;
          animation: lbSlideIn 0.3s ease-out;
        }
        .lb-trade-dot {
          width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
          box-shadow: 0 0 8px currentColor;
        }
        .lb-chart-shell {
          border-radius: 10px;
          border: 1px solid rgba(251, 191, 36, 0.2);
          background: linear-gradient(180deg, rgba(10, 14, 25, 0.95), rgba(15, 20, 35, 0.9));
          box-shadow: 0 0 24px rgba(251, 191, 36, 0.06);
          overflow: hidden;
        }
        .lb-glow-line {
          position: absolute; left: 0; right: 0; height: 2px;
          background: linear-gradient(90deg, transparent, rgba(251, 191, 36, 0.5), rgba(239, 68, 68, 0.4), rgba(251, 191, 36, 0.5), transparent);
          animation: lbGlowPulse 3s ease-in-out infinite;
        }
        .lb-qr-card {
          display: flex; align-items: center; gap: 16px; padding: 14px 18px;
          border-radius: 14px;
          border: 2px solid rgba(251, 191, 36, 0.5);
          background: linear-gradient(135deg, rgba(10, 14, 25, 0.97), rgba(20, 25, 42, 0.85));
          box-shadow: 0 0 32px rgba(251, 191, 36, 0.15), inset 0 1px 0 rgba(255,255,255,0.06);
        }
        .lb-idle-spinner {
          width: 34px; height: 34px; border-radius: 50%;
          border: 3px solid rgba(110, 231, 183, 0.12);
          border-top-color: #6ee7b7;
          animation: proIdleSpin 0.72s linear infinite;
        }
        @keyframes proIdleSpin { to { transform: rotate(360deg); } }
        @keyframes lbPositionPulse {
          0%, 100% { box-shadow: 0 0 12px rgba(var(--lb-pos-rgb, 74, 222, 128), 0.15); }
          50% { box-shadow: 0 0 22px rgba(var(--lb-pos-rgb, 74, 222, 128), 0.3); }
        }
        .lb-position-active {
          animation: lbPositionPulse 2s ease-in-out infinite;
        }
        @keyframes lbBotPillPulse {
          0%, 100% { box-shadow: 0 0 22px rgba(251, 191, 36, 0.28), inset 0 1px 0 rgba(255,255,255,0.06); }
          50% { box-shadow: 0 0 34px rgba(251, 191, 36, 0.45), inset 0 1px 0 rgba(255,255,255,0.08); }
        }
        @keyframes lbTelegramBorder {
          0%, 100% {
            border-color: rgba(96, 165, 250, 0.55);
            box-shadow: 0 0 28px rgba(59, 130, 246, 0.22), 0 0 14px rgba(251, 191, 36, 0.18), inset 0 1px 0 rgba(255,255,255,0.08);
          }
          50% {
            border-color: rgba(251, 191, 36, 0.85);
            box-shadow: 0 0 44px rgba(251, 191, 36, 0.45), 0 0 22px rgba(96, 165, 250, 0.35), inset 0 1px 0 rgba(255,255,255,0.12);
          }
        }
        @keyframes lbChevronSlide {
          0% { transform: translateX(-4px); opacity: 0.3; }
          50% { transform: translateX(2px); opacity: 1; }
          100% { transform: translateX(-4px); opacity: 0.3; }
        }
        @keyframes lbRibbonShine {
          0% { background-position: -200% 0; }
          100% { background-position: 200% 0; }
        }
        .lb-telegram-card {
          position: relative;
          display: flex;
          align-items: center;
          gap: 18px;
          padding: 16px 20px;
          border-radius: 16px;
          border: 2px solid rgba(96, 165, 250, 0.55);
          background:
            linear-gradient(135deg, rgba(30, 58, 138, 0.35) 0%, rgba(10, 14, 25, 0.97) 55%, rgba(69, 26, 3, 0.4) 100%);
          overflow: hidden;
          animation: lbTelegramBorder 2.8s ease-in-out infinite;
        }
        .lb-telegram-card::before {
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(100deg,
            transparent 35%,
            rgba(251, 191, 36, 0.18) 50%,
            transparent 65%);
          background-size: 200% 100%;
          animation: lbRibbonShine 3.6s linear infinite;
          pointer-events: none;
        }
        .lb-telegram-ribbon {
          position: absolute;
          top: 26px;
          right: -34px;
          transform: rotate(45deg);
          transform-origin: center center;
          padding: 5px 38px;
          background: linear-gradient(90deg, #dc2626, #f97316);
          color: #fff;
          font-size: 12px;
          font-weight: 900;
          letter-spacing: 0.22em;
          text-transform: uppercase;
          text-shadow: 0 1px 2px rgba(0,0,0,0.5);
          box-shadow: 0 6px 18px rgba(220, 38, 38, 0.55), 0 0 0 1px rgba(255,255,255,0.18) inset;
          pointer-events: none;
          z-index: 3;
          white-space: nowrap;
        }
        .lb-telegram-chevron {
          display: inline-block;
          animation: lbChevronSlide 1.2s ease-in-out infinite;
          color: #fbbf24;
          font-weight: 900;
        }
        .lb-telegram-chevron.c2 { animation-delay: 0.15s; }
        .lb-telegram-chevron.c3 { animation-delay: 0.3s; }
        .lb-telegram-btn {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 8px 14px;
          margin-top: 8px;
          border-radius: 10px;
          background: linear-gradient(135deg, #3b82f6, #1d4ed8);
          color: #fff !important;
          text-decoration: none;
          font-weight: 900;
          font-size: 13px;
          letter-spacing: 0.05em;
          box-shadow: 0 4px 16px rgba(59, 130, 246, 0.45);
          border: 1px solid rgba(147, 197, 253, 0.5);
          transition: transform 0.18s ease, box-shadow 0.18s ease;
        }
        .lb-telegram-btn:hover {
          transform: translateY(-1px) scale(1.02);
          box-shadow: 0 6px 22px rgba(59, 130, 246, 0.6);
        }
      `}</style>

      <BroadcastFit
        enabled={fb}
        parentRef={broadcastParentRef}
        contentRef={broadcastContentRef}
      >
        <div
          style={{
            padding: fb ? "6px 12px 10px" : "20px 28px 28px",
            ...(fb
              ? {
                  height: "100%",
                  minHeight: 0,
                  boxSizing: "border-box",
                  display: "flex",
                  flexDirection: "column",
                }
              : {}),
          }}
        >
          <div
            style={fb ? { flexShrink: 0, width: "100%" } : undefined}
          >
          {/* ── LIVE NOW BANNER ── */}
          <div
            style={{
              position: "relative",
              textAlign: "center",
              padding: "10px 0 12px",
              marginBottom: fb ? 8 : 16,
            }}
          >
            <div className="lb-glow-line" style={{ top: 0 }} />
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 10,
                fontSize: 13,
                fontWeight: 800,
                letterSpacing: "0.12em",
                color: "#fca5a5",
                textTransform: "uppercase",
                flexWrap: "wrap",
                justifyContent: "center",
              }}
            >
              <span className="lb-live-dot" />
              LIVE NOW — Join before next trade closes
            </div>
            <div className="lb-glow-line" style={{ bottom: 0 }} />
          </div>

          {/* ── TITLE ── */}
          <div style={{ textAlign: "center", marginBottom: fb ? 10 : 18 }}>
            <h1
              style={{
                margin: 0,
                fontSize: fb ? 28 : 36,
                fontWeight: 900,
                letterSpacing: "0.06em",
                color: "#fff",
                textShadow: "0 0 40px rgba(255,255,255,0.15)",
              }}
            >
              LIVE TRADE –{" "}
              <span style={{ color: "#fbbf24" }}>BITCOIN</span>
            </h1>
            <div
              style={{
                fontSize: 13,
                color: "rgba(255,255,255,0.55)",
                marginTop: 4,
                fontWeight: 500,
                letterSpacing: "0.04em",
              }}
            >
              {nowDateLabel()} | {nowTimeWindowLabel(market)}
            </div>
            <div
              style={{
                marginTop: 10,
                display: "flex",
                justifyContent: "center",
                alignItems: "center",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <div
                title={
                  botRunning
                    ? "Wall time since the bot engine was enabled"
                    : "Bot engine is currently idle"
                }
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "7px 16px",
                  borderRadius: 999,
                  fontSize: fb ? 15 : 17,
                  fontWeight: 900,
                  letterSpacing: "0.1em",
                  fontVariantNumeric: "tabular-nums",
                  textTransform: "uppercase",
                  color: botRunning ? "#fde68a" : "rgba(255,255,255,0.4)",
                  background: botRunning
                    ? "linear-gradient(135deg, rgba(251, 191, 36, 0.18), rgba(234, 88, 12, 0.10))"
                    : "rgba(255,255,255,0.04)",
                  border: botRunning
                    ? "1.5px solid rgba(251, 191, 36, 0.55)"
                    : "1px solid rgba(255,255,255,0.12)",
                  boxShadow: botRunning
                    ? "0 0 22px rgba(251, 191, 36, 0.28), inset 0 1px 0 rgba(255,255,255,0.06)"
                    : "none",
                  animation: botRunning
                    ? "lbBotPillPulse 2.4s ease-in-out infinite"
                    : undefined,
                }}
              >
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    background: botRunning ? "#fbbf24" : "rgba(255,255,255,0.3)",
                    boxShadow: botRunning
                      ? "0 0 10px rgba(251, 191, 36, 0.9)"
                      : "none",
                    animation: botRunning
                      ? "lbLivePulse 1.6s ease-in-out infinite"
                      : undefined,
                    flexShrink: 0,
                  }}
                />
                <span>
                  {botRunning ? "BOT RUNNING" : "BOT OFFLINE"}
                </span>
                <span
                  style={{
                    color: botRunning ? "#ffffff" : "rgba(255,255,255,0.4)",
                    fontWeight: 900,
                    letterSpacing: "0.04em",
                    textShadow: botRunning
                      ? "0 0 14px rgba(251, 191, 36, 0.5)"
                      : "none",
                  }}
                >
                  {botRunDisplay}
                </span>
              </div>
            </div>
          </div>

          {err && (
            <div
              style={{
                padding: 10,
                borderRadius: 8,
                background: "rgba(239, 68, 68, 0.15)",
                border: "1px solid rgba(239, 68, 68, 0.4)",
                color: "#fca5a5",
                marginBottom: 12,
                fontSize: 13,
              }}
            >
              {err}
            </div>
          )}

          {exitBanner && (
            <div
              role="status"
              aria-live="polite"
              style={{
                marginBottom: fb ? 8 : 14,
                padding: "10px 14px",
                borderRadius: 10,
                border: "1px solid rgba(52, 211, 153, 0.55)",
                background:
                  "linear-gradient(135deg, rgba(52, 211, 153, 0.12), rgba(10, 14, 25, 0.96))",
                fontSize: 14,
                fontWeight: 650,
                color: "#e8e6f0",
                lineHeight: 1.45,
                boxShadow:
                  "0 0 28px rgba(52, 211, 153, 0.18), inset 0 1px 0 rgba(255,255,255,0.05)",
              }}
            >
              {exitBanner}
            </div>
          )}

          {/* ── POSITION STATUS + SOUND CONTROLS ── */}
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: fb ? 6 : 10,
              marginBottom: fb ? 8 : 14,
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            {/* Position status */}
            <div
              className={open && agg ? "lb-position-active" : undefined}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "8px 14px",
                borderRadius: 10,
                border: open && agg
                  ? `1.5px solid ${agg!.side === "Up" ? "rgba(74, 222, 128, 0.6)" : "rgba(251, 113, 133, 0.6)"}`
                  : "1px solid rgba(255,255,255,0.1)",
                background: open && agg
                  ? agg!.side === "Up"
                    ? "linear-gradient(135deg, rgba(22, 101, 52, 0.4), rgba(10, 14, 25, 0.95))"
                    : "linear-gradient(135deg, rgba(127, 29, 29, 0.4), rgba(10, 14, 25, 0.95))"
                  : "rgba(255,255,255,0.03)",
                boxShadow: open && agg
                  ? agg!.side === "Up"
                    ? "0 0 20px rgba(52, 211, 153, 0.15)"
                    : "0 0 20px rgba(251, 113, 133, 0.15)"
                  : "none",
                flex: "1 1 auto",
                minWidth: 0,
              }}
            >
              {open && agg ? (
                <>
                  <span
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: agg.side === "Up" ? "#4ade80" : "#fb7185",
                      boxShadow: `0 0 10px ${agg.side === "Up" ? "rgba(74,222,128,0.6)" : "rgba(251,113,133,0.6)"}`,
                      flexShrink: 0,
                    }}
                  />
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 800,
                      letterSpacing: "0.08em",
                      color: agg.side === "Up" ? "#4ade80" : "#fb7185",
                      textTransform: "uppercase",
                    }}
                  >
                    IN TRADE · {agg.side}
                  </span>
                  <span
                    style={{
                      fontSize: 12,
                      color: "rgba(255,255,255,0.5)",
                      fontWeight: 600,
                    }}
                  >
                    Entry: {agg.avgEntryCents > 0 ? `${agg.avgEntryCents.toFixed(1)}¢` : "—"}
                  </span>
                  {livePct != null && (
                    <span
                      style={{
                        fontSize: 12,
                        fontWeight: 800,
                        fontVariantNumeric: "tabular-nums",
                        color: pnlColor,
                      }}
                    >
                      {livePct >= 0 ? "+" : ""}{livePct.toFixed(2)}%
                    </span>
                  )}
                </>
              ) : (
                <>
                  <span
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: "rgba(255,255,255,0.2)",
                      flexShrink: 0,
                    }}
                  />
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 700,
                      letterSpacing: "0.06em",
                      color: "rgba(255,255,255,0.35)",
                      textTransform: "uppercase",
                    }}
                  >
                    NO OPEN POSITION
                  </span>
                </>
              )}
            </div>

            {/* ── BALANCE BADGE (click to reveal amount) ── */}
            <button
              type="button"
              onClick={() => {
                setShowBalance((v) => {
                  const next = !v;
                  try {
                    localStorage.setItem("streamShowBalance", next ? "1" : "0");
                  } catch { /* private mode */ }
                  return next;
                });
              }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "6px 12px",
                borderRadius: 10,
                border: isLive
                  ? "1.5px solid rgba(248, 113, 113, 0.7)"
                  : "1px solid rgba(148, 163, 184, 0.35)",
                background: isLive
                  ? "linear-gradient(135deg, rgba(127, 29, 29, 0.55), rgba(10, 14, 25, 0.95))"
                  : "rgba(255,255,255,0.04)",
                boxShadow: isLive
                  ? "0 0 14px rgba(248, 113, 113, 0.25)"
                  : "none",
                flexShrink: 0,
                cursor: "pointer",
                fontFamily: "inherit",
              }}
              title={showBalance ? "Click to hide balance" : "Click to show balance"}
            >
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: isLive ? "#f87171" : "#94a3b8",
                  boxShadow: isLive
                    ? "0 0 8px rgba(248, 113, 113, 0.8)"
                    : "none",
                  animation: isLive ? "lbLivePulse 1.6s ease-in-out infinite" : undefined,
                }}
              />
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 900,
                  letterSpacing: "0.14em",
                  color: isLive ? "#fca5a5" : "rgba(255,255,255,0.5)",
                }}
              >
                BALANCE
              </span>
              {showBalance && (
                <span
                  style={{
                    fontSize: 13,
                    fontWeight: 900,
                    fontVariantNumeric: "tabular-nums",
                    color: isLive ? "#fef2f2" : "rgba(255,255,255,0.75)",
                  }}
                >
                  {(() => {
                    const v = isLive ? liveAccountUsd : demoBalanceUsd;
                    if (typeof v !== "number" || !Number.isFinite(v)) return "—";
                    return `$${v.toFixed(2)}`;
                  })()}
                </span>
              )}
            </button>

            {/* Sound controls */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flexShrink: 0,
              }}
            >
              <label
                style={{
                  fontSize: 11,
                  color: "rgba(255,255,255,0.4)",
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                  cursor: "pointer",
                  userSelect: "none",
                }}
              >
                <input
                  type="checkbox"
                  checked={entrySoundOn}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setEntrySoundOn(on);
                    try {
                      localStorage.setItem("streamEntrySound", on ? "1" : "0");
                    } catch { /* private mode */ }
                    if (on) {
                      void resumeStreamAudio().then((ok) => {
                        if (ok) setAudioUnlocked(true);
                      });
                    }
                  }}
                  style={{ accentColor: "#fbbf24" }}
                />
                Sounds
              </label>
              {entrySoundOn && (
                <>
                  <button
                    type="button"
                    onClick={async () => {
                      const ok = await resumeStreamAudio();
                      if (ok) setAudioUnlocked(true);
                      playEntryChime();
                    }}
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      padding: "3px 8px",
                      borderRadius: 6,
                      background: "rgba(255,255,255,0.05)",
                      border: "1px solid rgba(74, 222, 128, 0.4)",
                      color: "#4ade80",
                      cursor: "pointer",
                    }}
                  >
                    ▲ Entry
                  </button>
                  <button
                    type="button"
                    onClick={async () => {
                      const ok = await resumeStreamAudio();
                      if (ok) setAudioUnlocked(true);
                      playExitChime();
                    }}
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      padding: "3px 8px",
                      borderRadius: 6,
                      background: "rgba(255,255,255,0.05)",
                      border: "1px solid rgba(251, 146, 60, 0.4)",
                      color: "#fb923c",
                      cursor: "pointer",
                    }}
                  >
                    ▼ Exit
                  </button>
                </>
              )}
              {entrySoundOn && !audioUnlocked && (
                <span style={{ fontSize: 9, color: "rgba(255,255,255,0.3)" }}>
                  click page to unlock audio
                </span>
              )}
            </div>
          </div>

          {/* ── HERO: QR + STATS ── */}
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: fb ? 8 : 14,
              marginBottom: fb ? 8 : 16,
              alignItems: "stretch",
            }}
          >
            {/* QR block — the single loudest CTA on the page */}
            <a
              href="https://t.me/roller000"
              target="_blank"
              rel="noopener noreferrer"
              className="lb-telegram-card"
              style={{
                flex: "1.4 1 340px",
                textDecoration: "none",
                color: "inherit",
                cursor: "pointer",
              }}
              aria-label="Open Telegram @roller000"
            >
              <div className="lb-telegram-ribbon">FREE · LIVE</div>
              <div
                style={{
                  position: "relative",
                  padding: 6,
                  borderRadius: 12,
                  background: "#ffffff",
                  boxShadow: "0 6px 30px rgba(251, 191, 36, 0.35), 0 0 0 2px rgba(251, 191, 36, 0.5)",
                  flexShrink: 0,
                }}
              >
                <QRCodeSVG
                  value="https://t.me/roller000"
                  size={fb ? 110 : 128}
                  bgColor="#ffffff"
                  fgColor="#0f172a"
                  level="M"
                  role="img"
                  aria-label="QR code — Telegram @roller000"
                />
                <div
                  style={{
                    position: "absolute",
                    bottom: -9,
                    left: "50%",
                    transform: "translateX(-50%)",
                    padding: "2px 10px",
                    borderRadius: 999,
                    background: "linear-gradient(90deg, #fbbf24, #f97316)",
                    fontSize: 9,
                    fontWeight: 900,
                    letterSpacing: "0.16em",
                    color: "#0b0f1a",
                    whiteSpace: "nowrap",
                    boxShadow: "0 2px 10px rgba(249, 115, 22, 0.5)",
                  }}
                >
                  SCAN ME
                </div>
              </div>
              <div style={{ flex: 1, minWidth: 0, position: "relative", zIndex: 1 }}>
                <div
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "3px 10px",
                    borderRadius: 999,
                    background: "rgba(220, 38, 38, 0.18)",
                    border: "1px solid rgba(248, 113, 113, 0.45)",
                    fontSize: 9,
                    fontWeight: 900,
                    letterSpacing: "0.18em",
                    color: "#fca5a5",
                    marginBottom: 6,
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: "#ef4444",
                      animation: "lbLivePulse 1.4s ease-in-out infinite",
                    }}
                  />
                  SIGNALS DROPPING NOW
                </div>
                <div
                  style={{
                    fontSize: fb ? 22 : 26,
                    fontWeight: 900,
                    letterSpacing: "0.02em",
                    color: "#fff",
                    lineHeight: 1.1,
                    textShadow: "0 0 24px rgba(251, 191, 36, 0.35)",
                  }}
                >
                  CHAT WITH ME
                  <br />
                  ON TELEGRAM
                </div>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    marginTop: 8,
                    fontSize: 13,
                    fontWeight: 800,
                    color: "#fbbf24",
                    letterSpacing: "0.02em",
                  }}
                >
                  <span className="lb-telegram-chevron">›</span>
                  <span className="lb-telegram-chevron c2">›</span>
                  <span className="lb-telegram-chevron c3">›</span>
                  <span style={{ color: "#60a5fa", fontSize: 16 }}>✈</span>
                  <span
                    style={{
                      color: "#fde68a",
                      fontWeight: 900,
                      letterSpacing: "0.04em",
                    }}
                  >
                    t.me/roller000
                  </span>
                </div>
                <div
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 6,
                    marginTop: 8,
                    padding: "7px 13px",
                    borderRadius: 10,
                    background: "linear-gradient(135deg, #3b82f6, #1d4ed8)",
                    color: "#fff",
                    fontWeight: 900,
                    fontSize: 13,
                    letterSpacing: "0.05em",
                    boxShadow: "0 4px 16px rgba(59, 130, 246, 0.45)",
                    border: "1px solid rgba(147, 197, 253, 0.5)",
                  }}
                >
                  JOIN NOW · FREE →
                </div>
              </div>
            </a>

            {/* Stat boxes */}
            <div
              style={{
                flex: "1 1 320px",
                display: "flex",
                gap: fb ? 6 : 10,
                alignItems: "stretch",
                flexWrap: "wrap",
              }}
            >
              <div className="lb-stat-box" style={{ flex: "1 1 100px" }}>
                <div className="lb-stat-label">SESSION PNL</div>
                <div
                  className="lb-stat-value"
                  style={{
                    color: pnlPositive ? "#4ade80" : "#fb7185",
                    textShadow: pnlPositive
                      ? "0 0 20px rgba(74, 222, 128, 0.5)"
                      : "0 0 20px rgba(251, 113, 133, 0.5)",
                  }}
                >
                  {runPnlUsd != null && Number.isFinite(runPnlUsd)
                    ? formatUsdSigned(runPnlUsd)
                    : "—"}
                </div>
                <div
                  style={{
                    marginTop: 4,
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: "0.1em",
                    textTransform: "uppercase",
                    color: "rgba(255,255,255,0.4)",
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {botRunUptimeSec != null
                    ? `Last ${formatBotUptime(botRunUptimeSec)}`
                    : "Since bot start"}
                </div>
              </div>
              <div className="lb-stat-box" style={{ flex: "1 1 100px" }}>
                <div className="lb-stat-label">WIN RATE</div>
                <div
                  className="lb-stat-value"
                  style={{ color: "#fbbf24", textShadow: "0 0 20px rgba(251, 191, 36, 0.4)" }}
                >
                  {wrDisplay}
                </div>
                <div
                  style={{
                    marginTop: 4,
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: "0.08em",
                    color: "rgba(255,255,255,0.4)",
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {winRateExits > 0
                    ? `${winRateWins}W · ${winRateExits - winRateWins}L · ${winRateExits} trades`
                    : "No exits yet"}
                </div>
              </div>
              <div className="lb-stat-box" style={{ flex: "1 1 100px" }}>
                <div className="lb-stat-label">ROUND ENDS IN</div>
                <div
                  className="lb-stat-value"
                  style={{ color: "#fbbf24", textShadow: "0 0 20px rgba(251, 191, 36, 0.4)" }}
                >
                  {timeDisplay}
                </div>
                <div
                  style={{
                    marginTop: 4,
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: "0.1em",
                    textTransform: "uppercase",
                    color: "rgba(255,255,255,0.4)",
                  }}
                >
                  Until next close
                </div>
              </div>
            </div>
          </div>

          {/* ── UP / DOWN CARDS ── */}
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: fb ? 8 : 14,
              marginBottom: fb ? 8 : 16,
              justifyContent: "center",
            }}
          >
            <div
              className="lb-side-card"
              style={{
                background:
                  "linear-gradient(160deg, rgba(22, 101, 52, 0.85), rgba(5, 46, 22, 0.95))",
                border: "2px solid rgba(74, 222, 128, 0.6)",
                boxShadow:
                  "0 0 40px rgba(52, 211, 153, 0.25), inset 0 1px 0 rgba(255,255,255,0.08)",
              }}
            >
              <div
                style={{
                  fontSize: 14,
                  letterSpacing: "0.18em",
                  textTransform: "uppercase",
                  color: "rgba(167, 243, 208, 0.9)",
                  fontWeight: 900,
                  marginBottom: 6,
                }}
              >
                UP
              </div>
              <div
                style={{
                  fontSize: fb ? 36 : 48,
                  fontWeight: 900,
                  color: "#4ade80",
                  fontVariantNumeric: "tabular-nums",
                  textShadow: "0 0 30px rgba(74, 222, 128, 0.6)",
                  lineHeight: 1.1,
                }}
              >
                {pxToCentsLabel(orderbook?.up?.mid ?? undefined)}
              </div>
            </div>

            <div
              className="lb-side-card"
              style={{
                background:
                  "linear-gradient(160deg, rgba(127, 29, 29, 0.85), rgba(69, 10, 10, 0.95))",
                border: "2px solid rgba(251, 113, 133, 0.6)",
                boxShadow:
                  "0 0 40px rgba(251, 113, 133, 0.25), inset 0 1px 0 rgba(255,255,255,0.08)",
              }}
            >
              <div
                style={{
                  fontSize: 14,
                  letterSpacing: "0.18em",
                  textTransform: "uppercase",
                  color: "rgba(254, 202, 202, 0.9)",
                  fontWeight: 900,
                  marginBottom: 6,
                }}
              >
                DOWN
              </div>
              <div
                style={{
                  fontSize: fb ? 36 : 48,
                  fontWeight: 900,
                  color: "#fb7185",
                  fontVariantNumeric: "tabular-nums",
                  textShadow: "0 0 30px rgba(251, 113, 133, 0.6)",
                  lineHeight: 1.1,
                }}
              >
                {pxToCentsLabel(orderbook?.down?.mid ?? undefined)}
              </div>
            </div>
          </div>
          </div>

          {/* ── CHART + LAST TRADES ── */}
          <section
            id="stream-stats"
            aria-label="Run PnL and trade history"
            style={
              fb
                ? {
                    flex: 1,
                    minHeight: 0,
                    display: "flex",
                    flexDirection: "column",
                  }
                : undefined
            }
          >
            {/* Chart header */}
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "flex-end",
                justifyContent: "space-between",
                gap: 10,
                marginBottom: 6,
              }}
            >
              <div
                style={{
                  fontSize: 11,
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  color: "rgba(255,255,255,0.45)",
                  fontWeight: 800,
                }}
              >
                CUMULATIVE RUN PNL »
              </div>
              {runUsdSessionStats && runPnlSeries.length > 0 && (
                <div
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    gap: 10,
                    fontSize: 11,
                    fontVariantNumeric: "tabular-nums",
                    color: "rgba(255,255,255,0.5)",
                    justifyContent: "flex-end",
                  }}
                >
                  <span>
                    <span
                      style={{
                        color: "#fbbf24",
                        fontWeight: 800,
                        marginRight: 4,
                      }}
                    >
                      ▲ A
                    </span>
                    {formatUsdSigned(runUsdSessionStats.maxUsd)}
                  </span>
                  <span>
                    <span
                      style={{
                        color: "#f87171",
                        fontWeight: 800,
                        marginRight: 4,
                      }}
                    >
                      ♥ Low
                    </span>
                    {formatUsdSigned(runUsdSessionStats.minUsd)}
                  </span>
                  <span style={{ color: "rgba(255,255,255,0.3)" }}>●</span>
                  <span style={{ color: "rgba(255,255,255,0.35)" }}>0%</span>
                </div>
              )}
            </div>

            <div
              style={{
                display: "flex",
                flexWrap: fb ? "nowrap" : "wrap",
                gap: fb ? 6 : 12,
                alignItems: "stretch",
                ...(fb
                  ? {
                      flex: 1,
                      minHeight: 0,
                    }
                  : {}),
              }}
            >
              {/* Chart */}
              <div
                className="lb-chart-shell"
                style={{
                  flex: fb ? "2 1 0" : "3 1 300px",
                  minWidth: 0,
                  width: "100%",
                  ...(fb
                    ? {
                        minHeight: 0,
                        alignSelf: "stretch",
                        display: "flex",
                        flexDirection: "column",
                      }
                    : { height: 300 }),
                }}
              >
                {stratCfg?.mode === "off" && !botRunning && chartIdleCopy ? (
                  <div
                    style={{
                      flex: fb ? 1 : undefined,
                      height: "100%",
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 12,
                      padding: 16,
                      textAlign: "center",
                      color: "rgba(255,255,255,0.5)",
                    }}
                  >
                    {chartIdleCopy.showSpinner && (
                      <div className="lb-idle-spinner" aria-hidden />
                    )}
                    <div>
                      <div
                        style={{
                          fontSize: 16,
                          fontWeight: 750,
                          color: "rgba(255,255,255,0.7)",
                        }}
                      >
                        {chartIdleCopy.headline}
                      </div>
                      <div
                        style={{
                          fontSize: 12,
                          marginTop: 6,
                          color: "rgba(255,255,255,0.4)",
                        }}
                      >
                        {chartIdleCopy.sub}
                      </div>
                    </div>
                  </div>
                ) : botRunning && runPnlSeries.length === 0 ? (
                  <div
                    style={{
                      flex: fb ? 1 : undefined,
                      height: "100%",
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 12,
                      padding: 12,
                    }}
                  >
                    <div className="lb-idle-spinner" aria-hidden />
                    <div
                      style={{
                        fontSize: 14,
                        fontWeight: 600,
                        color: "rgba(255,255,255,0.6)",
                      }}
                    >
                      {runPnlUsd == null
                        ? "Waiting for equity baseline…"
                        : "Building the run P&L curve…"}
                    </div>
                  </div>
                ) : (
                  <div
                    style={
                      fb
                        ? { flex: 1, minHeight: 0, width: "100%", minWidth: 0 }
                        : { width: "100%", height: "100%" }
                    }
                    onMouseEnter={() => setFrozenRunPnlSeries(runPnlSeries)}
                    onMouseLeave={() => setFrozenRunPnlSeries(null)}
                  >
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart
                      data={effectiveRunPnlSeries}
                      margin={{ top: 8, right: 6, bottom: 4, left: 4 }}
                    >
                      <CartesianGrid
                        stroke="rgba(255,255,255,0.06)"
                        strokeDasharray="2 8"
                        vertical={false}
                      />
                      <XAxis
                        dataKey="t"
                        type="number"
                        domain={[
                          "dataMin",
                          (dataMax: number) => {
                            const first = effectiveRunPnlSeries[0]?.t;
                            const last = effectiveRunPnlSeries[effectiveRunPnlSeries.length - 1]?.t;
                            const span =
                              typeof first === "number" && typeof last === "number" && last > first
                                ? last - first
                                : 0;
                            const pad = Math.max(span * 0.03, 3);
                            return dataMax + pad;
                          },
                        ]}
                        tick={{
                          fill: "rgba(255,255,255,0.3)",
                          fontSize: 10,
                        }}
                        tickFormatter={(v) => israelTime(Number(v))}
                        stroke="rgba(255,255,255,0.08)"
                      />
                      <YAxis
                        dataKey="usd"
                        domain={runUsdYDomain}
                        tick={{
                          fill: "rgba(255,255,255,0.3)",
                          fontSize: 10,
                        }}
                        tickFormatter={(v) => formatUsdSigned(Number(v))}
                        width={68}
                        tickCount={7}
                        stroke="rgba(255,255,255,0.08)"
                      />
                      <Tooltip
                        cursor={{
                          stroke: "rgba(248, 250, 252, 0.2)",
                          strokeWidth: 1,
                        }}
                        content={({ active, payload }) => {
                          if (!active || !payload || payload.length === 0)
                            return null;
                          const row =
                            payload.find(
                              (p) => String(p.name ?? "") === "runPnlHit"
                            ) ?? payload[0];
                          const pt = row?.payload as RunPnlPoint;
                          if (!pt || pt.t == null) return null;
                          const v = pt.usd;
                          const c = v >= 0 ? RUN_PNL_GREEN : RUN_PNL_RED;
                          return (
                            <div
                              style={{
                                background: "rgba(10, 14, 25, 0.95)",
                                border:
                                  "1px solid rgba(251, 191, 36, 0.3)",
                                borderRadius: 8,
                                fontSize: 12,
                                padding: "6px 10px",
                                boxShadow:
                                  "0 4px 16px rgba(0,0,0,0.5)",
                              }}
                            >
                              <div
                                style={{
                                  color: "rgba(255,255,255,0.4)",
                                  marginBottom: 3,
                                }}
                              >
                                {israelTime(pt.t)}
                              </div>
                              <div style={{ color: c, fontWeight: 700 }}>
                                {formatUsdSigned(v)}
                              </div>
                            </div>
                          );
                        }}
                      />
                      <ReferenceLine
                        y={0}
                        stroke="rgba(255,255,255,0.12)"
                        strokeDasharray="4 4"
                      />
                      {runUsdChartRefExtremes &&
                        runPnlSeries.length > 1 &&
                        runUsdChartRefExtremes.maxUsd !==
                          runUsdChartRefExtremes.minUsd && (
                          <>
                            <ReferenceLine
                              y={runUsdChartRefExtremes.maxUsd}
                              stroke="#fbbf24"
                              strokeDasharray="5 5"
                              strokeOpacity={0.4}
                            />
                            <ReferenceLine
                              y={runUsdChartRefExtremes.minUsd}
                              stroke="#f87171"
                              strokeDasharray="5 5"
                              strokeOpacity={0.4}
                            />
                          </>
                        )}
                      {runPnlSegments.map((seg, i) => (
                        <Area
                          key={`lb-pnl-area-${i}`}
                          type="monotone"
                          data={seg.data}
                          dataKey="usd"
                          stroke="none"
                          fill={seg.fill}
                          fillOpacity={0.15}
                          baseLine={0}
                          isAnimationActive={false}
                          activeDot={false}
                          pointerEvents="none"
                        />
                      ))}
                      {runPnlSegments.map((seg, i) => (
                        <Line
                          key={`lb-pnl-line-${i}`}
                          type="monotone"
                          data={seg.data}
                          dataKey="usd"
                          stroke={seg.stroke}
                          strokeWidth={2.5}
                          dot={false}
                          isAnimationActive={false}
                          activeDot={false}
                          pointerEvents="none"
                        />
                      ))}
                      {(() => {
                        const last = effectiveRunPnlSeries[effectiveRunPnlSeries.length - 1];
                        if (!last) return null;
                        const c = last.usd >= 0 ? RUN_PNL_GREEN : RUN_PNL_RED;
                        return (
                          <ReferenceDot
                            x={last.t}
                            y={last.usd}
                            r={5}
                            fill={c}
                            stroke="rgba(255,255,255,0.55)"
                            strokeWidth={1.5}
                            isFront
                            ifOverflow="extendDomain"
                          />
                        );
                      })()}
                      {/* Single invisible series: full series so Tooltip/activeDot work on entire curve (not only green fill). */}
                      <Line
                        name="runPnlHit"
                        type="monotone"
                        data={effectiveRunPnlSeries}
                        dataKey="usd"
                        stroke="transparent"
                        strokeWidth={20}
                        dot={false}
                        isAnimationActive={false}
                        activeDot={(props: {
                          cx?: number;
                          cy?: number;
                          payload?: RunPnlPoint;
                        }) => {
                          const v = props.payload?.usd ?? 0;
                          const c = v >= 0 ? RUN_PNL_GREEN : RUN_PNL_RED;
                          const cx = props.cx ?? 0;
                          const cy = props.cy ?? 0;
                          return (
                            <circle
                              cx={cx}
                              cy={cy}
                              r={6}
                              fill={c}
                              stroke="rgba(255,255,255,0.35)"
                              strokeWidth={1}
                            />
                          );
                        }}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                  </div>
                )}
              </div>

              {/* Last trades sidebar */}
              <aside
                aria-label="Last trades"
                style={{
                  flex: fb ? "1 1 0" : "1 1 200px",
                  minWidth: fb ? 0 : 180,
                  maxWidth: fb ? "none" : 300,
                  ...(fb
                    ? {
                        minHeight: 0,
                        alignSelf: "stretch",
                        height: "100%",
                      }
                    : { maxHeight: 300 }),
                  overflowY: "auto",
                  overflowX: "hidden",
                  padding: "12px 14px",
                  borderRadius: 10,
                  border: "1px solid rgba(251, 191, 36, 0.3)",
                  background:
                    "linear-gradient(165deg, rgba(10, 14, 25, 0.98), rgba(20, 22, 38, 0.9))",
                  boxShadow:
                    "0 0 20px rgba(251, 191, 36, 0.08), inset 0 1px 0 rgba(255,255,255,0.04)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 10,
                    position: "sticky",
                    top: -12,
                    zIndex: 2,
                    paddingTop: 12,
                    paddingBottom: 8,
                    marginTop: -12,
                    marginLeft: -14,
                    marginRight: -14,
                    paddingLeft: 14,
                    paddingRight: 14,
                    background:
                      "linear-gradient(165deg, rgba(10, 14, 25, 0.98), rgba(20, 22, 38, 0.95))",
                  }}
                >
                  <div
                    style={{
                      fontSize: 11,
                      letterSpacing: "0.16em",
                      textTransform: "uppercase",
                      color: "#fb7185",
                      fontWeight: 900,
                    }}
                  >
                    LAST TRADES
                  </div>
                  <button
                    type="button"
                    onClick={() => setShowPnl((v) => !v)}
                    style={{
                      fontSize: 9,
                      fontWeight: 700,
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: showPnl ? "#fde68a" : "rgba(255,255,255,0.4)",
                      background: showPnl
                        ? "rgba(251,191,36,0.12)"
                        : "rgba(255,255,255,0.05)",
                      border: showPnl
                        ? "1px solid rgba(251,191,36,0.45)"
                        : "1px solid rgba(255,255,255,0.1)",
                      borderRadius: 5,
                      padding: "2px 7px",
                      cursor: "pointer",
                      transition: "background 0.08s ease, border-color 0.08s ease, color 0.08s ease",
                      lineHeight: 1.6,
                    }}
                    title={showPnl ? "Hide P&L" : "Show P&L"}
                  >
                    {showPnl ? "Hide" : "Show"} P&L
                  </button>
                </div>

                {lastTrades.length === 0 ? (
                  <div
                    style={{
                      fontSize: 12,
                      color: "rgba(255,255,255,0.4)",
                      fontWeight: 600,
                    }}
                  >
                    No closed rounds yet.
                  </div>
                ) : (
                  lastTrades.map((r) => (
                    <div key={r.id} className="lb-trade-row">
                      <span
                        className="lb-trade-dot"
                        style={{
                          color: r.win ? "#4ade80" : "#fb7185",
                          background: r.win ? "#4ade80" : "#fb7185",
                        }}
                      />
                      <span
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          flexWrap: "wrap",
                          flex: 1,
                          minWidth: 0,
                        }}
                      >
                        <span
                          style={{
                            fontSize: 12,
                            fontWeight: 700,
                            color: "rgba(255,255,255,0.65)",
                          }}
                        >
                          {r.startLabel === r.endLabel
                            ? r.startLabel
                            : `${r.startLabel}`}
                        </span>
                        {r.side ? (
                          <span
                            style={{
                              fontSize: 10,
                              fontWeight: 900,
                              letterSpacing: "0.12em",
                              color: r.side === "Up" ? "#4ade80" : "#fb7185",
                              flexShrink: 0,
                            }}
                          >
                            {r.side === "Up" ? "UP" : "DOWN"}
                          </span>
                        ) : null}
                      </span>
                      {showPnl && (
                        <span
                          style={{
                            fontSize: 13,
                            fontWeight: 900,
                            fontVariantNumeric: "tabular-nums",
                            color:
                              r.pnlUsd == null
                                ? "rgba(255,255,255,0.3)"
                                : r.win
                                  ? "#4ade80"
                                  : "#fb7185",
                          }}
                        >
                          {r.pnlUsd == null
                            ? "—"
                            : `${r.pnlUsd >= 0 ? "+" : ""}$${Math.abs(r.pnlUsd).toFixed(0)}`}
                        </span>
                      )}
                    </div>
                  ))
                )}
              </aside>
            </div>
          </section>
        </div>
      </BroadcastFit>
    </div>
  );
}

