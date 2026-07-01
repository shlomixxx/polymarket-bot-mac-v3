import { useCallback, useEffect, useMemo, useState } from "react";
import { api, isPageHidden } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { Button } from "./ui/Button";

/** לשונית "תקלות ובאגים" — מעקב אחרי כשלים במערכת: חומרה, ספירה, טופל/לא, שיתוף. */

// manual-F1: timeout נדיב ל-/api/faults (ברירת המחדל 15s צרה מדי כשהמנוע עסוק — הסיבה שתקלה
// ידנית "לא נשמרה": ה-POST הצליח בשרת אבל הדפדפן הפיל אותו ב-15s). מיושר ל-45s כמו endpoints כבדים.
const FAULTS_TIMEOUT_MS = 45_000;

type Severity = "critical" | "high" | "medium" | "low";

type Fault = {
  id: number;
  dedup_key: string | null;
  first_ts: number;
  last_ts: number;
  count: number;
  category: string;
  severity: Severity | string;
  title: string;
  detail: string | null;
  source: string | null;
  context: Record<string, unknown>;
  handled: boolean;
  resolved_ts: number | null;
  resolution_note: string | null;
};

type Counts = {
  by_severity: Record<string, number>;
  open: number;
  handled: number;
  total: number;
  open_severe: number;
};

type FaultsResponse = { faults: Fault[]; counts: Counts };

// SEV_META — צבעי חומרה מכוונים (קריטי/גבוה/בינוני/נמוך): נשמרים כי הם מקודדים משמעות.
const SEV_META: Record<Severity, { label: string; color: string; bg: string }> = {
  critical: { label: "קריטי", color: "#fecaca", bg: "#7f1d1d" },
  high: { label: "גבוה", color: "#fed7aa", bg: "#7c2d12" },
  medium: { label: "בינוני", color: "#fde68a", bg: "#713f12" },
  low: { label: "נמוך", color: "#cbd5e1", bg: "#334155" },
};

function sevMeta(s: string) {
  return SEV_META[(s as Severity)] ?? SEV_META.low;
}

function fmtTime(ts: number): string {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString("he-IL", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return String(ts);
  }
}

function fmtAgo(ts: number): string {
  if (!ts) return "";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return `לפני ${sec}ש׳`;
  if (sec < 3600) return `לפני ${Math.floor(sec / 60)} דק׳`;
  if (sec < 86400) return `לפני ${Math.floor(sec / 3600)} שע׳`;
  return `לפני ${Math.floor(sec / 86400)} ימים`;
}

