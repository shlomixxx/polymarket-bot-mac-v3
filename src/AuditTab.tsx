import { useCallback, useEffect, useState } from "react";
import { api, isPageHidden } from "./api";

/** לשונית "ביקורת עסקאות" — שורה לכל סשן עסקה שנסגר, עם drill-down מלא ללמידה. */

const AUDIT_TIMEOUT_MS = 45_000;

type AuditRow = {
  session_id: string;
  mode: string; slug: string; window_sec: number; side: "Up" | "Down" | string;
  decision_ts: number; settled_ts: number | null;
  recommendation: string | null; weighted_score: number | null; confidence_pct: number | null;
  vol_bucket: string | null; loss_recovery_multiplier: number | null;
  exit_type: string | null; settlement_status: string;
  realized_pnl: number | null; realized_pct: number | null;
  peak_unrealized_pct: number | null; trough_unrealized_pct: number | null;
  settlement_btc_start: number | null; settlement_btc_end: number | null; resolved_outcome: string | null;
  exit_efficiency: number | null; missed_profit_pct: number | null;
  signal_was_correct: boolean | null; signals_agreement: number | null; signal_conflict: boolean | null;
  cf_other_side_pnl: number | null; lesson_tag: string | null;
  contracts: number | null; avg_fill_price: number | null;
  context: Record<string, unknown>;
  rule_flags: Record<string, unknown>;
  cf_exit_variants: Record<string, unknown>;
  pnl_path: Array<{ ts?: number; upnl_pct?: number; bid?: number }>;
};
type AuditCounts = {
  by_status: Record<string, number>; total: number; win_rate_pct: number;
  avg_exit_efficiency: number | null; top_lessons: Array<{ lesson_tag: string; n: number }>;
};
type AuditResponse = { rows: AuditRow[]; counts: AuditCounts };

// ── status meta ──────────────────────────────────────────────────────────────
function statusColor(s: string): string {
  if (s === "WIN") return "#065f46";
  if (s === "LOSS") return "#7f1d1d";
  return "#334155";
}
function statusChipColor(s: string): { color: string; bg: string } {
  if (s === "WIN") return { color: "#6ee7b7", bg: "#065f46" };
  if (s === "LOSS") return { color: "#fecaca", bg: "#7f1d1d" };
  return { color: "#cbd5e1", bg: "#334155" };
}

// ── formatters ─────────────────────────────────────────────────────────────
// IMPORTANT: audit timestamps are in milliseconds — do NOT multiply by 1000.
function fmtTime(ts: number | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("he-IL", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return String(ts);
  }
}

function fmtUsd(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}

