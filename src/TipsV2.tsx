import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";

type TipsV2Metrics = {
  expectancy: number;
  win_rate: number;
  avg_win: number;
  avg_loss_abs: number;
  rr: number;
  expire_rate: number;
  total_count: number;
  avg_peak_roi_tp?: number | null;
  avg_trough_roi_tp?: number | null;
  avg_trough_roi_expire?: number | null;
  avg_after_tp_peak_delta_cents?: number | null;
  avg_after_tp_peak_delta_pct?: number | null;
  avg_after_tp_trough_delta_cents?: number | null;
  avg_after_tp_trough_delta_pct?: number | null;
  avg_entries_per_session?: number | null;
  avg_duration_sec?: number | null;
  time_buckets?: TimeBucketRow[] | null;
  optimal_exit_bucket?: TimeBucketRow | null;
};

type BinComparisonRow = {
  bin_value: unknown;
  expectancy: number;
  total_count: number;
  win_rate: number;
  expire_rate: number;
  recommended: boolean;
};

type TipMode = "full" | "no_contrast" | "insufficient_data";

type Tip = {
  key: string;
  label: string;
  tip_mode?: TipMode;
  current_value: unknown;
  recommended_value: unknown;
  action: string;
  metrics: TipsV2Metrics | null;
  reasoning: string;
  bin_comparison?: BinComparisonRow[] | null;
};

type DataQuality = {
  runs_used: number;
  sessions_total: number;
  params_with_contrast: number;
  params_without_contrast: number;
  params_insufficient: number;
};

type SideMetricsExtra = {
  sessions: number;
  expectancy: number | null;
  tp_win_pct: number | null;
  tp_count?: number;
  expire_count?: number;
};

type PnlPercentiles = {
  p10: number | null;
  p50: number | null;
  p90: number | null;
  min: number;
  max: number;
};

type TimeBucketRow = {
  bucket: string;
  count: number;
  tp_count: number;
  expire_count: number;
  expectancy: number | null;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss_abs: number | null;
};

type ExtendedMetrics = {
  profit_factor: number | null;
  avg_duration_tp_sec: number | null;
  avg_duration_expire_sec: number | null;
  pnl_percentiles: PnlPercentiles | null;
  entry_spread_avg_usd: number | null;
  entry_spread_avg_cents: number | null;
  entry_spread_sessions: number;
  by_side: { Up: SideMetricsExtra; Down: SideMetricsExtra };
  low_sample_warning: boolean;
  low_sample_message: string | null;
  // duration distribution
  duration_min: number | null;
  duration_max: number | null;
  duration_p10: number | null;
  duration_p50: number | null;
  duration_p90: number | null;
  duration_p10_tp: number | null;
  duration_p50_tp: number | null;
  duration_p90_tp: number | null;
  duration_p10_expire: number | null;
  duration_p50_expire: number | null;
  duration_p90_expire: number | null;
  time_buckets: TimeBucketRow[] | null;
  optimal_exit_bucket: TimeBucketRow | null;
};

type WindowComparisonSlice = {
  expectancy?: number | null;
  sessions_total?: number | null;
  profit_factor?: number | null;
  tp_win_pct?: number | null;
};

/** טיפים + מדדים לפי שוק Polymarket (5m / 15m) — לא מעורבבים */
type TipsWindowBundle = {
  title: string;
  tips: Tip[];
  summary: string;
  global_metrics?: TipsV2Metrics | null;
  global_narrative?: string | null;
  data_quality?: DataQuality | null;
  extended_metrics?: ExtendedMetrics | null;
};

type TipsV2Response = {
  generated_at: number;
  summary: string;
  global_metrics?: TipsV2Metrics | null;
  global_narrative?: string | null;
  data_quality?: DataQuality | null;
  tips: Tip[];
  by_btc_window?: Record<string, TipsWindowBundle>;
  window_comparison?: Record<string, WindowComparisonSlice>;
  guardrails?: { min_samples?: number; use_guardrails?: boolean };
  note?: string;
};

type TipsV2RunFile = { name: string; size_bytes: number | null };
type TipsV2RunRow = {
  run_key: string;
  counts_toward_v3: boolean;
  mtime: number;
  files: TipsV2RunFile[];
  trade_rows: number | null;
};
type TipsV2RunsResponse = {
  runs_root: string;
  runs: TipsV2RunRow[];
  total_runs?: number;
  limit_applied?: number;
  truncated?: boolean;
};

function fmtPct01(x: number): string {
  if (!Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(1)}%`;
}

function fmtMaybe(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    if (v === Math.floor(v) && Math.abs(v) < 1e9) return String(v);
    return v.toFixed(2);
  }
  return String(v);
}

function fmtMaybeSigned(v: number | null | undefined, decimals = 1): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(decimals)}`;
}

function fmtMaybeSignedPct(v: number | null | undefined, decimals = 1): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${fmtMaybeSigned(v, decimals)}%`;
}

function fmtMaybeSignedCents(v: number | null | undefined, decimals = 1): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${fmtMaybeSigned(v, decimals)}¢`;
}

function fmtBinValue(v: unknown): string {
  if (typeof v === "boolean") return v ? "כן" : "לא";
  return fmtMaybe(v);
}

function fmtProfitFactor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toFixed(2);
}

