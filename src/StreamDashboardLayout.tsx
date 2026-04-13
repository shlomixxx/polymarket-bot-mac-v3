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
import type { StreamSpectatorLayoutProps, RoundOutcomeRow } from "./StreamSpectatorLayout";

/* ── colours ────────────────────────────────── */
const GREEN = "#34d399";
const RED = "#fb7185";

/* ── utilities (mirrors spectator helpers) ──── */

function formatUsdSigned(v: number): string {
  const a = Math.abs(v);
  return `${v >= 0 ? "+" : "-"}$${a.toFixed(2)}`;
}

function formatBotUptime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m ${String(s).padStart(2, "0")}s`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function windowLabel(m: { btc_window?: string; window_sec?: number } | null): string {
  if (!m) return "—";
  const w = m.btc_window;
  if (w === "5m" || w === "15m") return w;
  const sec = m.window_sec;
  if (sec === 300) return "5m";
  if (sec === 900) return "15m";
  if (typeof sec === "number" && sec > 0) return `${Math.round(sec / 60)}m`;
  return "—";
}

function pxToCentsLabel(px: number | null | undefined): string {
  if (px == null || !Number.isFinite(Number(px))) return "—";
  return `${(Number(px) * 100).toFixed(1)}¢`;
}

type RunPnlPoint = { t: number; usd: number };

function splitRunPnlSegments(points: RunPnlPoint[]): { stroke: string; fill: string; data: RunPnlPoint[] }[] {
  if (points.length === 0) return [];
  if (points.length === 1) {
    const p = points[0];
    const c = p.usd >= 0 ? GREEN : RED;
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
    if (Math.abs(u1 - u0) < 1e-12) {
      current.push({ ...curr });
      continue;
    }
    const ratio = u0 / (u0 - u1);
    const tz = prev.t + ratio * (curr.t - prev.t);
    const crossing: RunPnlPoint = { t: tz, usd: 0 };
    current.push(crossing);
    segments.push({ stroke: prevPos ? GREEN : RED, fill: prevPos ? GREEN : RED, data: [...current] });
    current = [crossing, { ...curr }];
  }
  const lastPos = points[points.length - 1].usd >= 0;
  segments.push({ stroke: lastPos ? GREEN : RED, fill: lastPos ? GREEN : RED, data: [...current] });
  return segments.filter((s) => s.data.length >= 2);
}

/* ── KPI card ───────────────────────────────── */

function KpiCard(props: {
  label: string;
  value: string;
  sub?: string;
  valueColor?: string;
  pulse?: boolean;
}) {
  return (
    <div
      aria-label={props.label}
      style={{
        flex: "1 1 180px",
        minWidth: 150,
        padding: "18px 20px",
        borderRadius: "var(--radius-md)",
        background: "var(--card)",
        border: "1px solid var(--border)",
        boxShadow: "var(--shadow-card)",
      }}
    >
      <div
        style={{
          fontSize: 11,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: "var(--muted)",
          marginBottom: 8,
          fontWeight: 600,
        }}
      >
        {props.label}
      </div>
      <div
        style={{
          fontSize: 24,
          fontWeight: 700,
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.2,
          color: props.valueColor ?? "var(--text)",
        }}
      >
        {props.pulse ? <span className="pnl-live-dot">{props.value}</span> : props.value}
      </div>
      {props.sub ? (
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>{props.sub}</div>
      ) : null}
    </div>
  );
}

/* ── Price pill (simplified) ────────────────── */

function PricePill(props: { label: string; mid: number | null; accent: "up" | "down" }) {
  const isUp = props.accent === "up";
  const color = isUp ? "#4ade80" : "#fb7185";
  const bg = isUp
    ? "linear-gradient(145deg, rgba(52, 211, 153, 0.10), var(--card))"
    : "linear-gradient(145deg, rgba(251, 113, 133, 0.10), var(--card))";
  const border = isUp
    ? "1px solid rgba(52, 211, 153, 0.35)"
    : "1px solid rgba(251, 113, 133, 0.35)";
  return (
    <div
      style={{
        flex: "1 1 160px",
        padding: "16px 20px",
        borderRadius: "var(--radius-md)",
        background: bg,
        border,
        textAlign: "center",
      }}
    >
      <div
        style={{
          fontSize: 11,
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          color,
          fontWeight: 700,
          marginBottom: 6,
        }}
      >
        {props.label}
      </div>
      <div
        style={{
          fontSize: 28,
          fontWeight: 800,
          fontVariantNumeric: "tabular-nums",
          color,
          lineHeight: 1.1,
        }}
      >
        {pxToCentsLabel(props.mid ?? undefined)}
      </div>
      <div style={{ fontSize: 10, marginTop: 6, color: "var(--muted)" }}>Live mid (¢)</div>
    </div>
  );
}

/* ── Round row ──────────────────────────────── */

function RoundRow({ r, showPnl }: { r: RoundOutcomeRow; showPnl: boolean }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 12px",
        borderRadius: 10,
        border: "1px solid var(--border)",
        background: "var(--bg-elevated)",
        marginBottom: 6,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      <span
        style={{
          width: 10,
          height: 10,
          borderRadius: "50%",
          flexShrink: 0,
          background: r.win ? GREEN : RED,
          boxShadow: `0 0 8px ${r.win ? GREEN : RED}`,
        }}
        aria-hidden
      />
      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text)", flex: 1 }}>
        {r.startLabel === r.endLabel ? r.startLabel : `${r.startLabel} – ${r.endLabel}`}
      </span>
      {showPnl && (
        <span
          style={{
            fontSize: 12,
            fontWeight: 700,
            color: r.pnlUsd == null ? "var(--muted)" : r.win ? GREEN : RED,
            minWidth: 48,
            textAlign: "right",
          }}
        >
          {r.pnlUsd == null ? "—" : `${r.pnlUsd >= 0 ? "+" : ""}${r.pnlUsd.toFixed(2)}$`}
        </span>
      )}
    </div>
  );
}

/* ── Main dashboard layout ──────────────────── */

export function StreamDashboardLayout(props: StreamSpectatorLayoutProps) {
  const {
    err,
    market,
    stratCfg,
    orderbook,
    open,
    agg,
    livePct,
    pnlColor,
    streamMood,
    moodStyle,
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
    windowElapsedPct,
    roundOutcomes,
    chartIdleCopy,
  } = props;

  const runPnlSegments = useMemo(() => splitRunPnlSegments(runPnlSeries), [runPnlSeries]);
  const [showPnl, setShowPnl] = useState(true);

  const pnlNow = runPnlUsd ?? 0;
  const pnlSign = pnlNow >= 0;

  return (
    <div
      style={{
        boxSizing: "border-box",
        minHeight: "100vh",
        padding: "32px 28px 48px",
        background: "var(--bg)",
        color: "var(--text)",
        fontFamily: "var(--font-display)",
        maxWidth: 1080,
        margin: "0 auto",
      }}
    >
      {/* ── header ────────────────────────────── */}
      <header
        role="banner"
        style={{
          marginBottom: 32,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: 16,
        }}
      >
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              fontSize: 12,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              color: "var(--text-secondary)",
              fontWeight: 600,
              marginBottom: 8,
            }}
          >
            <span
              style={{
                width: 10,
                height: 10,
                borderRadius: "50%",
                background: "linear-gradient(145deg, #6ee7b7, #22c55e)",
                boxShadow: "0 0 8px rgba(52,211,153,0.5)",
              }}
              aria-hidden
            />
            Dashboard
            <span
              style={{
                fontSize: 10,
                fontWeight: 800,
                letterSpacing: "0.14em",
                color: "#a7f3d0",
                padding: "3px 8px",
                borderRadius: 999,
                background: "rgba(52,211,153,0.12)",
                border: "1px solid rgba(52,211,153,0.35)",
              }}
            >
              LIVE
            </span>
          </div>
          <h1
            style={{
              margin: 0,
              fontSize: 26,
              fontWeight: 700,
              lineHeight: 1.25,
              color: "var(--text)",
            }}
          >
            {market?.title ?? "Loading market…"}
          </h1>
          <p
            style={{
              margin: "6px 0 0",
              fontSize: 13,
              color: "var(--muted)",
              maxWidth: 520,
              lineHeight: 1.45,
            }}
          >
            Professional overview — key stats, P&L, live prices, and round history.
            Use{" "}
            <code style={{ color: "var(--accent-bright)", fontSize: 12 }}>?stream=4</code>{" "}
            or{" "}
            <code style={{ color: "var(--accent-bright)", fontSize: 12 }}>/stream/dashboard</code>.
          </p>
        </div>

        {/* mode badge */}
        <div
          style={{
            padding: "14px 18px",
            borderRadius: "var(--radius-md)",
            border: `1px solid ${moodStyle.border}`,
            background: moodStyle.bg,
            boxShadow: moodStyle.shadow,
            minWidth: 160,
            textAlign: "center",
          }}
        >
          <div
            style={{
              fontSize: 10,
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              color: "var(--muted)",
              marginBottom: 6,
            }}
          >
            Bot mode
          </div>
          <div
            style={{
              fontSize: 22,
              fontWeight: 800,
              letterSpacing: "0.06em",
              lineHeight: 1.1,
              color: moodStyle.color,
            }}
          >
            {streamMood.label}
            {open && agg ? <span style={{ letterSpacing: "0.04em" }}> · {agg.side}</span> : null}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 6 }}>
            {streamMood.hint}
          </div>
        </div>
      </header>

      {err ? (
        <div
          role="alert"
          style={{
            padding: 12,
            borderRadius: 8,
            background: "var(--down-muted)",
            color: "var(--text)",
            marginBottom: 20,
          }}
        >
          {err}
        </div>
      ) : null}

      {/* ── KPI row ───────────────────────────── */}
      <section aria-label="Key performance indicators" style={{ marginBottom: 28 }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>
          <KpiCard
            label="Run P&L"
            value={runPnlUsd != null ? formatUsdSigned(runPnlUsd) : "—"}
            valueColor={runPnlUsd != null ? (pnlSign ? GREEN : RED) : undefined}
            pulse={runPnlUsd != null}
            sub={
              runUsdSessionStats
                ? `High ${formatUsdSigned(runUsdSessionStats.maxUsd)} · Low ${formatUsdSigned(runUsdSessionStats.minUsd)}`
                : undefined
            }
          />
          <KpiCard
            label="Win rate"
            value={winRatePct != null ? `${winRatePct.toFixed(0)}%` : "—"}
            valueColor={winRateHot ? "#fbbf24" : undefined}
            sub={`${winRateWins}W / ${winRateExits - winRateWins}L (${winRateExits} exits)`}
          />
          <KpiCard
            label="Session uptime"
            value={botRunUptimeSec != null ? formatBotUptime(botRunUptimeSec) : "—"}
            sub={`Window: ${windowLabel(market)}`}
          />
          <KpiCard
            label={open && agg ? `Position · ${agg.side}` : "Position"}
            value={
              open && agg
                ? `${agg.contracts} @ ${agg.avgEntryCents.toFixed(1)}¢`
                : "Flat"
            }
            valueColor={
              open && livePct != null
                ? livePct >= 0
                  ? GREEN
                  : RED
                : "var(--text-secondary)"
            }
            sub={
              open && livePct != null
                ? `Unrealized ${livePct >= 0 ? "+" : ""}${livePct.toFixed(2)}%`
                : undefined
            }
          />
        </div>
      </section>

      {/* ── Live prices ───────────────────────── */}
      <section aria-label="Live market prices" style={{ marginBottom: 28 }}>
        <h2
          style={{
            fontSize: 14,
            fontWeight: 700,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
            color: "var(--text-secondary)",
            margin: "0 0 12px",
          }}
        >
          Live prices
        </h2>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>
          <PricePill label="Up" mid={orderbook?.up?.mid ?? null} accent="up" />
          <PricePill label="Down" mid={orderbook?.down?.mid ?? null} accent="down" />
        </div>
      </section>

      {/* ── Window progress ───────────────────── */}
      <div
        aria-label="Market window progress"
        role="progressbar"
        aria-valuenow={Math.round(windowElapsedPct * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        style={{
          height: 6,
          borderRadius: 999,
          background: "rgba(148,163,184,0.15)",
          overflow: "hidden",
          marginBottom: 28,
        }}
      >
        <div
          style={{
            height: "100%",
            borderRadius: 999,
            width: `${Math.min(windowElapsedPct * 100, 100)}%`,
            background: "linear-gradient(90deg, rgba(147,169,201,0.35), var(--accent-bright))",
            transition: "width 0.6s ease-out",
          }}
        />
      </div>

      {/* ── Chart + history ───────────────────── */}
      <section aria-label="Run P&L chart and round history" style={{ marginBottom: 32 }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: 12,
            marginBottom: 12,
          }}
        >
          <h2
            style={{
              fontSize: 14,
              fontWeight: 700,
              letterSpacing: "0.04em",
              textTransform: "uppercase",
              color: "var(--text-secondary)",
              margin: 0,
            }}
          >
            Cumulative run P&L (USD)
          </h2>
          {runUsdSessionStats && runPnlSeries.length > 0 ? (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 14,
                fontSize: 12,
                fontVariantNumeric: "tabular-nums",
                color: "var(--text-secondary)",
              }}
            >
              <span>
                <span style={{ color: "#fbbf24", fontWeight: 700, marginRight: 4 }}>▲</span>
                {formatUsdSigned(runUsdSessionStats.maxUsd)}
              </span>
              <span style={{ color: "var(--border)" }}>·</span>
              <span>
                <span style={{ color: "#f87171", fontWeight: 700, marginRight: 4 }}>▼</span>
                {formatUsdSigned(runUsdSessionStats.minUsd)}
              </span>
              <span style={{ color: "var(--border)" }}>·</span>
              <span>
                <span
                  style={{
                    color: runUsdSessionStats.last >= 0 ? GREEN : RED,
                    fontWeight: 700,
                    marginRight: 4,
                  }}
                >
                  ●
                </span>
                {formatUsdSigned(runUsdSessionStats.last)}
              </span>
            </div>
          ) : null}
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "stretch" }}>
          {/* chart */}
          <div
            style={{
              flex: "3 1 380px",
              minWidth: 0,
              height: 320,
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border)",
              background: "var(--card)",
              overflow: "hidden",
            }}
          >
            {stratCfg?.mode === "off" && chartIdleCopy ? (
              <div
                style={{
                  height: "100%",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 16,
                  padding: 24,
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: 18, fontWeight: 700, color: "var(--text)" }}>
                  {chartIdleCopy.headline}
                </div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)", maxWidth: 380 }}>
                  {chartIdleCopy.sub}
                </div>
              </div>
            ) : stratCfg?.mode !== "off" && runPnlSeries.length === 0 ? (
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
                <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-secondary)" }}>
                  {runPnlUsd == null
                    ? "Waiting for equity baseline…"
                    : "Building the run P&L curve…"}
                </div>
                <div style={{ fontSize: 13, color: "var(--muted)" }}>
                  Sampling starts as soon as data flows.
                </div>
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart
                  data={runPnlSeries}
                  margin={{ top: 10, right: 8, bottom: 6, left: 4 }}
                >
                  <CartesianGrid
                    stroke="var(--border)"
                    strokeOpacity={0.5}
                    strokeDasharray="3 6"
                    vertical={false}
                  />
                  <XAxis
                    dataKey="t"
                    type="number"
                    domain={["dataMin", "dataMax"]}
                    tick={{ fill: "var(--muted)", fontSize: 11 }}
                    tickFormatter={(v) =>
                      new Date(Number(v) * 1000).toLocaleTimeString("en-GB", {
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                        hour12: false,
                      })
                    }
                  />
                  <YAxis
                    dataKey="usd"
                    domain={runUsdYDomain}
                    tick={{ fill: "var(--muted)", fontSize: 11 }}
                    tickFormatter={(v) => formatUsdSigned(Number(v))}
                    width={72}
                    tickCount={7}
                  />
                  <Tooltip
                    cursor={{ stroke: "rgba(248,250,252,0.35)", strokeWidth: 1 }}
                    content={({ active, label }) => {
                      if (!active || label == null || runPnlSeries.length === 0) return null;
                      const t = Number(label);
                      let best = runPnlSeries[0];
                      let bestD = Infinity;
                      for (const p of runPnlSeries) {
                        const d = Math.abs(p.t - t);
                        if (d < bestD) {
                          bestD = d;
                          best = p;
                        }
                      }
                      const v = best.usd;
                      const c = v >= 0 ? GREEN : RED;
                      return (
                        <div
                          style={{
                            background: "var(--chart-tooltip-bg)",
                            border: "1px solid var(--chart-tooltip-border)",
                            borderRadius: 8,
                            fontSize: 13,
                            padding: "8px 12px",
                          }}
                        >
                          <div style={{ color: "var(--muted)", marginBottom: 4 }}>
                            {new Date(best.t * 1000).toLocaleString("en-GB", {
                              hour: "2-digit",
                              minute: "2-digit",
                              second: "2-digit",
                              hour12: false,
                            })}
                          </div>
                          <div style={{ color: c, fontWeight: 700 }}>{formatUsdSigned(v)}</div>
                          <div style={{ color: "var(--muted)", fontSize: 11, marginTop: 4 }}>
                            Run P&L
                          </div>
                        </div>
                      );
                    }}
                  />
                  <ReferenceLine y={0} stroke="var(--border-strong)" strokeDasharray="4 4" />
                  {runUsdChartRefExtremes &&
                  runPnlSeries.length > 1 &&
                  runUsdChartRefExtremes.maxUsd !== runUsdChartRefExtremes.minUsd ? (
                    <>
                      <ReferenceLine
                        y={runUsdChartRefExtremes.maxUsd}
                        stroke="#fbbf24"
                        strokeDasharray="5 5"
                        strokeOpacity={0.55}
                      />
                      <ReferenceLine
                        y={runUsdChartRefExtremes.minUsd}
                        stroke="#f87171"
                        strokeDasharray="5 5"
                        strokeOpacity={0.55}
                      />
                    </>
                  ) : null}
                  {runPnlSegments.map((seg, i) => (
                    <Area
                      key={`d-pnl-a-${i}`}
                      type="monotone"
                      data={seg.data}
                      dataKey="usd"
                      stroke="none"
                      fill={seg.fill}
                      fillOpacity={0.22}
                      baseLine={0}
                      isAnimationActive={false}
                    />
                  ))}
                  {runPnlSegments.map((seg, i) => (
                    <Line
                      key={`d-pnl-l-${i}`}
                      type="monotone"
                      data={seg.data}
                      dataKey="usd"
                      stroke={seg.stroke}
                      strokeWidth={2.5}
                      dot={false}
                      isAnimationActive={false}
                      activeDot={{ r: 5, strokeWidth: 0, fill: seg.stroke }}
                    />
                  ))}
                </ComposedChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* round history */}
          <aside
            aria-label="Round history"
            style={{
              flex: "1 1 240px",
              minWidth: 200,
              maxWidth: 340,
              maxHeight: 320,
              overflow: "auto",
              padding: "14px 16px",
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border)",
              background: "var(--card)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 10,
              }}
            >
              <div
                style={{
                  fontSize: 11,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  color: "var(--text-secondary)",
                  fontWeight: 700,
                }}
              >
                Round history
              </div>
              <button
                onClick={() => setShowPnl((v) => !v)}
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                  color: showPnl ? "var(--accent-bright)" : "var(--muted)",
                  background: showPnl ? "var(--accent-muted)" : "rgba(255,255,255,0.04)",
                  border: showPnl
                    ? "1px solid rgba(147,169,201,0.35)"
                    : "1px solid var(--border)",
                  borderRadius: 6,
                  padding: "3px 8px",
                  cursor: "pointer",
                  transition: "all 0.18s",
                  lineHeight: 1.6,
                }}
              >
                {showPnl ? "Hide" : "Show"} P&L
              </button>
            </div>
            {roundOutcomes.length === 0 ? (
              <div style={{ fontSize: 13, color: "var(--text-secondary)", fontWeight: 600 }}>
                No closed rounds yet.
              </div>
            ) : (
              roundOutcomes.map((r) => (
                <RoundRow key={r.id} r={r} showPnl={showPnl} />
              ))
            )}
          </aside>
        </div>
      </section>

      {/* ── Telegram contact ──────────────────── */}
      <footer
        role="contentinfo"
        style={{
          padding: "24px 28px",
          borderRadius: "var(--radius-lg)",
          border: "1px solid var(--border)",
          background: "var(--card)",
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 24,
        }}
      >
        <div
          aria-label="QR code — Telegram @roller000"
          role="img"
          style={{
            flexShrink: 0,
            padding: 10,
            background: "#fff",
            borderRadius: 10,
            lineHeight: 0,
          }}
        >
          <QRCodeSVG
            value="https://t.me/roller000"
            size={120}
            level="M"
            includeMargin={false}
          />
        </div>
        <div style={{ flex: 1, minWidth: 200 }}>
          <div
            style={{
              fontSize: 15,
              fontWeight: 700,
              color: "var(--text)",
              marginBottom: 6,
            }}
          >
            Telegram: @roller000
          </div>
          <p
            dir="rtl"
            lang="he"
            style={{
              margin: 0,
              fontSize: 14,
              color: "var(--text-secondary)",
              lineHeight: 1.55,
            }}
          >
            הצעות לשיפור או שיתוף פעולה? צרו קשר בטלגרם.
          </p>
          <a
            href="https://t.me/roller000"
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: "inline-block",
              marginTop: 10,
              fontSize: 13,
              fontWeight: 600,
              color: "var(--accent-bright)",
              textDecoration: "none",
              padding: "6px 14px",
              borderRadius: "var(--radius-sm)",
              border: "1px solid rgba(147,169,201,0.3)",
              background: "var(--accent-muted)",
              transition: "filter 0.18s",
            }}
          >
            Open in Telegram &rarr;
          </a>
        </div>
      </footer>

      {/* ── page footer ───────────────────────── */}
      <div
        style={{
          marginTop: 20,
          fontSize: 11,
          color: "var(--muted)",
          lineHeight: 1.55,
        }}
      >
        Other layouts:{" "}
        <code style={{ color: "var(--accent-bright)" }}>?stream=1</code> Classic ·{" "}
        <code style={{ color: "var(--accent-bright)" }}>?stream=2</code> Showcase ·{" "}
        <code style={{ color: "var(--accent-bright)" }}>?stream=3</code> Spectator
      </div>
    </div>
  );
}