export default function FaultsTab() {
  const [data, setData] = useState<FaultsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [filterHandled, setFilterHandled] = useState<"open" | "handled" | "all">("open");
  const [filterSev, setFilterSev] = useState<Severity | "all">("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [busyId, setBusyId] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);

  // manual add
  const [showAdd, setShowAdd] = useState(false);
  const [mTitle, setMTitle] = useState("");
  const [mDetail, setMDetail] = useState("");
  const [mSev, setMSev] = useState<Severity>("medium");
  const [submitting, setSubmitting] = useState(false);  // manual-F1: double-submit guard

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const qs = new URLSearchParams();
      if (filterHandled === "open") qs.set("handled", "false");
      if (filterHandled === "handled") qs.set("handled", "true");
      if (filterSev !== "all") qs.set("severity", filterSev);
      const res = await api<FaultsResponse>(`/api/faults?${qs.toString()}`, { timeoutMs: FAULTS_TIMEOUT_MS });
      setData(res);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [filterHandled, filterSev]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // auto-refresh every 12s — מדלג כשהטאב מוסתר, ומרענן מיד בחזרה (C-10).
  useEffect(() => {
    const id = setInterval(() => { if (!isPageHidden()) void refresh(); }, 12000);
    const onVisible = () => { if (!isPageHidden()) void refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  const faults = data?.faults ?? [];
  const counts = data?.counts;

  const toggleExpand = (id: number) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  const setHandled = async (f: Fault, handled: boolean) => {
    setBusyId(f.id);
    try {
      let note = f.resolution_note ?? "";
      if (handled) {
        note = window.prompt("הערת טיפול (אופציונלי):", note) ?? note;
      }
      const res = await api<{ ok?: boolean }>(`/api/faults/${f.id}/handled`, {
        method: "POST",
        body: JSON.stringify({ handled, resolution_note: note }),
        timeoutMs: FAULTS_TIMEOUT_MS,
      });
      if (!res?.ok) { alert("פעולה נכשלה (השרת לא עדכן)"); return; }  // manual-F2
      await refresh();
    } catch (e) {
      alert("פעולה נכשלה: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setBusyId(null);
    }
  };

  const addManual = async () => {
    if (!mTitle.trim() || submitting) return;  // manual-F1: ignore double-submit
    setSubmitting(true);
    try {
      const res = await api<{ ok?: boolean }>("/api/faults", {
        method: "POST",
        body: JSON.stringify({ title: mTitle.trim(), detail: mDetail.trim(), severity: mSev, category: "manual" }),
        timeoutMs: FAULTS_TIMEOUT_MS,
      });
      if (!res?.ok) { alert("הוספה נכשלה (השרת לא שמר את התקלה)"); return; }  // manual-F2: no silent success
      setMTitle(""); setMDetail(""); setMSev("medium"); setShowAdd(false);
      await refresh();
    } catch (e) {
      alert("הוספה נכשלה: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setSubmitting(false);
    }
  };

  const clearHandled = async () => {
    if (!window.confirm("למחוק את כל התקלות שטופלו?")) return;
    try {
      await api(`/api/faults?only_handled=true`, { method: "DELETE", timeoutMs: FAULTS_TIMEOUT_MS });
      await refresh();
    } catch (e) {
      alert("מחיקה נכשלה: " + (e instanceof Error ? e.message : String(e)));
    }
  };

  const copyReport = async () => {
    const lines: string[] = [];
    lines.push(`# דוח תקלות מערכת — ${new Date().toLocaleString("he-IL")}`);
    if (counts) {
      lines.push(
        `סה"כ: ${counts.total} | פתוחות: ${counts.open} | חמורות פתוחות: ${counts.open_severe} | טופלו: ${counts.handled}`
      );
    }
    lines.push("");
    for (const f of faults) {
      lines.push(
        `- [${sevMeta(f.severity).label}] ${f.title} (×${f.count}) — ${f.category} — ${f.handled ? "טופל" : "פתוח"}`
      );
      if (f.detail) lines.push(`  פירוט: ${f.detail}`);
      if (f.source) lines.push(`  מקור: ${f.source}`);
      lines.push(`  נראה לאחרונה: ${fmtTime(f.last_ts)}`);
      if (f.context && Object.keys(f.context).length) lines.push(`  הקשר: ${JSON.stringify(f.context)}`);
    }
    const text = lines.join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      window.prompt("העתק ידנית:", text);
    }
  };

  const sevOrder: Severity[] = ["critical", "high", "medium", "low"];

  return (
    <div dir="rtl" style={{ display: "grid", gap: "var(--s-4)", maxWidth: 1100, margin: "0 auto", paddingBottom: "var(--s-6)" }}>
      {/* ── Header card ── */}
      <Card>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap", gap: "var(--s-3)" }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)" }}>
              <span style={{ fontSize: "1.25rem" }}>🐞</span>
              <SectionTitle as="h3" className="section-title--reset">תקלות ובאגים</SectionTitle>
            </div>
            <p style={{ margin: "var(--s-1) 0 0", color: "var(--muted)", fontSize: "0.8125rem" }}>
              כל כשל במערכת נתפס כאן אוטומטית — מה קרה, כמה פעמים, ובאיזו חומרה.
            </p>
          </div>
          <div style={{ display: "flex", gap: "var(--s-2)" }}>
            <Button variant="ghost" onClick={() => void refresh()}>
              {loading ? "מרענן…" : "↻ רענן"}
            </Button>
            <Button variant={copied ? "primary" : "ghost"} onClick={() => void copyReport()}>
              {copied ? "✓ הועתק" : "📋 העתק דוח"}
            </Button>
          </div>
        </div>

        {/* Severe banner */}
        {counts && counts.open_severe > 0 && (
          <div style={{
            marginTop: "var(--s-4)", padding: "var(--s-3) var(--s-4)", borderRadius: "var(--radius-md)",
            background: "var(--down-muted)", border: "1px solid rgba(184, 92, 92, 0.35)",
            color: "var(--down)", fontWeight: 600, display: "flex", alignItems: "center", gap: "var(--s-2)",
          }}>
            <span style={{ fontSize: "1.125rem" }}>⚠️</span>
            יש {counts.open_severe} תקלות חמורות פתוחות (קריטי/גבוה) שדורשות טיפול.
          </div>
        )}

        {/* Counts strip — stat tiles */}
        <div style={{ display: "flex", gap: "var(--s-3)", marginTop: "var(--s-4)", flexWrap: "wrap" }}>
          {sevOrder.map((s) => {
            const m = SEV_META[s];
            const n = counts?.by_severity?.[s] ?? 0;
            return (
              <div key={s} style={{
                flex: "1 1 120px", minWidth: 110, padding: "var(--s-3)", borderRadius: "var(--radius-md)",
                background: "var(--bg-elevated)", border: "1px solid var(--border)",
                borderInlineStart: `4px solid ${m.bg}`,
              }}>
                <div style={{ fontSize: "0.75rem", color: m.color, fontWeight: 600 }}>{m.label}</div>
                <div className="tabular-nums" style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>{n}</div>
              </div>
            );
          })}
          <div style={{
            flex: "1 1 120px", minWidth: 110, padding: "var(--s-3)", borderRadius: "var(--radius-md)",
            background: "var(--bg-elevated)", border: "1px solid var(--border)",
          }}>
            <div style={{ fontSize: "0.75rem", color: "var(--muted)", fontWeight: 600 }}>פתוחות / סה"כ</div>
            <div className="tabular-nums" style={{ fontSize: "1.5rem", fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>
              {counts?.open ?? 0}<span style={{ fontSize: "0.9375rem", color: "var(--muted)" }}> / {counts?.total ?? 0}</span>
            </div>
          </div>
        </div>
      </Card>

      {/* ── Toolbar ── */}
      <div style={{ display: "flex", gap: "var(--s-2)", flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ display: "flex", gap: "var(--s-1)", background: "var(--card)", padding: "var(--s-1)", borderRadius: "var(--radius-md)", border: "1px solid var(--border)" }}>
          {(["open", "all", "handled"] as const).map((k) => (
            <button key={k} type="button" onClick={() => setFilterHandled(k)} style={chipStyle(filterHandled === k)}>
              {k === "open" ? "פתוחות" : k === "handled" ? "טופלו" : "הכל"}
            </button>
          ))}
        </div>
        <select value={filterSev} onChange={(e) => setFilterSev(e.target.value as Severity | "all")} style={selectStyle()}>
          <option value="all">כל החומרות</option>
          {sevOrder.map((s) => <option key={s} value={s}>{SEV_META[s].label}</option>)}
        </select>
        <div style={{ flex: 1 }} />
        <Button variant="ghost" onClick={() => setShowAdd((v) => !v)}>＋ דווח תקלה</Button>
        <Button variant="ghost" onClick={() => void clearHandled()}>🗑 נקה שטופלו</Button>
      </div>

      {/* ── Manual add ── */}
      {showAdd && (
        <Card>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginBottom: "var(--s-3)" }}>
            <span style={{ fontSize: "1.05rem" }}>＋</span>
            <SectionTitle as="h3" className="section-title--reset">דיווח תקלה ידני</SectionTitle>
          </div>
          <div style={{ display: "grid", gap: "var(--s-2)" }}>
            <input value={mTitle} onChange={(e) => setMTitle(e.target.value)} placeholder="כותרת התקלה (חובה)" style={inputStyle()} />
            <textarea value={mDetail} onChange={(e) => setMDetail(e.target.value)} placeholder="פירוט / מה קרה / איך לשחזר" rows={3} style={{ ...inputStyle(), resize: "vertical" }} />
            <div style={{ display: "flex", gap: "var(--s-2)", alignItems: "center" }}>
              <select value={mSev} onChange={(e) => setMSev(e.target.value as Severity)} style={selectStyle()}>
                {sevOrder.map((s) => <option key={s} value={s}>{SEV_META[s].label}</option>)}
              </select>
              <div style={{ flex: 1 }} />
              <Button variant="primary" onClick={() => void addManual()} disabled={!mTitle.trim()}>שמור</Button>
              <Button variant="ghost" onClick={() => setShowAdd(false)}>ביטול</Button>
            </div>
          </div>
        </Card>
      )}

      {err && (
        <div className="alert-error" style={{ margin: 0 }}>שגיאה בטעינה: {err}</div>
      )}

      {/* ── List ── */}
      <div style={{ display: "grid", gap: "var(--s-2)" }}>
        {!loading && faults.length === 0 && (
          <div style={{ padding: "var(--s-6)", textAlign: "center", color: "var(--muted)", border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}>
            {filterHandled === "open" ? "🎉 אין תקלות פתוחות" : "אין תקלות להצגה"}
          </div>
        )}
        {faults.map((f) => {
          const m = sevMeta(f.severity);
          const isOpen = expanded.has(f.id);
          return (
            <div key={f.id} style={{
              borderRadius: "var(--radius-md)", background: "var(--card)", border: "1px solid var(--border)",
              borderInlineStart: `4px solid ${m.bg}`, opacity: f.handled ? 0.62 : 1, overflow: "hidden",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", padding: "var(--s-3) var(--s-4)", cursor: "pointer" }}
                   onClick={() => toggleExpand(f.id)}>
                <span style={{
                  fontSize: "0.6875rem", fontWeight: 800, padding: "3px 8px", borderRadius: 999,
                  color: m.color, background: m.bg, whiteSpace: "nowrap",
                }}>{m.label}</span>
                <span style={{ fontWeight: 700, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {f.title}
                </span>
                {f.count > 1 && (
                  <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "#fbbf24", background: "#78350f55", padding: "2px 8px", borderRadius: 999 }}>
                    ×{f.count}
                  </span>
                )}
                <span style={{ fontSize: "0.75rem", color: "var(--muted)", whiteSpace: "nowrap" }}>{fmtAgo(f.last_ts)}</span>
                {f.handled && <span style={{ fontSize: "0.6875rem", color: "var(--up)" }}>✓ טופל</span>}
                <span style={{ color: "var(--muted)", transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }}>‹</span>
              </div>
              {isOpen && (
                <div style={{ padding: "0 var(--s-4) var(--s-4)", borderTop: "1px solid var(--border)", display: "grid", gap: "var(--s-2)", fontSize: "0.8125rem" }}>
                  <div style={{ display: "flex", gap: "var(--s-4)", flexWrap: "wrap", color: "var(--muted)", marginTop: "var(--s-3)" }}>
                    <span>קטגוריה: <b style={{ color: "var(--text)" }}>{f.category}</b></span>
                    <span>מקור: <b style={{ color: "var(--text)" }}>{f.source || "—"}</b></span>
                    <span>נראה לראשונה: {fmtTime(f.first_ts)}</span>
                    <span>לאחרונה: {fmtTime(f.last_ts)}</span>
                  </div>
                  {f.detail && <div style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{f.detail}</div>}
                  {f.context && Object.keys(f.context).length > 0 && (
                    <pre style={{ margin: 0, padding: "var(--s-3)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", fontSize: "0.75rem", overflow: "auto", color: "var(--muted)" }}>
                      {JSON.stringify(f.context, null, 2)}
                    </pre>
                  )}
                  {f.handled && f.resolution_note && (
                    <div style={{ color: "var(--up)" }}>הערת טיפול: {f.resolution_note}</div>
                  )}
                  <div style={{ display: "flex", gap: "var(--s-2)", marginTop: "var(--s-1)" }}>
                    {!f.handled ? (
                      <Button variant="primary" disabled={busyId === f.id} onClick={() => void setHandled(f, true)}>
                        ✓ סמן כטופל
                      </Button>
                    ) : (
                      <Button variant="ghost" disabled={busyId === f.id} onClick={() => void setHandled(f, false)}>
                        ↩ פתח מחדש
                      </Button>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── inline style helpers (match app dark theme, tokenized) ───────────────────
function chipStyle(active: boolean): React.CSSProperties {
  return {
    padding: "6px 14px", borderRadius: "var(--radius-sm)", border: "none",
    background: active ? "var(--accent)" : "transparent", color: active ? "var(--text-on-accent)" : "var(--muted)",
    fontSize: "0.8125rem", fontWeight: 700, cursor: "pointer",
  };
}
function selectStyle(): React.CSSProperties {
  return { padding: "7px 10px", borderRadius: "var(--radius-md)", border: "1px solid var(--border-strong)", background: "var(--bg-elevated)", color: "var(--text)", fontSize: "0.8125rem" };
}
function inputStyle(): React.CSSProperties {
  return { padding: "9px 11px", borderRadius: "var(--radius-md)", border: "1px solid var(--border-strong)", background: "var(--bg-elevated)", color: "var(--text)", fontSize: "0.8125rem", width: "100%", boxSizing: "border-box" };
}