function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function TipsV2RunsPanel({ onRunsChanged }: { onRunsChanged: () => void }) {
  const [rdata, setRdata] = useState<TipsV2RunsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);
  const [selectedRunKey, setSelectedRunKey] = useState<string | null>(null);
  const [filterQuery, setFilterQuery] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const res = await api<TipsV2RunsResponse>("/api/strategy/tips-v2/runs?limit=5000");
      setRdata(res);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "שגיאה בטעינת רשימת ריצות");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredRuns = useMemo(() => {
    if (!rdata?.runs?.length) return [];
    const q = filterQuery.trim().toLowerCase();
    if (!q) return rdata.runs;
    return rdata.runs.filter((r) => r.run_key.toLowerCase().includes(q));
  }, [rdata, filterQuery]);

  useEffect(() => {
    if (selectedRunKey && !filteredRuns.some((r) => r.run_key === selectedRunKey)) {
      setSelectedRunKey(null);
    }
  }, [filteredRuns, selectedRunKey]);

  const handleDelete = async (run_key: string) => {
    if (
      !window.confirm(
        `למחוק את כל תיקיית הריצה "${run_key}"?\nיימחקו כל הקבצים בתיקייה והנתונים לא ייכנסו יותר לסטטיסטיקת ניתוח v3. לא ניתן לבטל.`,
      )
    ) {
      return;
    }
    setDeleting(run_key);
    setErr("");
    try {
      await api<{ ok: boolean }>("/api/strategy/tips-v2/delete-run", {
        method: "POST",
        body: JSON.stringify({ run_key }),
      });
      if (selectedRunKey === run_key) setSelectedRunKey(null);
      await load();
      onRunsChanged();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "מחיקה נכשלה");
    } finally {
      setDeleting(null);
    }
  };

  const totalOnDisk = rdata?.total_runs ?? rdata?.runs?.length ?? 0;
  const truncated = Boolean(rdata?.truncated);

  return (
    <div
      style={{
        marginBottom: 20,
        padding: 14,
        borderRadius: 12,
        border: "1px solid #334155",
        background: "#0f172a",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: 10,
          marginBottom: 10,
        }}
      >
        <div>
          <h3 style={{ margin: "0 0 6px 0", color: "#e2e8f0", fontSize: 16 }}>
            כל הריצות — בחירה ומחיקה (מסירה מסטטיסטיקת v3)
          </h3>
          <p style={{ margin: 0, fontSize: 12, color: "var(--muted)", lineHeight: 1.55, maxWidth: 720 }}>
            אם ריצה אחת נפגעה מבאג או נתונים שגויים, בחר אותה ברשימה ומחק — הניתוח יתעדכן בלי אותה ריצה. ריצות עם{" "}
            <strong style={{ color: "#94a3b8" }}>«כן»</strong> בניתוח v3 הן אלה שנכנסות לחישוב ההמלצות.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            style={{
              padding: "6px 12px",
              borderRadius: 8,
              border: "1px solid #475569",
              background: loading ? "#1e293b" : "#334155",
              color: "#e2e8f0",
              cursor: loading ? "default" : "pointer",
              fontSize: 12,
            }}
          >
            רענון רשימה
          </button>
          <button
            type="button"
            disabled={!selectedRunKey || deleting !== null}
            onClick={() => selectedRunKey && void handleDelete(selectedRunKey)}
            style={{
              padding: "6px 14px",
              borderRadius: 8,
              border: "1px solid #7f1d1d",
              background: !selectedRunKey || deleting ? "#450a0a" : "#b91c1c",
              color: "#fecaca",
              cursor: !selectedRunKey || deleting ? "not-allowed" : "pointer",
              fontSize: 12,
              fontWeight: 700,
            }}
          >
            מחק את הריצה הנבחרת
          </button>
        </div>
      </div>

      <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 10, lineHeight: 1.5 }}>
        נתיב בשרת: <code style={{ fontSize: 11, color: "#94a3b8" }}>{rdata?.runs_root ?? "…"}</code>
        {" · "}
        {loading ? (
          "טוען…"
        ) : (
          <>
            <strong style={{ color: "#cbd5e1" }}>{totalOnDisk}</strong> ריצות בדיסק
            {truncated && rdata?.limit_applied != null && (
              <span style={{ color: "#fbbf24" }}>
                {" "}
                (מוצגות {rdata.runs.length} ראשונות מתוך {totalOnDisk} — הגדל limit ב-API אם צריך)
              </span>
            )}
          </>
        )}
      </div>

      <div style={{ marginBottom: 12, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <label style={{ fontSize: 12, color: "#94a3b8", display: "flex", alignItems: "center", gap: 6 }}>
          חיפוש לפי תאריך/שעה:
          <input
            type="search"
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            placeholder="למשל 2026-04 או 14-30"
            style={{
              minWidth: 200,
              padding: "6px 10px",
              borderRadius: 8,
              border: "1px solid #475569",
              background: "#1e293b",
              color: "#e2e8f0",
              fontSize: 12,
            }}
          />
        </label>
        {selectedRunKey && (
          <span style={{ fontSize: 12, color: "#7dd3fc" }}>
            נבחר: <code style={{ fontSize: 12 }}>{selectedRunKey}</code>
          </span>
        )}
      </div>

      {loading && <div style={{ color: "var(--muted)", fontSize: 13 }}>טוען את כל הריצות…</div>}
      {err && (
        <div style={{ color: "#f87171", marginBottom: 8, padding: 8, background: "#3f1f1f", fontSize: 12 }}>{err}</div>
      )}
      {rdata && rdata.runs.length === 0 && !loading && (
        <div style={{ color: "var(--muted)", fontSize: 13 }}>אין תיקיות ריצה תחת logs/runs.</div>
      )}
      {rdata && rdata.runs.length > 0 && !loading && (
        <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6 }}>
          מציג {filteredRuns.length} שורות
          {filterQuery.trim() ? ` (סינון מתוך ${rdata.runs.length})` : ""}
        </div>
      )}
      {rdata && rdata.runs.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "right", color: "#94a3b8" }}>
                <th style={{ padding: 6, borderBottom: "1px solid #334155", width: 36 }}>בחירה</th>
                <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>ריצה (יום / שעה)</th>
                <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>בניתוח v3</th>
                <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>שורות ב־trades.json</th>
                <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>עדכון</th>
                <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>קבצים</th>
                <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>מחיקה מהירה</th>
              </tr>
            </thead>
            <tbody>
              {filteredRuns.length === 0 && filterQuery.trim() !== "" && (
                <tr>
                  <td colSpan={7} style={{ padding: 12, color: "#94a3b8", textAlign: "center" }}>
                    אין תוצאות לחיפוש — נסה טקסט אחר או נקה את השדה
                  </td>
                </tr>
              )}
              {filteredRuns.map((row) => {
                const sel = selectedRunKey === row.run_key;
                return (
                  <tr
                    key={row.run_key}
                    onClick={() => setSelectedRunKey(row.run_key)}
                    style={{
                      color: "#e2e8f0",
                      verticalAlign: "top",
                      cursor: "pointer",
                      background: sel ? "rgba(30, 58, 95, 0.55)" : "transparent",
                    }}
                  >
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }} onClick={(e) => e.stopPropagation()}>
                      <input
                        type="radio"
                        name="tips-v2-run-pick"
                        checked={sel}
                        onChange={() => setSelectedRunKey(row.run_key)}
                        aria-label={`בחר ריצה ${row.run_key}`}
                      />
                    </td>
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b", fontFamily: "ui-monospace, monospace" }}>
                      {row.run_key}
                    </td>
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>
                      {row.counts_toward_v3 ? (
                        <span style={{ color: "#4ade80" }}>כן — משפיע</span>
                      ) : (
                        <span style={{ color: "#fbbf24" }}>לא</span>
                      )}
                    </td>
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>{fmtMaybe(row.trade_rows)}</td>
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b", whiteSpace: "nowrap" }}>
                      {new Date(row.mtime * 1000).toLocaleString("he-IL")}
                    </td>
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b", maxWidth: 280 }} onClick={(e) => e.stopPropagation()}>
                      <details style={{ fontSize: 11 }}>
                        <summary style={{ cursor: "pointer", color: "#94a3b8" }}>
                          {row.files.length} קבצים
                        </summary>
                        <ul style={{ margin: "6px 0 0 0", paddingInlineStart: 18, color: "#cbd5e1" }}>
                          {row.files.map((f) => (
                            <li key={f.name}>
                              {f.name} <span style={{ color: "#64748b" }}>({formatBytes(f.size_bytes)})</span>
                            </li>
                          ))}
                        </ul>
                      </details>
                    </td>
                    <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }} onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        disabled={deleting === row.run_key}
                        onClick={() => void handleDelete(row.run_key)}
                        style={{
                          padding: "4px 10px",
                          borderRadius: 6,
                          border: "1px solid #7f1d1d",
                          background: deleting === row.run_key ? "#450a0a" : "#991b1b",
                          color: "#fecaca",
                          cursor: deleting === row.run_key ? "wait" : "pointer",
                          fontSize: 11,
                        }}
                      >
                        {deleting === row.run_key ? "…" : "מחק"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function fmtSec(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${Math.round(v)}s`;
}

function TimeAnalyticsPanel({ em }: { em: ExtendedMetrics }) {
  const buckets = em.time_buckets;
  const optimal = em.optimal_exit_bucket;

  const hasDur =
    em.duration_min != null ||
    em.duration_p50 != null ||
    em.duration_max != null;

  return (
    <div
      style={{
        marginBottom: 14,
        padding: 12,
        borderRadius: 10,
        background: "#071220",
        border: "1px solid #1e3a5f",
        fontSize: 12,
        color: "#cbd5e1",
        lineHeight: 1.7,
      }}
    >
      <div style={{ fontWeight: 800, marginBottom: 8, color: "#7dd3fc", fontSize: 13 }}>
        ⏱ ניתוח זמן החזקה
      </div>

      {/* duration distribution */}
      {hasDur && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontWeight: 700, color: "#94a3b8", marginBottom: 4 }}>
            התפלגות משך עסקה (כל סוגים)
          </div>
          <div>
            min: {fmtSec(em.duration_min)} · p10: {fmtSec(em.duration_p10)} · p50:{" "}
            {fmtSec(em.duration_p50)} · p90: {fmtSec(em.duration_p90)} · max:{" "}
            {fmtSec(em.duration_max)}
          </div>
          <div style={{ marginTop: 4 }}>
            <span style={{ color: "#4ade80" }}>TP — </span>
            p10: {fmtSec(em.duration_p10_tp)} · p50: {fmtSec(em.duration_p50_tp)} · p90:{" "}
            {fmtSec(em.duration_p90_tp)}
          </div>
          <div>
            <span style={{ color: "#f87171" }}>EXPIRE — </span>
            p10: {fmtSec(em.duration_p10_expire)} · p50: {fmtSec(em.duration_p50_expire)} · p90:{" "}
            {fmtSec(em.duration_p90_expire)}
          </div>
        </div>
      )}

      {/* time-bucket expectancy table */}
      {buckets && buckets.length > 0 && (
        <div>
          <div style={{ fontWeight: 700, color: "#94a3b8", marginBottom: 6 }}>
            תוחלת לפי טווח זמן החזקה
          </div>
          <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "right", color: "#94a3b8" }}>
                <th style={{ padding: "3px 6px", borderBottom: "1px solid #334155" }}>טווח</th>
                <th style={{ padding: "3px 6px", borderBottom: "1px solid #334155" }}>מחזורים</th>
                <th style={{ padding: "3px 6px", borderBottom: "1px solid #334155" }}>תוחלת</th>
                <th style={{ padding: "3px 6px", borderBottom: "1px solid #334155" }}>TP win</th>
                <th style={{ padding: "3px 6px", borderBottom: "1px solid #334155" }}>avg TP</th>
                <th style={{ padding: "3px 6px", borderBottom: "1px solid #334155" }}>avg EXPIRE</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => {
                const isOptimal = optimal?.bucket === b.bucket;
                const expColor =
                  b.expectancy == null
                    ? "#64748b"
                    : b.expectancy >= 0
                      ? "var(--up)"
                      : "var(--down)";
                return (
                  <tr
                    key={b.bucket}
                    style={{
                      background: isOptimal ? "rgba(34,197,94,0.10)" : undefined,
                      color: "#e2e8f0",
                    }}
                  >
                    <td style={{ padding: "3px 6px", borderBottom: "1px solid #1e293b" }}>
                      {isOptimal ? "⭐ " : ""}{b.bucket}
                    </td>
                    <td style={{ padding: "3px 6px", borderBottom: "1px solid #1e293b" }}>
                      {b.count} ({b.tp_count}✓ / {b.expire_count}✗)
                    </td>
                    <td style={{ padding: "3px 6px", borderBottom: "1px solid #1e293b", color: expColor, fontWeight: isOptimal ? 800 : 400 }}>
                      {b.expectancy != null
                        ? `${b.expectancy >= 0 ? "+" : ""}${b.expectancy.toFixed(2)}$`
                        : "—"}
                    </td>
                    <td style={{ padding: "3px 6px", borderBottom: "1px solid #1e293b" }}>
                      {b.win_rate != null ? `${b.win_rate.toFixed(1)}%` : "—"}
                    </td>
                    <td style={{ padding: "3px 6px", borderBottom: "1px solid #1e293b", color: "#4ade80" }}>
                      {b.avg_win != null ? `+${b.avg_win.toFixed(2)}$` : "—"}
                    </td>
                    <td style={{ padding: "3px 6px", borderBottom: "1px solid #1e293b", color: "#f87171" }}>
                      {b.avg_loss_abs != null ? `-${b.avg_loss_abs.toFixed(2)}$` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* game-changer callout */}
      {optimal && optimal.expectancy != null && (
        <div
          style={{
            marginTop: 12,
            padding: "8px 12px",
            borderRadius: 8,
            background: "rgba(59,130,246,0.12)",
            border: "1px solid rgba(59,130,246,0.35)",
            color: "#93c5fd",
            fontSize: 12,
            lineHeight: 1.6,
          }}
        >
          <strong>💡 תובנת זמן אופטימלית:</strong> עסקאות שנסגרו בטווח{" "}
          <strong>{optimal.bucket}</strong> השיגו תוחלת של{" "}
          <strong style={{ color: optimal.expectancy >= 0 ? "#4ade80" : "#f87171" }}>
            {optimal.expectancy >= 0 ? "+" : ""}
            {optimal.expectancy.toFixed(2)}$
          </strong>{" "}
          עם {optimal.win_rate?.toFixed(1)}% TP win ({optimal.count} מחזורים).
          {optimal.expectancy > 0
            ? " זהו הטווח בו הביצועים הטובים ביותר — שקול לכוון את הגדרות ה-TP לטווח זה."
            : " שקול לשנות פרמטרים — אף טווח זמן לא הציג תוחלת חיובית."}
        </div>
      )}
    </div>
  );
}

function ExtendedMetricsPanel({ em, minSamples }: { em: ExtendedMetrics; minSamples?: number }) {
  const pp = em.pnl_percentiles;
  return (
    <div
      style={{
        marginBottom: 14,
        padding: 12,
        borderRadius: 10,
        background: "#0c1829",
        border: "1px solid #1e3a5f",
        fontSize: 12,
        color: "#cbd5e1",
        lineHeight: 1.65,
      }}
    >
      <div style={{ fontWeight: 800, marginBottom: 8, color: "#93c5fd" }}>מדדים מורחבים (לפי מחזורים בחלון זה)</div>
      <div>
        <strong>Profit factor</strong> (סה״כ TP / |סה״כ EXPIRE|): {fmtProfitFactor(em.profit_factor)}
      </div>
      <div>
        משך ממוצע — TP:{" "}
        {em.avg_duration_tp_sec != null && Number.isFinite(em.avg_duration_tp_sec)
          ? `${Math.round(em.avg_duration_tp_sec)}s`
          : "—"}{" "}
        · EXPIRE:{" "}
        {em.avg_duration_expire_sec != null && Number.isFinite(em.avg_duration_expire_sec)
          ? `${Math.round(em.avg_duration_expire_sec)}s`
          : "—"}
      </div>
      {pp && (
        <div style={{ marginTop: 6 }}>
          <strong>התפלגות PnL למחזור</strong> — min: {pp.min.toFixed(2)}$ · p10: {fmtMaybe(pp.p10)}$ · p50:{" "}
          {fmtMaybe(pp.p50)}$ · p90: {fmtMaybe(pp.p90)}$ · max: {pp.max.toFixed(2)}$
        </div>
      )}
      <div style={{ marginTop: 6 }}>
        רווח bid–ask בכניסה (ממוצע, מ־BUY ראשון):{" "}
        {em.entry_spread_avg_cents != null && Number.isFinite(em.entry_spread_avg_cents)
          ? `${em.entry_spread_avg_cents.toFixed(2)}¢ (${em.entry_spread_sessions} מחזורים עם נתון)`
          : "—"}
      </div>
      <div style={{ marginTop: 10, overflowX: "auto" }}>
        <div style={{ fontWeight: 700, marginBottom: 6, color: "#94a3b8" }}>Up מול Down (תוחלת לפי צד)</div>
        <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "right", color: "#94a3b8" }}>
              <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>צד</th>
              <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>מחזורים</th>
              <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>תוחלת</th>
              <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>TP win %</th>
            </tr>
          </thead>
          <tbody>
            {(["Up", "Down"] as const).map((sd) => {
              const row = em.by_side[sd];
              return (
                <tr key={sd} style={{ color: "#e2e8f0" }}>
                  <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>{sd}</td>
                  <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>{row.sessions}</td>
                  <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>
                    {row.expectancy != null && Number.isFinite(row.expectancy)
                      ? `${row.expectancy >= 0 ? "+" : ""}${row.expectancy.toFixed(2)}$`
                      : "—"}
                  </td>
                  <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>
                    {row.tp_win_pct != null && Number.isFinite(row.tp_win_pct) ? `${row.tp_win_pct.toFixed(1)}%` : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {em.low_sample_warning && em.low_sample_message && (
        <div
          style={{
            marginTop: 10,
            padding: 8,
            borderRadius: 8,
            background: "rgba(251,191,36,0.12)",
            border: "1px solid rgba(251,191,36,0.35)",
            color: "#fcd34d",
            fontSize: 11,
          }}
        >
          <strong>דגימה נמוכה</strong>
          {minSamples != null && ` (סף: ${minSamples})`}: {em.low_sample_message}
        </div>
      )}
    </div>
  );
}

function sortTipsList(tips: Tip[]): Tip[] {
  return [...tips].sort((a, b) => {
    const order: Record<string, number> = { full: 0, no_contrast: 1, insufficient_data: 2 };
    const ma = a.tip_mode || "full";
    const mb = b.tip_mode || "full";
    const ra = order[ma] ?? 3;
    const rb = order[mb] ?? 3;
    if (ra !== rb) return ra - rb;
    const ea = a.metrics?.expectancy ?? -Infinity;
    const eb = b.metrics?.expectancy ?? -Infinity;
    if (a.metrics == null && b.metrics != null) return 1;
    if (a.metrics != null && b.metrics == null) return -1;
    return eb - ea;
  });
}

function BinTimeAnalyticsPanel({
  buckets,
  optimal,
}: {
  buckets: TimeBucketRow[];
  optimal: TimeBucketRow | null | undefined;
}) {
  return (
    <div
      style={{
        marginTop: 10,
        padding: "8px 10px",
        borderRadius: 8,
        background: "#071220",
        border: "1px solid #1e3a5f",
        fontSize: 11,
        color: "#cbd5e1",
        lineHeight: 1.6,
      }}
    >
      <div style={{ fontWeight: 700, color: "#7dd3fc", marginBottom: 6 }}>
        ⏱ תוחלת לפי טווח זמן (בקבוצה המומלצת)
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "right", color: "#64748b" }}>
            <th style={{ padding: "2px 5px", borderBottom: "1px solid #1e293b" }}>טווח</th>
            <th style={{ padding: "2px 5px", borderBottom: "1px solid #1e293b" }}>מחזורים</th>
            <th style={{ padding: "2px 5px", borderBottom: "1px solid #1e293b" }}>תוחלת</th>
            <th style={{ padding: "2px 5px", borderBottom: "1px solid #1e293b" }}>TP win</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => {
            const isOpt = optimal?.bucket === b.bucket;
            const expColor =
              b.expectancy == null
                ? "#64748b"
                : b.expectancy >= 0
                  ? "var(--up)"
                  : "var(--down)";
            return (
              <tr
                key={b.bucket}
                style={{ background: isOpt ? "rgba(34,197,94,0.10)" : undefined }}
              >
                <td style={{ padding: "2px 5px", borderBottom: "1px solid #0f172a" }}>
                  {isOpt ? "⭐ " : ""}
                  {b.bucket}
                </td>
                <td style={{ padding: "2px 5px", borderBottom: "1px solid #0f172a" }}>
                  {b.count}
                </td>
                <td
                  style={{
                    padding: "2px 5px",
                    borderBottom: "1px solid #0f172a",
                    color: expColor,
                    fontWeight: isOpt ? 800 : 400,
                  }}
                >
                  {b.expectancy != null
                    ? `${b.expectancy >= 0 ? "+" : ""}${b.expectancy.toFixed(2)}$`
                    : "—"}
                </td>
                <td style={{ padding: "2px 5px", borderBottom: "1px solid #0f172a" }}>
                  {b.win_rate != null ? `${b.win_rate.toFixed(1)}%` : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {optimal && optimal.expectancy != null && (
        <div
          style={{
            marginTop: 8,
            padding: "5px 8px",
            borderRadius: 6,
            background: "rgba(59,130,246,0.12)",
            border: "1px solid rgba(59,130,246,0.35)",
            color: "#93c5fd",
          }}
        >
          <strong>💡 יציאה אופטימלית בקבוצה זו:</strong> טווח{" "}
          <strong>{optimal.bucket}</strong> — תוחלת{" "}
          <strong style={{ color: optimal.expectancy >= 0 ? "#4ade80" : "#f87171" }}>
            {optimal.expectancy >= 0 ? "+" : ""}
            {optimal.expectancy.toFixed(2)}$
          </strong>{" "}
          ({optimal.win_rate?.toFixed(1)}% TP win, {optimal.count} מחזורים)
        </div>
      )}
    </div>
  );
}

function TipCards({ tips }: { tips: Tip[] }) {
  const sortedTips = useMemo(() => sortTipsList(tips), [tips]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {sortedTips.map((t) => {
        const mode = t.tip_mode || "full";
        const exp = t.metrics?.expectancy;
        const expColor = exp == null ? "var(--muted)" : exp >= 0 ? "var(--up)" : "var(--down)";
        return (
          <div key={`${t.key}-${String(t.current_value)}`} style={{ border: "1px solid #334155", borderRadius: 10, padding: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
              <div style={{ fontWeight: 800 }}>{t.label}</div>
              <div style={{ color: expColor, fontWeight: 800 }}>
                {mode === "full" && t.metrics && exp != null && Number.isFinite(exp)
                  ? `Expectancy ${exp >= 0 ? "+" : ""}${exp.toFixed(2)}$`
                  : mode === "no_contrast"
                    ? "אין השוואה בין קבוצות"
                    : "—"}
              </div>
            </div>

            <div style={{ marginTop: 6, color: "var(--muted)", fontSize: 13 }}>
              <span>המצב עכשיו: </span>
              <span style={{ color: "#fff", fontWeight: 700 }}>{fmtMaybe(t.current_value)}</span>
              <span>{" · "}</span>
              <span>המלצה: </span>
              <span style={{ color: "#fff", fontWeight: 700 }}>{t.action}</span>
            </div>

            {t.metrics && mode === "full" && (
              <div style={{ marginTop: 8, fontSize: 12, color: "var(--muted)", lineHeight: 1.6 }}>
                <div>TP win: {t.metrics.win_rate.toFixed(1)}%</div>
                <div>
                  Avg TP: {t.metrics.avg_win >= 0 ? "+" : ""}
                  {t.metrics.avg_win.toFixed(2)}$
                </div>
                <div>Avg EXPIRE (abs): -{t.metrics.avg_loss_abs.toFixed(2)}$</div>
                <div>RR: {t.metrics.rr.toFixed(2)} · EXPIRE rate: {fmtPct01(t.metrics.expire_rate)}</div>
                <div>מחזורים בקבוצה: {t.metrics.total_count}</div>
                <div>
                  שיא החזקה (TP): {fmtMaybeSignedPct(t.metrics.avg_peak_roi_tp ?? null)}
                  {" · "}שפל החזקה (TP): {fmtMaybeSignedPct(t.metrics.avg_trough_roi_tp ?? null)}
                </div>
                <div>שפל ב-EXPIRE: {fmtMaybeSignedPct(t.metrics.avg_trough_roi_expire ?? null)}</div>
                <div>
                  אחרי TP: שיא bid {fmtMaybeSignedCents(t.metrics.avg_after_tp_peak_delta_cents ?? null)}{" "}
                  ({fmtMaybeSignedPct(t.metrics.avg_after_tp_peak_delta_pct ?? null)}) · שפל bid{" "}
                  {fmtMaybeSignedCents(t.metrics.avg_after_tp_trough_delta_cents ?? null)}{" "}
                  ({fmtMaybeSignedPct(t.metrics.avg_after_tp_trough_delta_pct ?? null)})
                </div>
                <div>
                  כניסות ממוצע: {fmtMaybe(t.metrics.avg_entries_per_session)} · משך ממוצע:{" "}
                  {t.metrics.avg_duration_sec != null && Number.isFinite(t.metrics.avg_duration_sec)
                    ? `${Math.round(t.metrics.avg_duration_sec)}s`
                    : "—"}
                </div>
              </div>
            )}

            {t.metrics && mode === "full" && t.metrics.time_buckets && t.metrics.time_buckets.length > 0 && (
              <BinTimeAnalyticsPanel
                buckets={t.metrics.time_buckets}
                optimal={t.metrics.optimal_exit_bucket}
              />
            )}

            {t.bin_comparison && t.bin_comparison.length >= 2 && mode === "full" && (
              <div style={{ marginTop: 10, overflowX: "auto" }}>
                <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 6, color: "#94a3b8" }}>
                  השוואת קבוצות (לפי תוחלת)
                </div>
                <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ textAlign: "right", color: "#94a3b8" }}>
                      <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>ערך</th>
                      <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>תוחלת</th>
                      <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>מחזורים</th>
                      <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>TP win</th>
                      <th style={{ padding: 4, borderBottom: "1px solid #334155" }}>מומלץ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.bin_comparison.map((row, i) => (
                      <tr
                        key={i}
                        style={{
                          background: row.recommended ? "rgba(34,197,94,0.12)" : undefined,
                          color: "#e2e8f0",
                        }}
                      >
                        <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>{fmtBinValue(row.bin_value)}</td>
                        <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>
                          {row.expectancy >= 0 ? "+" : ""}
                          {row.expectancy.toFixed(2)}$
                        </td>
                        <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>{row.total_count}</td>
                        <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>{row.win_rate.toFixed(1)}%</td>
                        <td style={{ padding: 4, borderBottom: "1px solid #1e293b" }}>{row.recommended ? "כן" : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div style={{ marginTop: 8, fontSize: 12, color: "#e2e8f0" }}>{t.reasoning}</div>
          </div>
        );
      })}
      {sortedTips.length === 0 && <div style={{ color: "var(--muted)" }}>אין טיפים להצגה בחלק זה.</div>}
    </div>
  );
}

type BtcWindowKey = "5m" | "15m";

function WindowTabBar({
  active,
  onChange,
  has5,
  has15,
}: {
  active: BtcWindowKey;
  onChange: (k: BtcWindowKey) => void;
  has5: boolean;
  has15: boolean;
}) {
  const btn = (key: BtcWindowKey, label: string, enabled: boolean) => {
    const isOn = active === key;
    return (
      <button
        type="button"
        disabled={!enabled}
        onClick={() => enabled && onChange(key)}
        style={{
          flex: 1,
          maxWidth: 200,
          padding: "10px 16px",
          borderRadius: 10,
          border: isOn ? "2px solid #3b82f6" : "1px solid #334155",
          background: isOn ? "rgba(59,130,246,0.2)" : "#0f172a",
          color: enabled ? (isOn ? "#e0f2fe" : "#94a3b8") : "#475569",
          fontWeight: isOn ? 800 : 600,
          fontSize: 14,
          cursor: enabled ? "pointer" : "not-allowed",
          transition: "background 0.15s, border-color 0.15s",
        }}
      >
        {label}
      </button>
    );
  };
  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        marginBottom: 16,
        flexWrap: "wrap",
        alignItems: "center",
      }}
    >
      {btn("5m", "שוק 5 דק׳ (Up/Down)", has5)}
      {btn("15m", "שוק 15 דק׳ (Up/Down)", has15)}
      <span style={{ fontSize: 11, color: "var(--muted)", width: "100%", marginTop: 2 }}>
        בחר חלון זמן — מוצגים טיפים ומדדים רק לשוק הנבחר (ללא גלילה בין 5 ל־15).
      </span>
    </div>
  );
}

export default function TipsV2() {
  const [data, setData] = useState<TipsV2Response | null>(null);
  const [err, setErr] = useState<string>("");
  const [loading, setLoading] = useState(false);
  /** לשונית פעילה כשיש גם 5m וגם 15m */
  const [windowTab, setWindowTab] = useState<BtcWindowKey>("5m");

  const refreshTips = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const res = await api<TipsV2Response>("/api/strategy/tips-v2");
      setData(res);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "שגיאה לא ידועה");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshTips();
  }, [refreshTips]);

  const gm = data?.global_metrics;
  const byWin = data?.by_btc_window;
  const has5 = Boolean(byWin?.["5m"]);
  const has15 = Boolean(byWin?.["15m"]);
  /** כשיש רק חלון אחד — תמיד מציגים אותו; כשיש שניים — לפי בחירת המשתמש */
  const effectiveWindow = useMemo((): BtcWindowKey => {
    if (has5 && has15) return windowTab;
    if (has5 && !has15) return "5m";
    if (!has5 && has15) return "15m";
    return windowTab;
  }, [has5, has15, windowTab]);
  const winCmp = data?.window_comparison;

  return (
    <div style={{ background: "var(--card)", padding: 20, borderRadius: 12 }}>
      <h2 style={{ marginTop: 0 }}>ניתוח מתקדם v3 (מקסימום תוחלת)</h2>
      <TipsV2RunsPanel onRunsChanged={refreshTips} />
      {loading && <div style={{ color: "var(--muted)" }}>טוען נתונים…</div>}
      {err && (
        <div style={{ color: "#f87171", marginBottom: 12, padding: 8, background: "#3f1f1f" }}>{err}</div>
      )}
      {data && (
        <>
          <div style={{ color: "var(--muted)", fontSize: 13, marginBottom: 12 }}>{data.summary}</div>

          {data.data_quality && (
            <div
              style={{
                marginBottom: 14,
                padding: 10,
                borderRadius: 8,
                background: "var(--bg-soft, #1e293b)",
                border: "1px solid #334155",
                fontSize: 12,
                color: "var(--muted)",
                lineHeight: 1.5,
              }}
            >
              <strong style={{ color: "#e2e8f0" }}>איכות נתונים להמלצות: </strong>
              ריצות שנסרקו: {data.data_quality.runs_used} · מחזורים (סה״כ): {data.data_quality.sessions_total} · פרמטרים
              עם השוואה בין קבוצות: {data.data_quality.params_with_contrast} · בלי גיוון (טקסט קצר):{" "}
              {data.data_quality.params_without_contrast} · חסר מידע: {data.data_quality.params_insufficient}
            </div>
          )}

          {data.global_narrative && (
            <div style={{ marginBottom: 14, padding: 12, borderRadius: 10, background: "#0f172a", border: "1px solid #334155" }}>
              <div style={{ fontWeight: 800, marginBottom: 8, color: "#e2e8f0" }}>תובנות טיפים v2 — תמונת מצב כללית</div>
              <div style={{ fontSize: 13, color: "#cbd5e1", lineHeight: 1.55 }}>{data.global_narrative}</div>
              {gm && (
                <div style={{ marginTop: 10, fontSize: 12, color: "var(--muted)", lineHeight: 1.65 }}>
                  <div>
                    תוחלת: {gm.expectancy >= 0 ? "+" : ""}
                    {gm.expectancy.toFixed(2)}$ · TP win: {gm.win_rate.toFixed(1)}% · RR: {gm.rr.toFixed(2)} · EXPIRE rate:{" "}
                    {fmtPct01(gm.expire_rate)}
                  </div>
                  <div>
                    שיא החזקה (TP): {fmtMaybeSignedPct(gm.avg_peak_roi_tp ?? null)}
                    {" · "}
                    שפל החזקה (TP): {fmtMaybeSignedPct(gm.avg_trough_roi_tp ?? null)}
                  </div>
                  <div>שפל ב-EXPIRE: {fmtMaybeSignedPct(gm.avg_trough_roi_expire ?? null)}</div>
                  <div>
                    אחרי TP: שיא bid {fmtMaybeSignedCents(gm.avg_after_tp_peak_delta_cents ?? null)} (
                    {fmtMaybeSignedPct(gm.avg_after_tp_peak_delta_pct ?? null)}) · שפל bid{" "}
                    {fmtMaybeSignedCents(gm.avg_after_tp_trough_delta_cents ?? null)} (
                    {fmtMaybeSignedPct(gm.avg_after_tp_trough_delta_pct ?? null)})
                  </div>
                  <div>
                    כניסות ממוצע: {fmtMaybe(gm.avg_entries_per_session)} · משך ממוצע:{" "}
                    {gm.avg_duration_sec != null && Number.isFinite(gm.avg_duration_sec)
                      ? `${Math.round(gm.avg_duration_sec)}s`
                      : "—"}
                  </div>
                </div>
              )}
            </div>
          )}

          {winCmp && winCmp["5m"] && winCmp["15m"] && (
            <div
              style={{
                marginBottom: 16,
                padding: 12,
                borderRadius: 10,
                background: "#0f172a",
                border: "1px solid #334155",
              }}
            >
              <div style={{ fontWeight: 800, marginBottom: 10, color: "#e2e8f0" }}>השוואת שווקים (5m / 15m)</div>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ textAlign: "right", color: "#94a3b8" }}>
                      <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>שוק</th>
                      <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>מחזורים</th>
                      <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>תוחלת</th>
                      <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>TP win</th>
                      <th style={{ padding: 6, borderBottom: "1px solid #334155" }}>Profit factor</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(["5m", "15m"] as const).map((k) => {
                      const row = winCmp[k];
                      return (
                        <tr key={k} style={{ color: "#e2e8f0" }}>
                          <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>{k === "5m" ? "5 דק׳" : "15 דק׳"}</td>
                          <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>{fmtMaybe(row.sessions_total)}</td>
                          <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>
                            {row.expectancy != null && Number.isFinite(row.expectancy)
                              ? `${row.expectancy >= 0 ? "+" : ""}${row.expectancy.toFixed(2)}$`
                              : "—"}
                          </td>
                          <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>
                            {row.tp_win_pct != null && Number.isFinite(row.tp_win_pct) ? `${row.tp_win_pct.toFixed(1)}%` : "—"}
                          </td>
                          <td style={{ padding: 6, borderBottom: "1px solid #1e293b" }}>{fmtProfitFactor(row.profit_factor)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 16 }}>
            {data.guardrails?.min_samples != null && <span>סף מידע: {data.guardrails.min_samples} מחזורים</span>}
            {data.guardrails?.use_guardrails != null && (
              <span> · Guardrails: {data.guardrails.use_guardrails ? "פעיל" : "כבוי"}</span>
            )}
            {data.note && <div style={{ marginTop: 6 }}>{data.note}</div>}
          </div>

          {(() => {
            const bundle = byWin && (has5 || has15) ? byWin[effectiveWindow] : undefined;
            if (!bundle) {
              return <TipCards tips={data?.tips || []} />;
            }
            const wk = effectiveWindow;
            const wgm = bundle.global_metrics;
            return (
              <>
                {has5 && has15 && (
                  <WindowTabBar active={windowTab} onChange={setWindowTab} has5={has5} has15={has15} />
                )}
                <div
                  key={wk}
                  style={{
                    marginBottom: 20,
                    padding: 14,
                    borderRadius: 12,
                    border: "1px solid #334155",
                    background: "var(--bg-soft, #0f172a)",
                  }}
                >
                  <h3 style={{ marginTop: 0, marginBottom: 8, color: "#e2e8f0" }}>{bundle.title}</h3>
                  <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 10 }}>{bundle.summary}</div>
                  {bundle.data_quality && (
                    <div
                      style={{
                        marginBottom: 10,
                        padding: 8,
                        borderRadius: 8,
                        background: "#1e293b",
                        fontSize: 11,
                        color: "var(--muted)",
                        lineHeight: 1.5,
                      }}
                    >
                      <strong style={{ color: "#94a3b8" }}>איכות נתונים ({wk}): </strong>
                      ריצות: {bundle.data_quality.runs_used} · מחזורים: {bundle.data_quality.sessions_total} · עם השוואה:{" "}
                      {bundle.data_quality.params_with_contrast} · בלי גיוון: {bundle.data_quality.params_without_contrast} ·
                      חסר: {bundle.data_quality.params_insufficient}
                    </div>
                  )}
                  {bundle.global_narrative && (
                    <div
                      style={{
                        marginBottom: 12,
                        padding: 10,
                        borderRadius: 8,
                        background: "#0f172a",
                        fontSize: 12,
                        color: "#cbd5e1",
                        lineHeight: 1.55,
                      }}
                    >
                      {bundle.global_narrative}
                      {wgm && (
                        <div style={{ marginTop: 8, fontSize: 11, color: "var(--muted)" }}>
                          תוחלת: {wgm.expectancy >= 0 ? "+" : ""}
                          {wgm.expectancy.toFixed(2)}$ · TP win: {wgm.win_rate.toFixed(1)}% · RR: {wgm.rr.toFixed(2)} · EXPIRE
                          rate: {fmtPct01(wgm.expire_rate)}
                        </div>
                      )}
                    </div>
                  )}
                  {bundle.extended_metrics && (
                    <>
                      <TimeAnalyticsPanel em={bundle.extended_metrics} />
                      <ExtendedMetricsPanel em={bundle.extended_metrics} minSamples={data.guardrails?.min_samples} />
                    </>
                  )}
                  <TipCards tips={bundle.tips || []} />
                </div>
              </>
            );
          })()}
        </>
      )}
    </div>
  );
}
