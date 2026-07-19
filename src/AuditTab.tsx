import { useCallback, useEffect, useState } from "react";
import { api, isPageHidden } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { Button } from "./ui/Button";
import { Collapsible } from "./ui/Collapsible";
import { israelDateTimeMs } from "./timeFormat";

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

// ── lessons (🎓 מאמן העסקאות) ────────────────────────────────────────────────
type Lesson = {
  key: string; severity: "critical" | "high" | "medium" | "low" | string; title: string;
  stat: Record<string, string | number | null>; recommendation: string; confidence: string;
};
type LessonsResponse = { note: string; eras: Record<string, number | null>; lessons: Lesson[] };

// severity palette — same language as the Faults tab
const SEV_META: Record<string, { label: string; color: string; bg: string }> = {
  critical: { label: "קריטי", color: "#fecaca", bg: "#7f1d1d" },
  high: { label: "גבוה", color: "#fed7aa", bg: "#7c2d12" },
  medium: { label: "בינוני", color: "#fde68a", bg: "#713f12" },
  low: { label: "נמוך", color: "#cbd5e1", bg: "#334155" },
};
function sevMeta(s: string) {
  return SEV_META[s] ?? SEV_META.low;
}

// ── status meta ──────────────────────────────────────────────────────────────
function statusColor(s: string): string {
  if (s === "WIN") return "var(--up)";
  if (s === "LOSS") return "var(--down)";
  return "var(--border-strong)";
}
function statusChipColor(s: string): { color: string; bg: string } {
  if (s === "WIN") return { color: "var(--up)", bg: "var(--up-muted)" };
  if (s === "LOSS") return { color: "var(--down)", bg: "var(--down-muted)" };
  return { color: "var(--text-secondary)", bg: "var(--bg-elevated)" };
}

// ── formatters ─────────────────────────────────────────────────────────────
// IMPORTANT: audit timestamps are in milliseconds — do NOT multiply by 1000.
function fmtTime(ts: number | null): string {
  if (!ts) return "—";
  return israelDateTimeMs(ts);
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
  if (v == null || !Number.isFinite(v)) return "var(--muted)";
  return v >= 0 ? "var(--up)" : "var(--down)";
}

function sideColor(side: string): string {
  if (side === "Up") return "var(--up)";
  if (side === "Down") return "var(--down)";
  return "var(--text)";
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
      <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY} stroke="var(--border-strong)" strokeWidth={1} strokeDasharray="2 2" />
      <polyline
        points={points}
        fill="none"
        stroke={last >= 0 ? "var(--up)" : "var(--down)"}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

