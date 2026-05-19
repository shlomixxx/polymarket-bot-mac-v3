import { useCallback, useEffect, useState } from "react";
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  Cell, ReferenceLine,
} from "recharts";
import { api } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { Button } from "./ui/Button";

// ── Types ─────────────────────────────────────────────────────────────────

type Overview = {
  total_sessions: number;
  win_count: number;
  loss_count: number;
  win_rate_pct: number;
  total_pnl_usd: number;
  avg_win_usd: number;
  avg_loss_usd: number;
  expectancy_usd: number;
  profit_factor: number | null;
  rr_ratio: number | null;
  sharpe_ratio: number | null;
  max_drawdown_usd: number;
  recovery_factor: number | null;
  streaks: {
    max_win_streak: number;
    max_loss_streak: number;
    current_streak_type: string | null;
    current_streak_length: number;
  };
  by_exit_type: Record<string, { count: number; pnl: number }>;
  avg_peak_left_on_table_pct: number | null;
  avg_duration_sec: number;
};

type EquityPoint = { ts: number; cumulative_pnl: number; pnl: number; exit_type: string; side: string };
type DrawdownPoint = { ts: number; equity: number; drawdown: number; drawdown_pct: number; peak_equity: number };
type HourlyRow = { hour: number; total: number; wins: number; win_rate_pct: number; avg_pnl: number; expectancy: number };
type WeekdayRow = { weekday: number; day_name: string; total: number; wins: number; win_rate_pct: number; avg_pnl: number };
type HeatmapCell = { hour: number; weekday: number; day_name: string; total: number; win_rate_pct: number };
type EntryMinRow = { entry_minute: number; total: number; wins: number; win_rate_pct: number; avg_pnl: number };
type BucketRow = { bucket: string; count: number; wins: number; win_rate_pct: number; avg_pnl: number };
type HistBin = { bin_start: number; bin_end: number; count: number };
type Insight = { level: string; category: string; message: string };

type SubTab = "overview" | "timing" | "strategy" | "risk" | "backtest" | "insights";

// ── Helpers ───────────────────────────────────────────────────────────────

const fmtUsd = (v: number | null | undefined) => v == null ? "—" : `$${v.toFixed(2)}`;
const fmtPct = (v: number | null | undefined) => v == null ? "—" : `${v.toFixed(1)}%`;
const fmtNum = (v: number | null | undefined, d = 2) => v == null ? "—" : v.toFixed(d);
const fmtTs = (ts: number) => {
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
};

const LEVEL_COLORS: Record<string, string> = {
  critical: "#ff4444",
  warning: "#ffaa00",
  opportunity: "#44aaff",
  info: "#888",
};

const LEVEL_ICONS: Record<string, string> = {
  critical: "!!",
  warning: "!",
  opportunity: "->",
  info: "i",
};

// ── Metric Card ───────────────────────────────────────────────────────────

function MetricCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{
      background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
      padding: "10px 14px", minWidth: 120, flex: "1 1 140px",
      border: "1px solid var(--border)",
    }}>
      <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, color: color || "var(--fg)" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────