function fmtPct(v: number | null, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${v >= 0 ? "" : ""}${v.toFixed(digits)}%`;
}

function fmtNum(v: number | null, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

function pnlColor(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "var(--muted,#94a3b8)";
  return v >= 0 ? "#6ee7b7" : "#fca5a5";
}

function sideColor(side: string): string {
  if (side === "Up") return "#6ee7b7";
  if (side === "Down") return "#fca5a5";
  return "#e2e8f0";
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// tiny inline sparkline of upnl_pct over the pnl_path
function Sparkline({ path }: { path: Array<{ upnl_pct?: number }> }) {
  const ys = path.map((p) => (typeof p.upnl_pct === "number" ? p.upnl_pct : 0));
  if (ys.length < 2) return null;
  const W = 120, H = 32, PAD = 2;
  const min = Math.min(...ys, 0);
  const max = Math.max(...ys, 0);
  const span = max - min || 1;
  const stepX = (W - PAD * 2) / (ys.length - 1);
  const points = ys
    .map((y, i) => {
      const px = PAD + i * stepX;
      const py = PAD + (H - PAD * 2) * (1 - (y - min) / span);
      return `${px.toFixed(1)},${py.toFixed(1)}`;
    })
    .join(" ");
  // zero baseline
  const zeroY = PAD + (H - PAD * 2) * (1 - (0 - min) / span);
  const last = ys[ys.length - 1];
  return (
    <svg width={W} height={H} style={{ display: "block" }}>
      <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY} stroke="#334155" strokeWidth={1} strokeDasharray="2 2" />
      <polyline
        points={points}
        fill="none"
        stroke={last >= 0 ? "#6ee7b7" : "#fca5a5"}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

// labeled drill-down section for a nested context sub-object
function Section({ title, obj }: { title: string; obj: unknown }) {
  if (!isPlainObject(obj) || Object.keys(obj).length === 0) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 12, fontWeight: 700, color: "#93c5fd", marginBottom: 4 }}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", fontSize: 12, color: "#cbd5e1" }}>
        {Object.entries(obj).map(([k, v]) => (
          <span key={k}>
            {k}: <b style={{ color: "#e2e8f0" }}>{isPlainObject(v) || Array.isArray(v) ? JSON.stringify(v) : String(v)}</b>
          </span>
        ))}
      </div>
    </div>
  );
}

export default function AuditTab() {
  const [data, setData] = useState<AuditResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const [fMode, setFMode] = useState<"all" | "demo" | "live">("all");
  const [fWindow, setFWindow] = useState<"all" | "300" | "900">("all");
  const [fStatus, setFStatus] = useState<"all" | "WIN" | "LOSS" | "VOID" | "UNKNOWN" | "PENDING">("all");

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const qs = new URLSearchParams();
      if (fMode !== "all") qs.set("mode", fMode);
      if (fWindow !== "all") qs.set("window_sec", fWindow);
      if (fStatus !== "all") qs.set("settlement_status", fStatus);
      const res = await api<AuditResponse>(`/api/audit?${qs.toString()}`, { timeoutMs: AUDIT_TIMEOUT_MS });
      setData(res);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [fMode, fWindow, fStatus]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // auto-refresh every 12s — מדלג כשהטאב מוסתר, ומרענן מיד בחזרה.
  useEffect(() => {
    const id = setInterval(() => { if (!isPageHidden()) void refresh(); }, 12000);
    const onVisible = () => { if (!isPageHidden()) void refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  const rows = data?.rows ?? [];
  const counts = data?.counts;

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  // status cards: WIN/LOSS first (colored), then everything else (grey)
  const statusOrder = ["WIN", "LOSS", "VOID", "UNKNOWN", "PENDING"];
  const byStatus = counts?.by_status ?? {};
  const statusKeys = [
    ...statusOrder.filter((k) => k in byStatus),
    ...Object.keys(byStatus).filter((k) => !statusOrder.includes(k)),
  ];

  return (
    <div dir="rtl" style={{ padding: "4px 2px 40px", maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 24, fontWeight: 800, letterSpacing: "-0.02em" }}>📋 ביקורת עסקאות</h2>
          <p style={{ margin: "4px 0 0", color: "var(--muted, #94a3b8)", fontSize: 13 }}>
            כל עסקה מתועדת כאן — למה נכנסנו, מה קרה, ומה היה אפשר טוב יותר. הבסיס שה-AI ילמד ממנו.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" onClick={() => void refresh()} style={btnStyle()}>
            {loading ? "מרענן…" : "↻ רענן"}
          </button>
        </div>
      </div>

      {/* Summary strip */}
      <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap" }}>
        <div style={{
          flex: "1 1 120px", minWidth: 110, padding: "12px 14px", borderRadius: 12,
          background: "var(--card, #0f172a)", border: "1px solid #065f46", borderInlineStart: "4px solid #065f46",
        }}>
          <div style={{ fontSize: 12, color: "#6ee7b7", fontWeight: 700 }}>אחוז ניצחון</div>
          <div style={{ fontSize: 26, fontWeight: 800, lineHeight: 1.1, marginTop: 2 }}>
            {counts ? `${counts.win_rate_pct.toFixed(1)}%` : "—"}
          </div>
        </div>
        <div style={{
          flex: "1 1 120px", minWidth: 110, padding: "12px 14px", borderRadius: 12,
          background: "var(--card, #0f172a)", border: "1px solid #1e3a5f", borderInlineStart: "4px solid #1d4ed8",
        }}>
          <div style={{ fontSize: 12, color: "#93c5fd", fontWeight: 700 }}>יעילות יציאה ממוצעת</div>
          <div style={{ fontSize: 26, fontWeight: 800, lineHeight: 1.1, marginTop: 2 }}>
            {counts && counts.avg_exit_efficiency != null ? `${(counts.avg_exit_efficiency * 100).toFixed(0)}%` : "—"}
          </div>
        </div>
        <div style={{
          flex: "1 1 120px", minWidth: 110, padding: "12px 14px", borderRadius: 12,
          background: "var(--card, #0f172a)", border: "1px solid #1e293b",
        }}>
          <div style={{ fontSize: 12, color: "var(--muted, #94a3b8)", fontWeight: 700 }}>סה"כ עסקאות</div>
          <div style={{ fontSize: 26, fontWeight: 800, lineHeight: 1.1, marginTop: 2 }}>{counts?.total ?? 0}</div>
        </div>
        {statusKeys.map((s) => {
          const m = statusChipColor(s);
          return (
            <div key={s} style={{
              flex: "1 1 100px", minWidth: 90, padding: "12px 14px", borderRadius: 12,
              background: "var(--card, #0f172a)", border: `1px solid ${m.bg}`, borderInlineStart: `4px solid ${m.bg}`,
            }}>
              <div style={{ fontSize: 12, color: m.color, fontWeight: 700 }}>{s}</div>
              <div style={{ fontSize: 26, fontWeight: 800, lineHeight: 1.1, marginTop: 2 }}>{byStatus[s] ?? 0}</div>
            </div>
          );
        })}
      </div>

      {/* Top lessons chips */}
      {counts && counts.top_lessons.length > 0 && (
        <div style={{ display: "flex", gap: 6, marginTop: 12, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", fontWeight: 700 }}>לקחים נפוצים:</span>
          {counts.top_lessons.map((l) => (
            <span key={l.lesson_tag} style={{
              fontSize: 12, fontWeight: 700, padding: "3px 10px", borderRadius: 999,
              color: "#fde68a", background: "#713f1255", border: "1px solid #713f12",
            }}>
              {l.lesson_tag} ×{l.n}
            </span>
          ))}
        </div>
      )}

      {/* Filters toolbar */}
      <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap", alignItems: "center" }}>
        <select value={fMode} onChange={(e) => setFMode(e.target.value as "all" | "demo" | "live")} style={selectStyle()}>
          <option value="all">כל המצבים</option>
          <option value="demo">דמו</option>
          <option value="live">לייב</option>
        </select>
        <select value={fWindow} onChange={(e) => setFWindow(e.target.value as "all" | "300" | "900")} style={selectStyle()}>
          <option value="all">כל החלונות</option>
          <option value="300">5 דקות</option>
          <option value="900">15 דקות</option>
        </select>
        <select value={fStatus} onChange={(e) => setFStatus(e.target.value as typeof fStatus)} style={selectStyle()}>
          <option value="all">כל הסטטוסים</option>
          <option value="WIN">WIN</option>
          <option value="LOSS">LOSS</option>
          <option value="VOID">VOID</option>
          <option value="UNKNOWN">UNKNOWN</option>
          <option value="PENDING">PENDING</option>
        </select>
      </div>

      {err && <div style={{ marginTop: 14, color: "#fecaca", background: "#7f1d1d33", padding: 12, borderRadius: 10 }}>שגיאה בטעינה: {err}</div>}

      {/* Row list */}
      <div style={{ marginTop: 16, display: "grid", gap: 8 }}>
        {!loading && rows.length === 0 && (
          <div style={{ padding: 40, textAlign: "center", color: "var(--muted,#94a3b8)", border: "1px dashed #1e293b", borderRadius: 12 }}>
            אין עסקאות להצגה
          </div>
        )}
        {rows.map((r) => {
          const isOpen = expanded.has(r.session_id);
          const sc = statusChipColor(r.settlement_status);
          const sigMark = r.signal_was_correct === true ? "✓" : r.signal_was_correct === false ? "✗" : "—";
          const sigColor = r.signal_was_correct === true ? "#6ee7b7" : r.signal_was_correct === false ? "#fca5a5" : "var(--muted,#94a3b8)";
          const ctx = r.context ?? {};
          const provenance = isPlainObject(ctx.provenance) ? ctx.provenance : {};
          const signalsMissing = provenance.signals_missing === true;
          // settlement fields are top-level audit columns (written at finalize), not in the decision context
          const resolvedOutcome = r.resolved_outcome ?? null;
          const btcStart = r.settlement_btc_start ?? null;
          const btcEnd = r.settlement_btc_end ?? null;
          return (
            <div key={r.session_id} style={{
              borderRadius: 12, background: "var(--card,#0f172a)", border: "1px solid #1e293b",
              borderInlineStart: `4px solid ${statusColor(r.settlement_status)}`, overflow: "hidden",
            }}>
              {/* Collapsed row */}
              <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", cursor: "pointer", overflowX: "auto" }}
                   onClick={() => toggleExpand(r.session_id)}>
                <span style={{
                  fontSize: 11, fontWeight: 800, padding: "3px 8px", borderRadius: 999,
                  color: sc.color, background: sc.bg, whiteSpace: "nowrap",
                }}>{r.settlement_status}</span>
                <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", whiteSpace: "nowrap" }}>{fmtTime(r.decision_ts)}</span>
                <span style={{ fontSize: 12, color: "#e2e8f0", whiteSpace: "nowrap" }}>{r.window_sec === 900 ? "15m" : "5m"}</span>
                <span style={{ fontSize: 12, fontWeight: 800, color: sideColor(r.side), whiteSpace: "nowrap" }}>{r.side}</span>
                <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", whiteSpace: "nowrap" }}>@{fmtNum(r.avg_fill_price)}</span>
                <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", whiteSpace: "nowrap" }}>×{r.contracts ?? "—"}</span>
                {r.exit_type && <span style={{ fontSize: 12, color: "#cbd5e1", whiteSpace: "nowrap" }}>{r.exit_type}</span>}
                <span style={{ fontSize: 13, fontWeight: 800, color: pnlColor(r.realized_pnl), whiteSpace: "nowrap" }}>{fmtUsd(r.realized_pnl)}</span>
                <span style={{ fontSize: 12, fontWeight: 700, color: pnlColor(r.realized_pct), whiteSpace: "nowrap" }}>ROI {fmtPct(r.realized_pct)}</span>
                <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", whiteSpace: "nowrap" }}>שיא {fmtPct(r.peak_unrealized_pct)}</span>
                <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", whiteSpace: "nowrap" }}>
                  יעילות {r.exit_efficiency != null ? `${r.exit_efficiency.toFixed(2)}×` : "—"}
                </span>
                <span style={{ fontSize: 13, fontWeight: 800, color: sigColor, whiteSpace: "nowrap" }} title="סיגנל צדק?">{sigMark}</span>
                {r.lesson_tag && (
                  <span style={{
                    fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999,
                    color: "#fde68a", background: "#713f1255", whiteSpace: "nowrap",
                  }}>{r.lesson_tag}</span>
                )}
                <span style={{ flex: 1 }} />
                <span style={{ color: "var(--muted,#94a3b8)", transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }}>‹</span>
              </div>

              {/* Drill-down */}
              {isOpen && (
                <div style={{ padding: "0 14px 14px", borderTop: "1px solid #1e293b", display: "grid", gap: 6, fontSize: 13 }}>
                  {/* Top meta line */}
                  <div style={{ display: "flex", gap: 16, flexWrap: "wrap", color: "var(--muted,#94a3b8)", marginTop: 10 }}>
                    <span>מצב: <b style={{ color: "#e2e8f0" }}>{r.mode}</b></span>
                    <span>שוק: <b style={{ color: "#e2e8f0" }}>{r.slug}</b></span>
                    <span>החלטה: {fmtTime(r.decision_ts)}</span>
                    <span>סגירה: {fmtTime(r.settled_ts)}</span>
                    <span>המלצה: <b style={{ color: "#e2e8f0" }}>{r.recommendation ?? "—"}</b></span>
                    <span>ציון משוקלל: <b style={{ color: "#e2e8f0" }}>{fmtNum(r.weighted_score)}</b></span>
                    <span>ביטחון: <b style={{ color: "#e2e8f0" }}>{r.confidence_pct != null ? `${r.confidence_pct.toFixed(0)}%` : "—"}</b></span>
                    <span>תנודתיות: <b style={{ color: "#e2e8f0" }}>{r.vol_bucket ?? "—"}</b></span>
                    <span>מכפיל התאוששות: <b style={{ color: "#e2e8f0" }}>{fmtNum(r.loss_recovery_multiplier)}</b></span>
                  </div>

                  {signalsMissing && (
                    <div style={{ fontSize: 12, color: "var(--muted,#94a3b8)", fontStyle: "italic" }}>
                      אין נתוני סיגנל (עסקה היסטורית/לפני חיווט הסיגנלים)
                    </div>
                  )}

                  {/* PnL / efficiency line */}
                  <div style={{ display: "flex", gap: 16, flexWrap: "wrap", color: "var(--muted,#94a3b8)" }}>
                    <span>שיא לא ממומש: <b style={{ color: pnlColor(r.peak_unrealized_pct) }}>{fmtPct(r.peak_unrealized_pct)}</b></span>
                    <span>שפל לא ממומש: <b style={{ color: pnlColor(r.trough_unrealized_pct) }}>{fmtPct(r.trough_unrealized_pct)}</b></span>
                    <span>רווח שהוחמץ: <b style={{ color: "#e2e8f0" }}>{fmtPct(r.missed_profit_pct)}</b></span>
                    <span>הסכמת סיגנלים: <b style={{ color: "#e2e8f0" }}>{r.signals_agreement != null ? `${(r.signals_agreement * 100).toFixed(0)}%` : "—"}</b></span>
                    <span>ניגוד סיגנלים: <b style={{ color: "#e2e8f0" }}>{r.signal_conflict === true ? "כן" : r.signal_conflict === false ? "לא" : "—"}</b></span>
                  </div>

                  {/* Sparkline */}
                  {r.pnl_path && r.pnl_path.length > 1 && (
                    <div style={{ marginTop: 4 }}>
                      <div style={{ fontSize: 12, fontWeight: 700, color: "#93c5fd", marginBottom: 2 }}>מסלול רווח/הפסד (% לא ממומש)</div>
                      <Sparkline path={r.pnl_path} />
                    </div>
                  )}

                  {/* Context sub-objects */}
                  <Section title="סיגנל" obj={ctx.signal} />
                  <Section title="ניתוח טכני" obj={ctx.ta} />
                  <Section title="CLOB" obj={ctx.clob} />
                  <Section title="סנטימנט" obj={ctx.sentiment} />
                  <Section title="היסטוריה" obj={ctx.history} />
                  <Section title="מדיניות" obj={ctx.policy} />
                  <Section title="משטר שוק" obj={ctx.regime} />
                  <Section title="מקור נתונים" obj={ctx.provenance} />

                  {/* Settlement */}
                  {(btcStart != null || btcEnd != null || resolvedOutcome != null) && (
                    <div style={{ marginTop: 8 }}>
                      <div style={{ fontSize: 12, fontWeight: 700, color: "#93c5fd", marginBottom: 4 }}>סיום עסקה</div>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", fontSize: 12, color: "#cbd5e1" }}>
                        <span>BTC פתיחה: <b style={{ color: "#e2e8f0" }}>{btcStart != null ? String(btcStart) : "—"}</b></span>
                        <span>BTC סגירה: <b style={{ color: "#e2e8f0" }}>{btcEnd != null ? String(btcEnd) : "—"}</b></span>
                        <span>תוצאה: <b style={{ color: "#e2e8f0" }}>{resolvedOutcome != null ? String(resolvedOutcome) : "—"}</b></span>
                      </div>
                    </div>
                  )}

                  {/* Counterfactuals */}
                  <div style={{ marginTop: 8 }}>
                    <div style={{ fontSize: 12, fontWeight: 700, color: "#93c5fd", marginBottom: 4 }}>תרחישים נגדיים</div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", fontSize: 12, color: "#cbd5e1" }}>
                      <span>רווח לו בחרנו בצד השני: <b style={{ color: pnlColor(r.cf_other_side_pnl) }}>{fmtUsd(r.cf_other_side_pnl)}</b></span>
                    </div>
                    {r.cf_exit_variants && Object.keys(r.cf_exit_variants).length > 0 && (
                      <Section title="חלופות יציאה" obj={r.cf_exit_variants} />
                    )}
                  </div>

                  {/* Rule flags */}
                  {r.rule_flags && Object.keys(r.rule_flags).length > 0 && (
                    <Section title="דגלי כללים" obj={r.rule_flags} />
                  )}

                  {/* Full context JSON dump */}
                  {ctx && Object.keys(ctx).length > 0 && (
                    <pre style={{ margin: "8px 0 0", padding: 10, background: "#020617", borderRadius: 8, fontSize: 12, overflow: "auto", color: "#94a3b8" }}>
                      {JSON.stringify(ctx, null, 2)}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── inline style helpers (match app dark theme) ──────────────────────────────
function btnStyle(bg?: string): React.CSSProperties {
  return {
    padding: "7px 12px", borderRadius: 9, border: "1px solid #1e293b",
    background: bg ?? "var(--card,#0f172a)", color: "#e2e8f0", fontSize: 13, fontWeight: 600,
    cursor: "pointer",
  };
}
function selectStyle(): React.CSSProperties {
  return { padding: "7px 10px", borderRadius: 9, border: "1px solid #1e293b", background: "var(--card,#0f172a)", color: "#e2e8f0", fontSize: 13 };
}