// 🎓 collapsible "Trade Coach" lessons — ranked, actionable, mined from the ledger
function LessonsSection({ lessons }: { lessons: LessonsResponse | null }) {
  const [open, setOpen] = useState(true);
  if (!lessons) return null; // failed/loading: don't render — never break the rest of the tab

  const e = lessons.eras ?? {};
  const num = (k: string): string => {
    const v = e[k];
    return v == null || !Number.isFinite(v) ? "—" : String(v);
  };

  return (
    <Card style={{ borderInlineStart: "4px solid var(--accent)" }}>
      {/* Header row */}
      <div
        style={{ display: "flex", alignItems: "baseline", gap: "var(--s-3)", cursor: "pointer", flexWrap: "wrap" }}
        onClick={() => setOpen((o) => !o)}
      >
        <div style={{ flex: 1, minWidth: 240 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)" }}>
            <span aria-hidden>🎓</span>
            <SectionTitle as="h3">לקחים — מאמן העסקאות</SectionTitle>
          </div>
          <div
            style={{ fontSize: "0.8125rem", color: "var(--muted)", marginTop: 2 }}
            title='סכומי $ מוטים ע"י martingale — הדירוג מתבסס על win-rate ו-median, לא על הרווח בדולרים.'
          >
            לקחים מדורגים מתוך כל העסקאות — מה לשפר.
          </div>
          {Object.keys(e).length > 0 && (
            <div style={{ fontSize: "0.8125rem", color: "var(--accent-bright)", marginTop: "var(--s-1)" }}>
              סה"כ {num("total")} עסקאות · {num("schema_v1")} עם סיגנלים · win {num("overall_winrate")}%
            </div>
          )}
        </div>
        <span style={{ color: "var(--muted)", transform: open ? "rotate(90deg)" : "none", transition: "transform .15s" }}>‹</span>
      </div>

      {open && (
        <div style={{ marginTop: "var(--s-3)", paddingTop: "var(--s-3)", borderTop: "1px solid var(--border)", display: "grid", gap: "var(--s-2)" }}>
          {lessons.lessons.length === 0 && (
            <div style={{ padding: "var(--s-5)", textAlign: "center", color: "var(--muted)", border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}>
              עוד אין מספיק נתונים ללקחים
            </div>
          )}

          {lessons.lessons.map((l) => {
            const m = sevMeta(l.severity);
            return (
              <div key={l.key} style={{
                borderRadius: "var(--radius-md)", background: "var(--bg-elevated)",
                border: "1px solid var(--border)", borderInlineStart: `4px solid ${m.bg}`, padding: "var(--s-3) var(--s-4)",
              }}>
                {/* title + severity badge */}
                <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", flexWrap: "wrap" }}>
                  <span style={{
                    fontSize: 11, fontWeight: 800, padding: "3px 8px", borderRadius: 999,
                    color: m.color, background: m.bg, whiteSpace: "nowrap",
                  }}>{m.label}</span>
                  <span style={{ fontSize: 14, fontWeight: 800, color: "var(--text)" }}>{l.title}</span>
                </div>

                {/* recommendation */}
                <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: "var(--s-2)", lineHeight: 1.5 }}>{l.recommendation}</div>

                {/* confidence chip */}
                {l.confidence && (
                  <div style={{ marginTop: "var(--s-2)" }}>
                    <span style={{
                      fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999,
                      color: "var(--accent-bright)", background: "var(--accent-muted)", border: "1px solid var(--border-strong)", whiteSpace: "nowrap",
                    }}>ביטחון: {l.confidence}</span>
                  </div>
                )}

                {/* stat pills */}
                {l.stat && Object.keys(l.stat).length > 0 && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-2)", marginTop: "var(--s-2)" }}>
                    {Object.entries(l.stat).map(([k, v]) => (
                      <span key={k} style={{
                        fontSize: 11, fontWeight: 600, padding: "3px 8px", borderRadius: "var(--radius-sm)",
                        color: "var(--text-secondary)", background: "var(--card)", border: "1px solid var(--border)", whiteSpace: "nowrap",
                      }}>
                        {k}: <b style={{ color: "var(--text)" }}>{v == null ? "—" : String(v)}</b>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}

          {/* note */}
          {lessons.note && (
            <div style={{ fontSize: "0.8125rem", color: "var(--muted)", fontStyle: "italic", marginTop: "var(--s-2)" }}>{lessons.note}</div>
          )}
        </div>
      )}
    </Card>
  );
}

// labeled drill-down section for a nested context sub-object
function Section({ title, obj }: { title: string; obj: unknown }) {
  if (!isPlainObject(obj) || Object.keys(obj).length === 0) return null;
  return (
    <div style={{ marginTop: "var(--s-2)" }}>
      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--accent-bright)", marginBottom: "var(--s-1)" }}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-1) var(--s-4)", fontSize: 12, color: "var(--text-secondary)" }}>
        {Object.entries(obj).map(([k, v]) => (
          <span key={k}>
            {k}: <b style={{ color: "var(--text)" }}>{isPlainObject(v) || Array.isArray(v) ? JSON.stringify(v) : String(v)}</b>
          </span>
        ))}
      </div>
    </div>
  );
}

export default function AuditTab() {
  const [data, setData] = useState<AuditResponse | null>(null);
  const [lessons, setLessons] = useState<LessonsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [downloading, setDownloading] = useState(false);

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
    // 🎓 לקחים — refreshes together with the audit data; never breaks the tab on failure.
    try {
      const les = await api<LessonsResponse>("/api/audit/lessons", { timeoutMs: AUDIT_TIMEOUT_MS });
      setLessons(les);
    } catch {
      setLessons(null);
    }
  }, [fMode, fWindow, fStatus]);

  // ⬇ הורד JSON — מוריד את כל ה-audit.db המלא (GET /api/audit/export) כקובץ JSON.
  const downloadExport = async () => {
    setDownloading(true);
    try {
      const res = await api<{ rows: unknown[] }>("/api/audit/export", { timeoutMs: 60_000 });
      const blob = new Blob([JSON.stringify(res.rows ?? [], null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `audit-export-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert("הורדה נכשלה: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setDownloading(false);
    }
  };

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
    <div dir="rtl" style={{ padding: "4px 2px 40px", maxWidth: 1100, margin: "0 auto", display: "grid", gap: "var(--s-4)" }}>
      {/* 🎓 לקחים — מאמן העסקאות (ranked, mined from the ledger) */}
      <LessonsSection lessons={lessons} />

      {/* Header + summary + filters card */}
      <Card>
        {/* Header row */}
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: "var(--s-2)" }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)" }}>
              <span aria-hidden>📋</span>
              <SectionTitle as="h3">ביקורת עסקאות</SectionTitle>
            </div>
            <div style={{ fontSize: "0.8125rem", color: "var(--muted)", marginTop: 2 }}>
              כל עסקה מתועדת כאן — למה נכנסנו, מה קרה, ומה היה אפשר טוב יותר. הבסיס שה-AI ילמד ממנו.
            </div>
          </div>
          <div style={{ display: "flex", gap: "var(--s-2)" }}>
            <Button variant="ghost" onClick={() => void downloadExport()} disabled={downloading}>
              {downloading ? "מוריד…" : "⬇ הורד JSON"}
            </Button>
            <Button variant="ghost" onClick={() => void refresh()}>
              {loading ? "מרענן…" : "↻ רענן"}
            </Button>
          </div>
        </div>

        {/* Summary strip — stat tiles */}
        <div style={{ display: "flex", gap: "var(--s-3)", marginTop: "var(--s-4)", flexWrap: "wrap" }}>
          <div style={{ ...tileStyle(), flex: "1 1 120px", minWidth: 110, borderInlineStart: "4px solid var(--up)" }}>
            <div style={{ fontSize: "0.75rem", color: "var(--muted)", fontWeight: 600 }}>אחוז ניצחון</div>
            <div style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1, marginTop: 2, fontVariantNumeric: "tabular-nums", color: "var(--up)" }}>
              {counts ? `${counts.win_rate_pct.toFixed(1)}%` : "—"}
            </div>
          </div>
          <div style={{ ...tileStyle(), flex: "1 1 120px", minWidth: 110, borderInlineStart: "4px solid var(--accent)" }}>
            <div style={{ fontSize: "0.75rem", color: "var(--muted)", fontWeight: 600 }}>יעילות יציאה ממוצעת</div>
            <div style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>
              {counts && counts.avg_exit_efficiency != null ? `${(counts.avg_exit_efficiency * 100).toFixed(0)}%` : "—"}
            </div>
          </div>
          <div style={{ ...tileStyle(), flex: "1 1 120px", minWidth: 110 }}>
            <div style={{ fontSize: "0.75rem", color: "var(--muted)", fontWeight: 600 }}>סה"כ עסקאות</div>
            <div style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>{counts?.total ?? 0}</div>
          </div>
          {statusKeys.map((s) => {
            const m = statusChipColor(s);
            const tone = s === "WIN" ? "var(--up)" : s === "LOSS" ? "var(--down)" : "var(--border-strong)";
            return (
              <div key={s} style={{ ...tileStyle(), flex: "1 1 100px", minWidth: 90, borderInlineStart: `4px solid ${tone}` }}>
                <div style={{ fontSize: "0.75rem", color: m.color, fontWeight: 600 }}>{s}</div>
                <div style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>{byStatus[s] ?? 0}</div>
              </div>
            );
          })}
        </div>

        {/* Top lessons chips */}
        {counts && counts.top_lessons.length > 0 && (
          <div style={{ display: "flex", gap: "var(--s-2)", marginTop: "var(--s-3)", flexWrap: "wrap", alignItems: "center" }}>
            <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 700 }}>לקחים נפוצים:</span>
            {counts.top_lessons.map((l) => (
              <span key={l.lesson_tag} style={{
                fontSize: 12, fontWeight: 700, padding: "3px 10px", borderRadius: 999,
                color: SEV_META.medium.color, background: SEV_META.medium.bg, border: `1px solid ${SEV_META.medium.bg}`,
              }}>
                {l.lesson_tag} ×{l.n}
              </span>
            ))}
          </div>
        )}

        {/* Filters toolbar */}
        <div style={{ display: "flex", gap: "var(--s-2)", marginTop: "var(--s-4)", flexWrap: "wrap", alignItems: "center" }}>
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

        {/* about the data — tucked away by default */}
        <div style={{ marginTop: "var(--s-3)" }}>
          <Collapsible title="על הנתונים" subtitle="היכן נשמר וכיצד להוריד">
            <div style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", lineHeight: 1.6 }}>
              הנתונים נשמרים ב-audit.db (נפח /data בשרת). ניתן להוריד את הכל בעזרת "⬇ הורד JSON" כאן, או ישירות דרך /api/audit/export.
            </div>
          </Collapsible>
        </div>
      </Card>

      {err && <div style={{ color: "var(--down)", background: "var(--down-muted)", padding: "var(--s-3)", borderRadius: "var(--radius-sm)" }}>שגיאה בטעינה: {err}</div>}

      {/* Row list */}
      <div style={{ display: "grid", gap: "var(--s-2)" }}>
        {!loading && rows.length === 0 && (
          <div style={{ padding: "var(--s-6)", textAlign: "center", color: "var(--muted)", border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}>
            אין עסקאות להצגה
          </div>
        )}
        {rows.map((r) => {
          const isOpen = expanded.has(r.session_id);
          const sc = statusChipColor(r.settlement_status);
          const sigMark = r.signal_was_correct === true ? "✓" : r.signal_was_correct === false ? "✗" : "—";
          const sigColor = r.signal_was_correct === true ? "var(--up)" : r.signal_was_correct === false ? "var(--down)" : "var(--muted)";
          const ctx = r.context ?? {};
          const provenance = isPlainObject(ctx.provenance) ? ctx.provenance : {};
          const signalsMissing = provenance.signals_missing === true;
          // settlement fields are top-level audit columns (written at finalize), not in the decision context
          const resolvedOutcome = r.resolved_outcome ?? null;
          const btcStart = r.settlement_btc_start ?? null;
          const btcEnd = r.settlement_btc_end ?? null;
          return (
            <div key={r.session_id} style={{
              borderRadius: "var(--radius-md)", background: "var(--card)", border: "1px solid var(--border)",
              borderInlineStart: `4px solid ${statusColor(r.settlement_status)}`, overflow: "hidden",
              boxShadow: "var(--shadow-card)",
            }}>
              {/* Collapsed row */}
              <div style={{ display: "flex", alignItems: "center", gap: "var(--s-3)", padding: "var(--s-3) var(--s-4)", cursor: "pointer", overflowX: "auto" }}
                   onClick={() => toggleExpand(r.session_id)}>
                <span style={{
                  fontSize: 11, fontWeight: 800, padding: "3px 8px", borderRadius: 999,
                  color: sc.color, background: sc.bg, whiteSpace: "nowrap",
                }}>{r.settlement_status}</span>
                <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>{fmtTime(r.decision_ts)}</span>
                <span style={{ fontSize: 12, color: "var(--text)", whiteSpace: "nowrap" }}>{r.window_sec === 900 ? "15m" : "5m"}</span>
                <span style={{ fontSize: 12, fontWeight: 800, color: sideColor(r.side), whiteSpace: "nowrap" }}>{r.side}</span>
                <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>@{fmtNum(r.avg_fill_price)}</span>
                <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>×{r.contracts ?? "—"}</span>
                {r.exit_type && <span style={{ fontSize: 12, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{r.exit_type}</span>}
                <span style={{ fontSize: 13, fontWeight: 800, color: pnlColor(r.realized_pnl), whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>{fmtUsd(r.realized_pnl)}</span>
                <span style={{ fontSize: 12, fontWeight: 700, color: pnlColor(r.realized_pct), whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>ROI {fmtPct(r.realized_pct)}</span>
                <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>שיא {fmtPct(r.peak_unrealized_pct)}</span>
                <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>
                  יעילות {r.exit_efficiency != null ? `${r.exit_efficiency.toFixed(2)}×` : "—"}
                </span>
                <span style={{ fontSize: 13, fontWeight: 800, color: sigColor, whiteSpace: "nowrap" }} title="סיגנל צדק?">{sigMark}</span>
                {r.lesson_tag && (
                  <span style={{
                    fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999,
                    color: SEV_META.medium.color, background: SEV_META.medium.bg, whiteSpace: "nowrap",
                  }}>{r.lesson_tag}</span>
                )}
                <span style={{ flex: 1 }} />
                <span style={{ color: "var(--muted)", transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }}>‹</span>
              </div>

              {/* Drill-down */}
              {isOpen && (
                <div style={{ padding: "0 var(--s-4) var(--s-4)", borderTop: "1px solid var(--border)", display: "grid", gap: "var(--s-2)", fontSize: 13 }}>
                  {/* Top meta line */}
                  <div style={{ display: "flex", gap: "var(--s-4)", flexWrap: "wrap", color: "var(--muted)", marginTop: "var(--s-3)" }}>
                    <span>מצב: <b style={{ color: "var(--text)" }}>{r.mode}</b></span>
                    <span>שוק: <b style={{ color: "var(--text)" }}>{r.slug}</b></span>
                    <span>החלטה: {fmtTime(r.decision_ts)}</span>
                    <span>סגירה: {fmtTime(r.settled_ts)}</span>
                    <span>המלצה: <b style={{ color: "var(--text)" }}>{r.recommendation ?? "—"}</b></span>
                    <span>ציון משוקלל: <b style={{ color: "var(--text)" }}>{fmtNum(r.weighted_score)}</b></span>
                    <span>ביטחון: <b style={{ color: "var(--text)" }}>{r.confidence_pct != null ? `${r.confidence_pct.toFixed(0)}%` : "—"}</b></span>
                    <span>תנודתיות: <b style={{ color: "var(--text)" }}>{r.vol_bucket ?? "—"}</b></span>
                    <span>מכפיל התאוששות: <b style={{ color: "var(--text)" }}>{fmtNum(r.loss_recovery_multiplier)}</b></span>
                  </div>

                  {signalsMissing && (
                    <div style={{ fontSize: 12, color: "var(--muted)", fontStyle: "italic" }}>
                      אין נתוני סיגנל (עסקה היסטורית/לפני חיווט הסיגנלים)
                    </div>
                  )}

                  {/* PnL / efficiency line */}
                  <div style={{ display: "flex", gap: "var(--s-4)", flexWrap: "wrap", color: "var(--muted)" }}>
                    <span>שיא לא ממומש: <b style={{ color: pnlColor(r.peak_unrealized_pct) }}>{fmtPct(r.peak_unrealized_pct)}</b></span>
                    <span>שפל לא ממומש: <b style={{ color: pnlColor(r.trough_unrealized_pct) }}>{fmtPct(r.trough_unrealized_pct)}</b></span>
                    <span>רווח שהוחמץ: <b style={{ color: "var(--text)" }}>{fmtPct(r.missed_profit_pct)}</b></span>
                    <span>הסכמת סיגנלים: <b style={{ color: "var(--text)" }}>{r.signals_agreement != null ? `${(r.signals_agreement * 100).toFixed(0)}%` : "—"}</b></span>
                    <span>ניגוד סיגנלים: <b style={{ color: "var(--text)" }}>{r.signal_conflict === true ? "כן" : r.signal_conflict === false ? "לא" : "—"}</b></span>
                  </div>

                  {/* Sparkline */}
                  {r.pnl_path && r.pnl_path.length > 1 && (
                    <div style={{ marginTop: "var(--s-1)" }}>
                      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--accent-bright)", marginBottom: 2 }}>מסלול רווח/הפסד (% לא ממומש)</div>
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
                    <div style={{ marginTop: "var(--s-2)" }}>
                      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--accent-bright)", marginBottom: "var(--s-1)" }}>סיום עסקה</div>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-1) var(--s-4)", fontSize: 12, color: "var(--text-secondary)" }}>
                        <span>BTC פתיחה: <b style={{ color: "var(--text)" }}>{btcStart != null ? String(btcStart) : "—"}</b></span>
                        <span>BTC סגירה: <b style={{ color: "var(--text)" }}>{btcEnd != null ? String(btcEnd) : "—"}</b></span>
                        <span>תוצאה: <b style={{ color: "var(--text)" }}>{resolvedOutcome != null ? String(resolvedOutcome) : "—"}</b></span>
                      </div>
                    </div>
                  )}

                  {/* Counterfactuals */}
                  <div style={{ marginTop: "var(--s-2)" }}>
                    <div style={{ fontSize: 12, fontWeight: 700, color: "var(--accent-bright)", marginBottom: "var(--s-1)" }}>תרחישים נגדיים</div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-1) var(--s-4)", fontSize: 12, color: "var(--text-secondary)" }}>
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
                    <div style={{ marginTop: "var(--s-2)" }}>
                      <Collapsible title="נתונים גולמיים (JSON)" subtitle="ההקשר המלא של ההחלטה">
                        <pre style={{ margin: 0, padding: "var(--s-3)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", fontSize: 12, overflow: "auto", color: "var(--muted)" }}>
                          {JSON.stringify(ctx, null, 2)}
                        </pre>
                      </Collapsible>
                    </div>
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

// ── inline style helpers (match app design tokens) ───────────────────────────
// stat tile recipe — label + big value; caller adds flex/borderInlineStart accent
function tileStyle(): React.CSSProperties {
  return {
    padding: "var(--s-3)", borderRadius: "var(--radius-md)",
    background: "var(--bg-elevated)", border: "1px solid var(--border)",
  };
}
function selectStyle(): React.CSSProperties {
  return { padding: "7px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-strong)", background: "var(--card)", color: "var(--text)", fontSize: 13 };
}
