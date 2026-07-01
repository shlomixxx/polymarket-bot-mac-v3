/**
 * BtcWindowChart — Polymarket-style "BTC Up or Down" window chart, dark-adapted.
 * Orange price line + gradient fill, dashed "Price to Beat" target with a right-edge pill,
 * a current-price dot, price axis on the right, time axis on the bottom, left delta pills,
 * and a header (price-to-beat / current price / MM:SS countdown).
 *
 * The chart body is forced dir="ltr" so time reads old→new left→right even inside the RTL app.
 *
 * NOTE: `data` is `btc.history` (~120 samples ~1s). The line can only cover as much of the
 * window as the server buffer holds; the backend buffer is sized to span a full window.
 */
import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { chartAxisTick, chartStroke, chartTooltipStyle, computeBtcPriceChartYDomain } from "../chartConstants";
import { safeSvgIdPart } from "../chartUtils";
import { formatPnlAxisTime } from "../pnlChartFormatters";

export type BtcWindowChartProps = {
  data: { t: number; p: number }[];
  priceToBeat: number | null;
  currentPrice: number;
  source?: string;
  secondsLeft: number | null;
  diff: number | null;
  windowSec: number;
  epoch: number;
};

function mmss(s: number | null): string {
  const v = Math.max(0, Math.floor(s ?? 0));
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
}

const usd2 = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 2 });

/** "יעד" pill pinned to the right edge of the target ReferenceLine. */
function TargetPill({ viewBox }: { viewBox?: { x: number; y: number; width: number } }) {
  if (!viewBox) return null;
  const x = viewBox.x + viewBox.width - 42;
  const y = viewBox.y - 9;
  return (
    <g transform={`translate(${x},${y})`}>
      <rect width={38} height={18} rx={5} fill="var(--bg-elevated)" stroke="var(--chart-tooltip-border)" />
      <text x={19} y={13} textAnchor="middle" fontSize={10} fill="var(--text-secondary)">
        יעד
      </text>
    </g>
  );
}