export default function AnalyticsV3() {
  const [subTab, setSubTab] = useState<SubTab>("overview");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [migrated, setMigrated] = useState(false);
  const [dbStats, setDbStats] = useState<{ trades: number; sessions: number; closed_sessions: number } | null>(null);

  // Data
  const [overview, setOverview] = useState<Overview | null>(null);
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([]);
  const [drawdownData, setDrawdownData] = useState<{ curve: DrawdownPoint[]; max_drawdown_usd: number; max_drawdown_pct: number } | null>(null);
  const [hourly, setHourly] = useState<HourlyRow[]>([]);
  const [weekday, setWeekday] = useState<WeekdayRow[]>([]);
  const [heatmap, setHeatmap] = useState<HeatmapCell[]>([]);
  const [entryMin, setEntryMin] = useState<EntryMinRow[]>([]);
  const [exitBuckets, setExitBuckets] = useState<BucketRow[]>([]);
  const [pnlDist, setPnlDist] = useState<{ histogram: HistBin[]; percentiles: Record<string, number>; mean: number; median: number } | null>(null);
  const [dcaData, setDcaData] = useState<any>(null);
  const [recoveryData, setRecoveryData] = useState<any>(null);
  const [tpData, setTpData] = useState<any>(null);
  const [sideData, setSideData] = useState<any>(null);
  const [slippageData, setSlippageData] = useState<any>(null);
  const [feeData, setFeeData] = useState<any>(null);
  const [backtestTp, setBacktestTp] = useState<any>(null);
  const [backtestEntry, setBacktestEntry] = useState<any>(null);
  const [signalData, setSignalData] = useState<any>(null);
  const [regimeData, setRegimeData] = useState<any>(null);
  const [insights, setInsights] = useState<{ insights: Insight[]; total_insights: number } | null>(null);
  const [recommendations, setRecommendations] = useState<any>(null);

  // Migration + DB stats check
  const checkDb = useCallback(async () => {
    try {
      const stats = await api<any>("/api/analytics/db-stats");
      setDbStats(stats);
      if (stats.sessions > 0) setMigrated(true);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { checkDb(); }, [checkDb]);

  const runMigration = async () => {
    setLoading(true);
    setError(null);
    try {
      await api<any>("/api/analytics/migrate", { method: "POST" });
      await checkDb();
      setMigrated(true);
    } catch (e: any) {
      setError(e.message || "Migration failed");
    }
    setLoading(false);
  };

  // Load data per sub-tab
  const loadOverview = useCallback(async () => {
    setLoading(true);
    try {
      const [ov, ec] = await Promise.all([
        api<Overview>("/api/analytics/overview"),
        api<{ curve: EquityPoint[] }>("/api/analytics/equity-curve"),
      ]);
      setOverview(ov);
      setEquityCurve(ec.curve);
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  }, []);

  const loadTiming = useCallback(async () => {
    setLoading(true);
    try {
      const [h, w, hm, em, oe] = await Promise.all([
        api<{ hourly: HourlyRow[] }>("/api/analytics/timing/hourly"),
        api<{ weekday: WeekdayRow[] }>("/api/analytics/timing/weekday"),
        api<{ heatmap: HeatmapCell[] }>("/api/analytics/timing/heatmap"),
        api<{ entry_minute: EntryMinRow[] }>("/api/analytics/timing/entry-minute"),
        api<{ buckets: BucketRow[] }>("/api/analytics/timing/optimal-exit"),
      ]);
      setHourly(h.hourly);
      setWeekday(w.weekday);
      setHeatmap(hm.heatmap);
      setEntryMin(em.entry_minute);
      setExitBuckets(oe.buckets);
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  }, []);

  const loadStrategy = useCallback(async () => {
    setLoading(true);
    try {
      const [d, r, t, s] = await Promise.all([
        api<any>("/api/analytics/strategy/dca"),
        api<any>("/api/analytics/strategy/loss-recovery"),
        api<any>("/api/analytics/strategy/tp-analysis"),
        api<any>("/api/analytics/strategy/side-preference"),
      ]);
      setDcaData(d);
      setRecoveryData(r);
      setTpData(t);
      setSideData(s);
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  }, []);

  const loadRisk = useCallback(async () => {
    setLoading(true);
    try {
      const [dd, sl, fe, pd] = await Promise.all([
        api<any>("/api/analytics/risk/drawdown"),
        api<any>("/api/analytics/risk/slippage"),
        api<any>("/api/analytics/risk/fees"),
        api<any>("/api/analytics/risk/pnl-distribution"),
      ]);
      setDrawdownData(dd);
      setSlippageData(sl);
      setFeeData(fe);
      setPnlDist(pd);
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  }, []);

  const loadBacktest = useCallback(async () => {
    setLoading(true);
    try {
      const [tp, en, sig, reg] = await Promise.all([
        api<any>("/api/analytics/backtest/optimal-tp"),
        api<any>("/api/analytics/backtest/optimal-entry"),
        api<any>("/api/analytics/signals/accuracy"),
        api<any>("/api/analytics/market/volatility-regimes"),
      ]);
      setBacktestTp(tp);
      setBacktestEntry(en);
      setSignalData(sig);
      setRegimeData(reg);
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  }, []);

  const loadInsights = useCallback(async () => {
    setLoading(true);
    try {
      const [ins, rec] = await Promise.all([
        api<any>("/api/analytics/insights"),
        api<any>("/api/analytics/recommendations"),
      ]);
      setInsights(ins);
      setRecommendations(rec);
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (!migrated) return;
    if (subTab === "overview") loadOverview();
    else if (subTab === "timing") loadTiming();
    else if (subTab === "strategy") loadStrategy();
    else if (subTab === "risk") loadRisk();
    else if (subTab === "backtest") loadBacktest();
    else if (subTab === "insights") loadInsights();
  }, [subTab, migrated, loadOverview, loadTiming, loadStrategy, loadRisk, loadBacktest, loadInsights]);

  // ── Not migrated yet ─────────────────────────────────────────────────────
  if (!migrated) {
    return (
      <Card>
        <SectionTitle>Analytics V3 — Migration Required</SectionTitle>
        <p style={{ color: "var(--muted)", marginBottom: 12 }}>
          First-time setup: migrate trade history from JSON to SQLite for fast analytics.
        </p>
        {dbStats && (
          <p style={{ fontSize: 12, color: "var(--muted)" }}>
            DB status: {dbStats.trades} trades, {dbStats.sessions} sessions, {dbStats.closed_sessions} closed
          </p>
        )}
        <Button onClick={runMigration} disabled={loading}>
          {loading ? "Migrating..." : "Run Migration"}
        </Button>
        {error && <div style={{ color: "#ff4444", marginTop: 8, fontSize: 12 }}>{error}</div>}
      </Card>
    );
  }

  // ── Sub-tab navigation ──────────────────────────────────────────────────��─
  const subTabs: [SubTab, string][] = [
    ["overview", "סקירה כללית"],
    ["timing", "תזמון"],
    ["strategy", "אסטרטגיה"],
    ["risk", "סיכונים"],
    ["backtest", "בקטסט"],
    ["insights", "תובנות"],
  ];

  return (
    <div>
      <div style={{ display: "flex", gap: 6, marginBottom: 16, flexWrap: "wrap" }}>
        {subTabs.map(([k, l]) => (
          <button
            key={k}
            type="button"
            onClick={() => setSubTab(k)}
            style={{
              padding: "6px 14px", borderRadius: "var(--radius-sm)", fontSize: 13, cursor: "pointer",
              background: subTab === k ? "var(--accent)" : "var(--bg-elevated)",
              color: subTab === k ? "#fff" : "var(--fg)",
              border: `1px solid ${subTab === k ? "var(--accent)" : "var(--border)"}`,
            }}
          >
            {l}
          </button>
        ))}
      </div>

      {loading && <div style={{ color: "var(--muted)", marginBottom: 8 }}>Loading...</div>}
      {error && <div style={{ color: "#ff4444", marginBottom: 8, fontSize: 12 }}>{error}</div>}

      {/* ── Overview Tab ───────────────────────────────────────────────���──── */}
      {subTab === "overview" && overview && (
        <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
            <MetricCard label="Win Rate" value={fmtPct(overview.win_rate_pct)}
              color={overview.win_rate_pct >= 50 ? "#44cc44" : "#ff4444"}
              sub={`${overview.win_count}W / ${overview.loss_count}L`} />
            <MetricCard label="Total PnL" value={fmtUsd(overview.total_pnl_usd)}
              color={overview.total_pnl_usd >= 0 ? "#44cc44" : "#ff4444"} />
            <MetricCard label="Expectancy" value={fmtUsd(overview.expectancy_usd)}
              color={overview.expectancy_usd >= 0 ? "#44cc44" : "#ff4444"}
              sub="per trade" />
            <MetricCard label="Profit Factor" value={fmtNum(overview.profit_factor, 2)} />
            <MetricCard label="Sharpe" value={fmtNum(overview.sharpe_ratio, 2)} />
            <MetricCard label="Max Drawdown" value={fmtUsd(overview.max_drawdown_usd)}
              color="#ff4444" />
            <MetricCard label="R:R" value={fmtNum(overview.rr_ratio, 2)} />
            <MetricCard label="Avg Duration" value={`${overview.avg_duration_sec.toFixed(0)}s`} />
          </div>

          {/* Streaks */}
          <Card style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", gap: 20, fontSize: 13 }}>
              <span>Max Win Streak: <b>{overview.streaks.max_win_streak}</b></span>
              <span>Max Loss Streak: <b style={{ color: "#ff4444" }}>{overview.streaks.max_loss_streak}</b></span>
              <span>Current: <b style={{ color: overview.streaks.current_streak_type === "win" ? "#44cc44" : "#ff4444" }}>
                {overview.streaks.current_streak_length} {overview.streaks.current_streak_type}
              </b></span>
            </div>
          </Card>

          {/* By Exit Type */}
          <Card style={{ marginBottom: 12 }}>
            <SectionTitle as="h3">By Exit Type</SectionTitle>
            <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
              {Object.entries(overview.by_exit_type).map(([k, v]) => (
                <span key={k}>{k}: {v.count} trades, <b style={{ color: v.pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(v.pnl)}</b></span>
              ))}
            </div>
          </Card>

          {/* Equity Curve */}
          {equityCurve.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Equity Curve</SectionTitle>
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={equityCurve}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="ts" tickFormatter={fmtTs} fontSize={10} stroke="var(--muted)" />
                  <YAxis fontSize={10} stroke="var(--muted)" tickFormatter={(v: number) => `$${v}`} />
                  <Tooltip formatter={(v: number) => `$${v.toFixed(2)}`} labelFormatter={fmtTs} />
                  <ReferenceLine y={0} stroke="var(--muted)" strokeDasharray="3 3" />
                  <Area type="monotone" dataKey="cumulative_pnl" stroke="#44aaff" fill="#44aaff" fillOpacity={0.15} />
                </AreaChart>
              </ResponsiveContainer>
            </Card>
          )}
        </>
      )}

      {/* ── Timing Tab ────────────────────────────────────────────────────── */}
      {subTab === "timing" && (
        <>
          {/* Hourly Performance */}
          {hourly.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Win Rate by Hour (UTC)</SectionTitle>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={hourly}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="hour" fontSize={10} stroke="var(--muted)" />
                  <YAxis fontSize={10} stroke="var(--muted)" domain={[0, 100]} tickFormatter={(v: number) => `${v}%`} />
                  <Tooltip formatter={(v: number, name: string) => name === "win_rate_pct" ? `${v}%` : `$${v}`} />
                  <Bar dataKey="win_rate_pct" name="Win Rate">
                    {hourly.map((h, i) => (
                      <Cell key={i} fill={h.win_rate_pct >= 50 ? "#44cc44" : "#ff4444"} fillOpacity={0.7} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Weekday Performance */}
          {weekday.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Win Rate by Day</SectionTitle>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={weekday}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="day_name" fontSize={11} stroke="var(--muted)" />
                  <YAxis fontSize={10} stroke="var(--muted)" domain={[0, 100]} tickFormatter={(v: number) => `${v}%`} />
                  <Tooltip />
                  <Bar dataKey="win_rate_pct" name="Win Rate" fill="#44aaff" fillOpacity={0.7} />
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Entry Minute */}
          {entryMin.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Win Rate by Entry Minute in Window</SectionTitle>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={entryMin}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="entry_minute" fontSize={10} stroke="var(--muted)" label={{ value: "min", position: "insideBottomRight", fontSize: 10 }} />
                  <YAxis fontSize={10} stroke="var(--muted)" domain={[0, 100]} tickFormatter={(v: number) => `${v}%`} />
                  <Tooltip />
                  <Bar dataKey="win_rate_pct" name="Win Rate">
                    {entryMin.map((e, i) => (
                      <Cell key={i} fill={e.win_rate_pct >= 50 ? "#44cc44" : "#ff4444"} fillOpacity={0.7} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Optimal Exit */}
          {exitBuckets.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Holding Duration vs Performance</SectionTitle>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={exitBuckets}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="bucket" fontSize={10} stroke="var(--muted)" />
                  <YAxis fontSize={10} stroke="var(--muted)" />
                  <Tooltip />
                  <Bar dataKey="avg_pnl" name="Avg PnL">
                    {exitBuckets.map((b, i) => (
                      <Cell key={i} fill={b.avg_pnl >= 0 ? "#44cc44" : "#ff4444"} fillOpacity={0.7} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Heatmap */}
          {heatmap.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Hour x Day Heatmap</SectionTitle>
              <div style={{ overflowX: "auto" }}>
                <table style={{ borderCollapse: "collapse", fontSize: 11, width: "100%" }}>
                  <thead>
                    <tr>
                      <th style={{ padding: "4px 8px", textAlign: "left" }}>Day/Hour</th>
                      {Array.from({ length: 24 }, (_, i) => (
                        <th key={i} style={{ padding: "4px 4px", textAlign: "center", minWidth: 30 }}>{i}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, di) => (
                      <tr key={day}>
                        <td style={{ padding: "4px 8px", fontWeight: 600 }}>{day}</td>
                        {Array.from({ length: 24 }, (_, hi) => {
                          const cell = heatmap.find(c => c.weekday === di && c.hour === hi);
                          const wr = cell?.win_rate_pct ?? null;
                          const bg = wr == null ? "transparent"
                            : wr >= 60 ? `rgba(68,204,68,${0.2 + 0.6 * (wr - 50) / 50})`
                            : wr >= 50 ? `rgba(68,204,68,0.15)`
                            : `rgba(255,68,68,${0.2 + 0.6 * (50 - wr) / 50})`;
                          return (
                            <td key={hi} style={{ padding: "4px 2px", textAlign: "center", background: bg, borderRadius: 2 }}
                              title={cell ? `${cell.total} trades, ${wr}% WR` : "no data"}>
                              {cell ? `${wr?.toFixed(0)}` : ""}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
        </>
      )}

      {/* ── Strategy Tab ──────────────────────────────────────────────────── */}
      {subTab === "strategy" && (
        <>
          {/* DCA */}
          {dcaData && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">DCA Effectiveness</SectionTitle>
              <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <th style={thStyle}>Type</th><th style={thStyle}>Trades</th><th style={thStyle}>Win Rate</th>
                    <th style={thStyle}>Avg PnL</th><th style={thStyle}>Total PnL</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td style={tdStyle}>Single Entry</td>
                    <td style={tdStyle}>{dcaData.single_entry.total}</td>
                    <td style={tdStyle}>{fmtPct(dcaData.single_entry.win_rate_pct)}</td>
                    <td style={{ ...tdStyle, color: dcaData.single_entry.avg_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(dcaData.single_entry.avg_pnl)}</td>
                    <td style={{ ...tdStyle, color: dcaData.single_entry.total_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(dcaData.single_entry.total_pnl)}</td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>DCA</td>
                    <td style={tdStyle}>{dcaData.dca.total}</td>
                    <td style={tdStyle}>{fmtPct(dcaData.dca.win_rate_pct)}</td>
                    <td style={{ ...tdStyle, color: dcaData.dca.avg_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(dcaData.dca.avg_pnl)}</td>
                    <td style={{ ...tdStyle, color: dcaData.dca.total_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(dcaData.dca.total_pnl)}</td>
                  </tr>
                </tbody>
              </table>
              {dcaData.dca_improves_win_rate != null && (
                <p style={{ marginTop: 8, fontSize: 12, color: dcaData.dca_improves_win_rate ? "#44cc44" : "#ff4444" }}>
                  {dcaData.dca_improves_win_rate ? "DCA improves win rate" : "DCA does NOT improve win rate"}
                </p>
              )}
            </Card>
          )}

          {/* Loss Recovery */}
          {recoveryData && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Loss Recovery Analysis</SectionTitle>
              <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <th style={thStyle}>Type</th><th style={thStyle}>Trades</th><th style={thStyle}>Win Rate</th>
                    <th style={thStyle}>Avg PnL</th><th style={thStyle}>Total PnL</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td style={tdStyle}>Normal (1x)</td>
                    <td style={tdStyle}>{recoveryData.normal.total}</td>
                    <td style={tdStyle}>{fmtPct(recoveryData.normal.win_rate_pct)}</td>
                    <td style={tdStyle}>{fmtUsd(recoveryData.normal.avg_pnl)}</td>
                    <td style={tdStyle}>{fmtUsd(recoveryData.normal.total_pnl)}</td>
                  </tr>
                  <tr>
                    <td style={tdStyle}>Recovery ({fmtNum(recoveryData.recovery.avg_multiplier, 1)}x avg)</td>
                    <td style={tdStyle}>{recoveryData.recovery.total}</td>
                    <td style={tdStyle}>{fmtPct(recoveryData.recovery.win_rate_pct)}</td>
                    <td style={tdStyle}>{fmtUsd(recoveryData.recovery.avg_pnl)}</td>
                    <td style={{ ...tdStyle, color: recoveryData.recovery_net_profitable ? "#44cc44" : "#ff4444" }}>{fmtUsd(recoveryData.recovery.total_pnl)}</td>
                  </tr>
                </tbody>
              </table>
              <p style={{ marginTop: 8, fontSize: 12, color: recoveryData.recovery_net_profitable ? "#44cc44" : "#ff4444" }}>
                Recovery net: {fmtUsd(recoveryData.recovery_net_pnl)} — {recoveryData.recovery_net_profitable ? "PROFITABLE" : "LOSING"}
              </p>
            </Card>
          )}

          {/* TP Analysis */}
          {tpData && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Take Profit Analysis</SectionTitle>
              <div style={{ display: "flex", gap: 16, fontSize: 12, flexWrap: "wrap" }}>
                <span>TP exits: <b>{tpData.tp_sessions}</b></span>
                <span>Expire exits: <b>{tpData.expire_sessions}</b></span>
                {tpData.tp_avg_left_on_table_pct != null && (
                  <span>Avg left on table: <b style={{ color: "#ffaa00" }}>{fmtPct(tpData.tp_avg_left_on_table_pct)}</b></span>
                )}
                {tpData.expire_had_tp_chance_pct != null && (
                  <span>Expiry had TP chance: <b>{fmtPct(tpData.expire_had_tp_chance_pct)}</b></span>
                )}
              </div>
            </Card>
          )}

          {/* Side Preference */}
          {sideData && sideData.by_side && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Side Preference: Up vs Down</SectionTitle>
              <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <th style={thStyle}>Side</th><th style={thStyle}>Trades</th><th style={thStyle}>Win Rate</th>
                    <th style={thStyle}>Avg PnL</th><th style={thStyle}>Total PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(sideData.by_side).map(([side, d]: [string, any]) => (
                    <tr key={side}>
                      <td style={{ ...tdStyle, fontWeight: side === sideData.better_side ? 700 : 400 }}>{side}</td>
                      <td style={tdStyle}>{d.total}</td>
                      <td style={tdStyle}>{fmtPct(d.win_rate_pct)}</td>
                      <td style={{ ...tdStyle, color: d.avg_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(d.avg_pnl)}</td>
                      <td style={{ ...tdStyle, color: d.total_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(d.total_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {sideData.better_side && (
                <p style={{ marginTop: 8, fontSize: 12, color: "#44aaff" }}>{sideData.recommendation}</p>
              )}
            </Card>
          )}
        </>
      )}

      {/* ── Risk Tab ──────────────────────────────────────────────────────── */}
      {subTab === "risk" && (
        <>
          {/* Drawdown Chart */}
          {drawdownData && drawdownData.curve.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Drawdown Curve</SectionTitle>
              <div style={{ display: "flex", gap: 16, fontSize: 12, marginBottom: 8 }}>
                <span>Max DD: <b style={{ color: "#ff4444" }}>{fmtUsd(drawdownData.max_drawdown_usd)}</b></span>
                <span>Max DD%: <b style={{ color: "#ff4444" }}>{fmtPct(drawdownData.max_drawdown_pct)}</b></span>
              </div>
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={drawdownData.curve}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="ts" tickFormatter={fmtTs} fontSize={10} stroke="var(--muted)" />
                  <YAxis fontSize={10} stroke="var(--muted)" tickFormatter={(v: number) => `$${v}`} />
                  <Tooltip formatter={(v: number) => `$${v.toFixed(2)}`} labelFormatter={fmtTs} />
                  <ReferenceLine y={0} stroke="var(--muted)" />
                  <Area type="monotone" dataKey="drawdown" stroke="#ff4444" fill="#ff4444" fillOpacity={0.2} />
                </AreaChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* PnL Distribution */}
          {pnlDist && pnlDist.histogram.length > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">PnL Distribution</SectionTitle>
              <div style={{ display: "flex", gap: 16, fontSize: 12, marginBottom: 8 }}>
                <span>Mean: <b>{fmtUsd(pnlDist.mean)}</b></span>
                <span>Median: <b>{fmtUsd(pnlDist.median)}</b></span>
                <span>P10: {fmtUsd(pnlDist.percentiles.p10)}</span>
                <span>P90: {fmtUsd(pnlDist.percentiles.p90)}</span>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={pnlDist.histogram}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="bin_start" fontSize={10} stroke="var(--muted)" tickFormatter={(v: number) => `$${v.toFixed(1)}`} />
                  <YAxis fontSize={10} stroke="var(--muted)" />
                  <Tooltip labelFormatter={(v: number) => `$${v.toFixed(2)}`} />
                  <Bar dataKey="count" name="Trades">
                    {pnlDist.histogram.map((b, i) => (
                      <Cell key={i} fill={b.bin_start >= 0 ? "#44cc44" : "#ff4444"} fillOpacity={0.7} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Slippage */}
          {slippageData && slippageData.total_trades > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Slippage Analysis</SectionTitle>
              <div style={{ display: "flex", gap: 16, fontSize: 12, flexWrap: "wrap" }}>
                <span>Avg Slippage: <b>{fmtNum(slippageData.avg_slippage_cents, 3)}c</b></span>
                <span>Total Cost: <b style={{ color: "#ff4444" }}>{fmtUsd(slippageData.total_slippage_usd)}</b></span>
                <span>Worse: {slippageData.worse_than_limit_count} ({fmtPct(slippageData.worse_pct)})</span>
                <span>Better: {slippageData.better_than_limit_count}</span>
                <span>Exact: {slippageData.exact_fill_count}</span>
              </div>
            </Card>
          )}

          {/* Fee Impact */}
          {feeData && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Fee Impact</SectionTitle>
              <div style={{ display: "flex", gap: 16, fontSize: 12, flexWrap: "wrap" }}>
                <span>Total Fees: <b style={{ color: "#ff4444" }}>{fmtUsd(feeData.total_fees_usd)}</b></span>
                <span>Avg Fee: {fmtUsd(feeData.avg_fee_usd)}</span>
                <span>Net PnL: <b>{fmtUsd(feeData.net_pnl_usd)}</b></span>
                <span>Gross (no fees): <b>{fmtUsd(feeData.gross_pnl_without_fees)}</b></span>
                <span>Fee Drag: <b style={{ color: "#ffaa00" }}>{fmtPct(feeData.fee_drag_pct)}</b></span>
              </div>
            </Card>
          )}
        </>
      )}

      {/* ── Backtest Tab ──────────────────────────────────────────────────── */}
      {subTab === "backtest" && (
        <>
          {/* Optimal TP */}
          {backtestTp && backtestTp.results && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Optimal TP% (Grid Search)</SectionTitle>
              <p style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
                Best by avg PnL: <b style={{ color: "#44aaff" }}>{backtestTp.optimal_tp_by_avg_pnl}%</b>
                {" | "}Best by total PnL: <b style={{ color: "#44aaff" }}>{backtestTp.optimal_tp_by_total_pnl}%</b>
              </p>
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={backtestTp.results}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="tp_pct" fontSize={10} stroke="var(--muted)" label={{ value: "TP%", position: "insideBottomRight", fontSize: 10 }} />
                  <YAxis fontSize={10} stroke="var(--muted)" tickFormatter={(v: number) => `$${v}`} />
                  <Tooltip />
                  <Line type="monotone" dataKey="simulated_avg_pnl" name="Avg PnL" stroke="#44aaff" dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Optimal Entry */}
          {backtestEntry && backtestEntry.results && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Optimal Entry Price (Grid Search)</SectionTitle>
              <p style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
                Best entry threshold: <b style={{ color: "#44aaff" }}>{backtestEntry.optimal_entry_cents}c</b>
              </p>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={backtestEntry.results}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="entry_cents" fontSize={10} stroke="var(--muted)" />
                  <YAxis fontSize={10} stroke="var(--muted)" />
                  <Tooltip />
                  <Bar dataKey="accepted.avg_pnl" name="Avg PnL (accepted)">
                    {backtestEntry.results.map((r: any, i: number) => (
                      <Cell key={i} fill={r.accepted?.avg_pnl >= 0 ? "#44cc44" : "#ff4444"} fillOpacity={0.7} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* Signal Accuracy */}
          {signalData && signalData.total > 0 && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Signal Accuracy</SectionTitle>
              <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <th style={thStyle}>Gate</th><th style={thStyle}>Trades</th><th style={thStyle}>Win Rate</th><th style={thStyle}>Avg PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(signalData.by_gate).map(([gate, d]: [string, any]) => (
                    <tr key={gate}>
                      <td style={tdStyle}>{gate}</td>
                      <td style={tdStyle}>{d.total}</td>
                      <td style={tdStyle}>{fmtPct(d.win_rate_pct)}</td>
                      <td style={{ ...tdStyle, color: d.avg_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(d.avg_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}

          {/* Volatility Regimes */}
          {regimeData && regimeData.regimes && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Volatility Regimes</SectionTitle>
              <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <th style={thStyle}>Regime</th><th style={thStyle}>Windows</th><th style={thStyle}>Trades</th>
                    <th style={thStyle}>Win Rate</th><th style={thStyle}>Avg PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {regimeData.regimes.map((r: any) => (
                    <tr key={r.regime}>
                      <td style={{ ...tdStyle, fontWeight: r.regime === regimeData.best_regime ? 700 : 400 }}>{r.label}</td>
                      <td style={tdStyle}>{r.windows}</td>
                      <td style={tdStyle}>{r.trades}</td>
                      <td style={tdStyle}>{fmtPct(r.win_rate_pct)}</td>
                      <td style={{ ...tdStyle, color: r.avg_pnl >= 0 ? "#44cc44" : "#ff4444" }}>{fmtUsd(r.avg_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </>
      )}

      {/* ── Insights Tab ──────────────────────────────────────────────────── */}
      {subTab === "insights" && (
        <>
          {insights && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Automated Insights ({insights.total_insights})</SectionTitle>
              {insights.insights.length === 0 && (
                <p style={{ color: "var(--muted)", fontSize: 12 }}>No insights yet — run migration and accumulate more data</p>
              )}
              {insights.insights.map((ins, i) => (
                <div key={i} style={{
                  padding: "8px 12px", marginBottom: 6, borderRadius: "var(--radius-sm)",
                  borderRight: `3px solid ${LEVEL_COLORS[ins.level] || "#888"}`,
                  background: "var(--bg-elevated)", fontSize: 12,
                }}>
                  <span style={{ color: LEVEL_COLORS[ins.level], fontWeight: 700, marginLeft: 4 }}>
                    [{LEVEL_ICONS[ins.level]}]
                  </span>{" "}
                  <span style={{ color: "var(--muted)" }}>[{ins.category}]</span>{" "}
                  {ins.message}
                </div>
              ))}
            </Card>
          )}

          {recommendations && recommendations.recommendations && (
            <Card style={{ marginBottom: 12 }}>
              <SectionTitle as="h3">Config Recommendations ({recommendations.total})</SectionTitle>
              {recommendations.recommendations.map((rec: any, i: number) => (
                <div key={i} style={{
                  padding: "8px 12px", marginBottom: 6, borderRadius: "var(--radius-sm)",
                  background: "var(--bg-elevated)", fontSize: 12,
                  borderRight: "3px solid #44aaff",
                }}>
                  <div><b>{rec.param}</b> <span style={{ color: "var(--muted)" }}>({rec.confidence} confidence)</span></div>
                  <div style={{ color: "var(--muted)", marginTop: 2 }}>{rec.current_issue}</div>
                  <div style={{ color: "#44aaff", marginTop: 2 }}>{rec.suggestion}</div>
                </div>
              ))}
            </Card>
          )}
        </>
      )}
    </div>
  );
}

// ── Style helpers ─────────────────────────────────────────────────────────
const thStyle: React.CSSProperties = { padding: "6px 10px", textAlign: "right", fontSize: 11, color: "var(--muted)" };
const tdStyle: React.CSSProperties = { padding: "6px 10px", textAlign: "right", fontSize: 12 };
