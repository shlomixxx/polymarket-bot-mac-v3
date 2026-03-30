import {
  Area,
  ComposedChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { chartAxisTick, chartTooltipStyle, pnlCurveKind, chartStroke } from "../chartConstants";
import { safeSvgIdPart } from "../chartUtils";
import { useChartAnimationGate } from "../hooks/useChartAnimationGate";
import { formatPnlAxisTime, formatPctAxisTick } from "../pnlChartFormatters";

type Row = { ts: number; upnl: number; t: string };

type PeakTrough = {
  peak: number | null | undefined;
  trough: number | null | undefined;
};

function tooltipCommon() {
  return {
    contentStyle: chartTooltipStyle,
    labelStyle: { color: "var(--text-secondary)" },
    itemStyle: { color: "var(--text)" },
  } as const;
}

export function PnlOpenAreaChart({
  sessionId,
  data,
  yDomain,
  peakTrough,
}: {
  sessionId: string;
  data: Row[];
  yDomain: [number, number];
  peakTrough: PeakTrough;
}) {
  const { peak, trough } = peakTrough;
  const lastUpnl = data.length ? data[data.length - 1].upnl : undefined;
  const anim = useChartAnimationGate(data.length, lastUpnl, { epsilon: 0.04 });
  const gid = safeSvgIdPart(sessionId);

  return (
    <ResponsiveContainer width="100%" height={140}>
      <ComposedChart data={data} margin={{ top: 4, right: 6, bottom: 4, left: 4 }}>
        <defs>
          <linearGradient id={`pnlFill-open-${gid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.32} />
            <stop offset="75%" stopColor="var(--accent)" stopOpacity={0.06} />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis
          dataKey="ts"
          type="number"
          domain={["dataMin", "dataMax"]}
          tick={{ ...chartAxisTick, fontSize: 10 }}
          tickFormatter={(v) => formatPnlAxisTime(Number(v))}
          allowDecimals
        />
        <YAxis
          width={56}
          tick={{ ...chartAxisTick, fontSize: 10 }}
          domain={yDomain}
          tickFormatter={formatPctAxisTick}
          allowDataOverflow
        />
        <Tooltip
          {...tooltipCommon()}
          labelFormatter={(label) => {
            const n = Number(label);
            return Number.isFinite(n) ? formatPnlAxisTime(n) : String(label);
          }}
          formatter={(v: number) => [`${Number(v).toFixed(1)}%`, "תשואה מול עלות"] as [string, string]}
        />
        <ReferenceLine y={0} stroke="var(--chart-axis)" strokeDasharray="2 2" />
        {peak != null && Number.isFinite(peak) && (
          <ReferenceLine y={peak} stroke="var(--up)" strokeDasharray="4 4" strokeOpacity={0.45} />
        )}
        {trough != null && Number.isFinite(trough) && (
          <ReferenceLine y={trough} stroke="var(--down)" strokeDasharray="4 4" strokeOpacity={0.45} />
        )}
        <Area
          type={pnlCurveKind}
          dataKey="upnl"
          stroke="var(--accent)"
          strokeWidth={chartStroke.width}
          strokeLinecap={chartStroke.linecap}
          strokeLinejoin={chartStroke.linejoin}
          fill={`url(#pnlFill-open-${gid})`}
          fillOpacity={1}
          dot={(dotProps: { cx?: number; cy?: number; index?: number }) => {
            const last = data.length - 1;
            const idx = dotProps.index ?? 0;
            if (dotProps.index !== last || dotProps.cx == null || dotProps.cy == null) {
              return <g key={`pnl-open-dot-${idx}`} />;
            }
            return (
              <circle
                key={`pnl-open-dot-${idx}`}
                cx={dotProps.cx}
                cy={dotProps.cy}
                r={4}
                fill="var(--accent)"
                stroke="var(--bg-elevated)"
                strokeWidth={1.5}
                className="pnl-live-dot"
              />
            );
          }}
          activeDot={{ r: 5, strokeWidth: 0 }}
          baseLine={0}
          isAnimationActive={anim}
          animationDuration={320}
          animationEasing="ease-out"
          connectNulls
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function PnlClosedAreaChart({
  sessionId,
  data,
  yDomain,
  peakTrough,
}: {
  sessionId: string;
  data: Row[];
  yDomain: [number, number];
  peakTrough: PeakTrough;
}) {
  const { peak, trough } = peakTrough;
  const lastUpnl = data.length ? data[data.length - 1].upnl : undefined;
  const anim = useChartAnimationGate(data.length, lastUpnl, { epsilon: 0.02 });
  const gid = safeSvgIdPart(sessionId);

  return (
    <ResponsiveContainer width="100%" height={140}>
      <ComposedChart data={data} margin={{ top: 4, right: 6, bottom: 4, left: 4 }}>
        <defs>
          <linearGradient id={`pnlFill-closed-${gid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.32} />
            <stop offset="75%" stopColor="var(--accent)" stopOpacity={0.06} />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis
          dataKey="ts"
          type="number"
          domain={["dataMin", "dataMax"]}
          tick={{ ...chartAxisTick, fontSize: 10 }}
          tickFormatter={(v) => formatPnlAxisTime(Number(v))}
          allowDecimals
        />
        <YAxis
          width={56}
          tick={{ ...chartAxisTick, fontSize: 10 }}
          domain={yDomain}
          tickFormatter={formatPctAxisTick}
          allowDataOverflow
        />
        <Tooltip
          {...tooltipCommon()}
          labelFormatter={(label) => {
            const n = Number(label);
            return Number.isFinite(n) ? formatPnlAxisTime(n) : String(label);
          }}
          formatter={(v: number) => [`${Number(v).toFixed(1)}%`, "תשואה מול עלות"] as [string, string]}
        />
        <ReferenceLine y={0} stroke="var(--chart-axis)" strokeDasharray="2 2" />
        {peak != null && Number.isFinite(peak) && (
          <ReferenceLine y={peak} stroke="var(--up)" strokeDasharray="4 4" strokeOpacity={0.45} />
        )}
        {trough != null && Number.isFinite(trough) && (
          <ReferenceLine y={trough} stroke="var(--down)" strokeDasharray="4 4" strokeOpacity={0.45} />
        )}
        <Area
          type={pnlCurveKind}
          dataKey="upnl"
          stroke="var(--accent)"
          strokeWidth={chartStroke.width}
          strokeLinecap={chartStroke.linecap}
          strokeLinejoin={chartStroke.linejoin}
          fill={`url(#pnlFill-closed-${gid})`}
          fillOpacity={1}
          dot={(dotProps: { cx?: number; cy?: number; index?: number }) => {
            const last = data.length - 1;
            const idx = dotProps.index ?? 0;
            if (dotProps.index !== last || dotProps.cx == null || dotProps.cy == null) {
              return <g key={`pnl-closed-dot-${idx}`} />;
            }
            return (
              <circle
                key={`pnl-closed-dot-${idx}`}
                cx={dotProps.cx}
                cy={dotProps.cy}
                r={4}
                fill="var(--accent)"
                stroke="var(--bg-elevated)"
                strokeWidth={1.5}
              />
            );
          }}
          activeDot={{ r: 5, strokeWidth: 0 }}
          baseLine={0}
          isAnimationActive={anim}
          animationDuration={380}
          animationEasing="ease-out"
          connectNulls
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