export function BtcWindowChart({
  data,
  priceToBeat,
  currentPrice,
  source,
  secondsLeft,
  diff,
  windowSec,
  epoch,
}: BtcWindowChartProps) {
  // scope to the current window: keep only samples from window-open onward
  const windowData = useMemo(() => {
    return (data || [])
      .filter((r) => Number.isFinite(r.t) && Number.isFinite(r.p) && (epoch <= 0 || r.t >= epoch))
      .map((r) => ({ ts: Number(r.t), p: Number(r.p) }))
      .sort((a, b) => a.ts - b.ts);
  }, [data, epoch]);

  const yDomain = useMemo(
    () => computeBtcPriceChartYDomain(windowData.map((d) => d.p), priceToBeat),
    [windowData, priceToBeat],
  );

  const xDomain: [number, number] =
    epoch > 0 ? [epoch, epoch + windowSec] : ["dataMin" as unknown as number, "dataMax" as unknown as number];

  // left delta pills: a few evenly-spaced Δ vs window open
  const deltaPills = useMemo(() => {
    if (windowData.length < 2) return [] as { ts: number; delta: number }[];
    const open = priceToBeat ?? windowData[0].p;
    const step = Math.max(1, Math.floor(windowData.length / 6));
    return windowData.filter((_, i) => i % step === 0).slice(-6).map((d) => ({ ts: d.ts, delta: d.p - open }));
  }, [windowData, priceToBeat]);

  const last = windowData.length ? windowData[windowData.length - 1] : null;
  const gid = safeSvgIdPart(`btcwin-${epoch}`);
  const isChainlink = source === "chainlink_stream";

  return (
    <div dir="ltr" style={{ direction: "ltr", textAlign: "left" }}>
      {/* header: price-to-beat / current price / countdown */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10, gap: 12 }}>
        <div>
          <div style={{ fontSize: 12, color: "var(--muted)" }}>מחיר יעד</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 24, fontWeight: 700, color: "var(--text)", lineHeight: 1.15 }}>
            {priceToBeat != null ? `$${usd2(priceToBeat)}` : "…"}
          </div>
          <div style={{ marginTop: 3, fontSize: 13, color: diff == null ? "var(--muted)" : diff >= 0 ? "var(--up)" : "var(--down)" }}>
            {diff != null ? (diff >= 0 ? "▲" : "▼") : ""} {diff != null ? `$${Math.abs(diff).toFixed(2)}` : ""}{" "}
            <span style={{ color: "var(--text-secondary)" }}>${usd2(currentPrice)}</span>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div
            style={{
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              fontSize: 24,
              fontWeight: 700,
              color: "var(--text)",
              letterSpacing: 1,
            }}
          >
            {mmss(secondsLeft)}
          </div>
          <div style={{ fontSize: 11, color: "var(--muted)" }}>
            {isChainlink ? "⛓️ Chainlink" : source === "binance_fallback" ? "⚠ Binance" : ""}
          </div>
        </div>
      </div>

      {/* delta pills + chart */}
      <div style={{ display: "flex", gap: 6 }}>
        {deltaPills.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", justifyContent: "space-evenly", gap: 3, paddingBlock: 4 }}>
            {deltaPills.map((d, i) => {
              const r = Math.round(d.delta);
              const sign = r > 0 ? "+" : r < 0 ? "−" : "";
              return (
                <span
                  key={`${d.ts}-${i}`}
                  style={{
                    fontSize: 10,
                    fontWeight: 700,
                    color: "#fff",
                    background: r > 0 ? "var(--up)" : r < 0 ? "var(--down)" : "var(--muted)",
                    borderRadius: 6,
                    padding: "1px 5px",
                    textAlign: "center",
                    whiteSpace: "nowrap",
                  }}
                >
                  {sign}${Math.abs(r)}
                </span>
              );
            })}
          </div>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={windowData} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
              <defs>
                <linearGradient id={`btcFill-${gid}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--btc-orange)" stopOpacity={0.26} />
                  <stop offset="72%" stopColor="var(--btc-orange)" stopOpacity={0.06} />
                  <stop offset="100%" stopColor="var(--btc-orange)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid vertical={false} stroke="var(--chart-grid)" strokeOpacity={0.6} />
              <XAxis
                dataKey="ts"
                type="number"
                domain={xDomain}
                allowDataOverflow
                tick={{ ...chartAxisTick, fontSize: 10 }}
                tickFormatter={(v) => formatPnlAxisTime(Number(v))}
                allowDecimals
              />
              <YAxis
                orientation="right"
                width={72}
                domain={yDomain ?? (["auto", "auto"] as const)}
                tick={{ ...chartAxisTick, fontSize: 10 }}
                tickFormatter={(v) =>
                  typeof v === "number" && Number.isFinite(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : ""
                }
              />
              <Tooltip
                contentStyle={chartTooltipStyle}
                labelStyle={{ color: "var(--text-secondary)" }}
                itemStyle={{ color: "var(--text)" }}
                labelFormatter={(label) => (Number.isFinite(Number(label)) ? formatPnlAxisTime(Number(label)) : String(label))}
                formatter={(value) => [
                  typeof value === "number" && Number.isFinite(value)
                    ? `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                    : String(value),
                  "מחיר",
                ]}
              />
              {priceToBeat != null && (
                <ReferenceLine
                  y={priceToBeat}
                  stroke="var(--btc-target)"
                  strokeDasharray="4 4"
                  ifOverflow="extendDomain"
                  label={<TargetPill />}
                />
              )}
              <Area
                type="monotone"
                dataKey="p"
                stroke="var(--btc-orange)"
                strokeWidth={chartStroke.width}
                strokeLinecap={chartStroke.linecap}
                strokeLinejoin={chartStroke.linejoin}
                fill={`url(#btcFill-${gid})`}
                fillOpacity={1}
                dot={false}
                activeDot={{ r: 5, strokeWidth: 0, fill: "var(--btc-orange)" }}
                isAnimationActive
                animationDuration={380}
                animationEasing="ease-out"
                connectNulls
              />
              {last && (
                <ReferenceDot
                  x={last.ts}
                  y={last.p}
                  r={4}
                  fill="var(--btc-orange)"
                  stroke="var(--card)"
                  strokeWidth={1.5}
                  isFront
                  ifOverflow="extendDomain"
                />
              )}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
