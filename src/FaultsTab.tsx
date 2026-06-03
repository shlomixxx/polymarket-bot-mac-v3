import { useCallback, useEffect, useMemo, useState } from "react";
import { api, isPageHidden } from "./api";

/** לשונית "תקלות ובאגים" — מעקב אחרי כשלים במערכת: חומרה, ספירה, טופל/לא, שיתוף. */

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

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const qs = new URLSearchParams();
      if (filterHandled === "open") qs.set("handled", "false");
      if (filterHandled === "handled") qs.set("handled", "true");
      if (filterSev !== "all") qs.set("severity", filterSev);
      const res = await api<FaultsResponse>(`/api/faults?${qs.toString()}`);
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
      await api(`/api/faults/${f.id}/handled`, {
        method: "POST",
        body: JSON.stringify({ handled, resolution_note: note }),
      });
      await refresh();
    } catch (e) {
      alert("פעולה נכשלה: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setBusyId(null);
    }
  };

  const addManual = async () => {
    if (!mTitle.trim()) return;
    try {
      await api("/api/faults", {
        method: "POST",
        body: JSON.stringify({ title: mTitle.trim(), detail: mDetail.trim(), severity: mSev, category: "manual" }),
      });
      setMTitle(""); setMDetail(""); setMSev("medium"); setShowAdd(false);
      await refresh();
    } catch (e) {
      alert("הוספה נכשלה: " + (e instanceof Error ? e.message : String(e)));
    }
  };

  const clearHandled = async () => {
    if (!window.confirm("למחוק את כל התקלות שטופלו?")) return;
    try {
      await api(`/api/faults?only_handled=true`, { method: "DELETE" });
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
    <div dir="rtl" style={{ padding: "4px 2px 40px", maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 24, fontWeight: 800, letterSpacing: "-0.02em" }}>🐞 תקלות ובאגים</h2>
          <p style={{ margin: "4px 0 0", color: "var(--muted, #94a3b8)", fontSize: 13 }}>
            כל כשל במערכת נתפס כאן אוטומטית — מה קרה, כמה פעמים, חומרה, והאם טופל. אפשר גם לדווח ידנית ולשתף דוח.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" onClick={() => void refresh()} style={btnStyle()}>
            {loading ? "מרענן…" : "↻ רענן"}
          </button>
          <button type="button" onClick={() => void copyReport()} style={btnStyle(copied ? "#065f46" : undefined)}>
            {copied ? "✓ הועתק" : "📋 העתק דוח"}
          </button>
        </div>
      </div>

      {/* Severe banner */}
      {counts && counts.open_severe > 0 && (
        <div style={{
          marginTop: 14, padding: "12px 16px", borderRadius: 12,
          background: "linear-gradient(90deg, #7f1d1d33, #7c2d1222)", border: "1px solid #ef444455",
          color: "#fecaca", fontWeight: 600, display: "flex", alignItems: "center", gap: 10,
        }}>
          <span style={{ fontSize: 18 }}>⚠️</span>
          יש {counts.open_severe} תקלות חמורות פתוחות (קריטי/גבוה) שדורשות טיפול.
        </div>
      )}

      {/* Counts strip */}
      <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap" }}>
        {sevOrder.map((s) => {
          const m = SEV_META[s];
          const n = counts?.by_severity?.[s] ?? 0;
          return (
            <div key={s} style={{
              flex: "1 1 120px", minWidth: 110, padding: "12px 14px", borderRadius: 12,
              background: "var(--card, #0f172a)", border: `1px solid ${m.bg}`,
              borderInlineStart: `4px solid ${m.bg}`,
            }}>
              <div style={{ fontSize: 12, color: m.color, fontWeight: 700 }}>{m.label}</div>
              <div style={{ fontSize: 26, fontWeight: 800, lineHeight: 1.1, marginTop: 2 }}>{n}</div>
            </div>
          );
        })}
        <div style={{
          flex: "1 1 120px", minWidth: 110, padding: "12px 14px", borderRadius: 12,
          background: "var(--card, #0f172a)", border: "1px solid #1e293b",
        }}>
          <div style={{ fontSize: 12, color: "var(--muted, #94a3b8)", fontWeight: 700 }}>פתוחות / סה"כ</div>
          <div style={{ fontSize: 26, fontWeight: 800, lineHeight: 1.1, marginTop: 2 }}>
            {counts?.open ?? 0}<span style={{ fontSize: 15, color: "var(--muted, #94a3b8)" }}> / {counts?.total ?? 0}</span>
          </div>
        </div>
      </div>

      {/* Toolbar */}
      <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 4, background: "var(--card,#0f172a)", padding: 4, borderRadius: 10, border: "1px solid #1e293b" }}>
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
        <button type="button" onClick={() => setShowAdd((v) => !v)} style={btnStyle()}>＋ דווח תקלה</button>
        <button type="button" onClick={() => void clearHandled()} style={btnStyle()}>🗑 נקה שטופלו</button>
      </div>

      {/* Manual add */}
      {showAdd && (
        <div style={{ marginTop: 12, padding: 14, borderRadius: 12, background: "var(--card,#0f172a)", border: "1px solid #1e293b", display: "grid", gap: 8 }}>
          <input value={mTitle} onChange={(e) => setMTitle(e.target.value)} placeholder="כותרת התקלה (חובה)" style={inputStyle()} />
          <textarea value={mDetail} onChange={(e) => setMDetail(e.target.value)} placeholder="פירוט / מה קרה / איך לשחזר" rows={3} style={{ ...inputStyle(), resize: "vertical" }} />
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select value={mSev} onChange={(e) => setMSev(e.target.value as Severity)} style={selectStyle()}>
              {sevOrder.map((s) => <option key={s} value={s}>{SEV_META[s].label}</option>)}
            </select>
            <div style={{ flex: 1 }} />
            <button type="button" onClick={() => void addManual()} style={btnStyle("#1d4ed8")} disabled={!mTitle.trim()}>שמור</button>
            <button type="button" onClick={() => setShowAdd(false)} style={btnStyle()}>ביטול</button>
          </div>
        </div>
      )}

      {err && <div style={{ marginTop: 14, color: "#fecaca", background: "#7f1d1d33", padding: 12, borderRadius: 10 }}>שגיאה בטעינה: {err}</div>}

      {/* List */}
      <div style={{ marginTop: 16, display: "grid", gap: 8 }}>
        {!loading && faults.length === 0 && (
          <div style={{ padding: 40, textAlign: "center", color: "var(--muted,#94a3b8)", border: "1px dashed #1e293b", borderRadius: 12 }}>
            {filterHandled === "open" ? "🎉 אין תקלות פתוחות" : "אין תקלות להצגה"}
          </div>
        )}
        {faults.map((f) => {
          const m = sevMeta(f.severity);
          const isOpen = expanded.has(f.id);
          return (
            <div key={f.id} style={{
              borderRadius: 12, background: "var(--card,#0f172a)", border: "1px solid #1e293b",
              borderInlineStart: `4px solid ${m.bg}`, opacity: f.handled ? 0.62 : 1, overflow: "hidden",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", cursor: "pointer" }}
                   onClick={() => toggleExpand(f.id)}>
                <span style={{
                  fontSize: 11, fontWeight: 800, padding: "3px 8px", borderRadius: 999,
                  color: m.color, background: m.bg, whiteSpace: "nowrap",
                }}>{m.label}</span>
                <span style={{ fontWeight: 700, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {f.title}
                </span>
                {f.count > 1 && (
                  <span style={{ fontSize: 12, fontWeight: 700, color: "#fbbf24", background: "#78350f55", padding: "2px 8px", borderRadius: 999 }}>
                    ×{f.count}
                  </span>
                )}
                <span style={{ fontSize: 12, color: "var(--muted,#94a3b8)", whiteSpace: "nowrap" }}>{fmtAgo(f.last_ts)}</span>
                {f.handled && <span style={{ fontSize: 11, color: "#6ee7b7" }}>✓ טופל</span>}
                <span style={{ color: "var(--muted,#94a3b8)", transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }}>‹</span>
              </div>
              {isOpen && (
                <div style={{ padding: "0 14px 14px", borderTop: "1px solid #1e293b", display: "grid", gap: 6, fontSize: 13 }}>
                  <div style={{ display: "flex", gap: 16, flexWrap: "wrap", color: "var(--muted,#94a3b8)", marginTop: 10 }}>
                    <span>קטגוריה: <b style={{ color: "#e2e8f0" }}>{f.category}</b></span>
                    <span>מקור: <b style={{ color: "#e2e8f0" }}>{f.source || "—"}</b></span>
                    <span>נראה לראשונה: {fmtTime(f.first_ts)}</span>
                    <span>לאחרונה: {fmtTime(f.last_ts)}</span>
                  </div>
                  {f.detail && <div style={{ color: "#e2e8f0", whiteSpace: "pre-wrap" }}>{f.detail}</div>}
                  {f.context && Object.keys(f.context).length > 0 && (
                    <pre style={{ margin: 0, padding: 10, background: "#020617", borderRadius: 8, fontSize: 12, overflow: "auto", color: "#94a3b8" }}>
                      {JSON.stringify(f.context, null, 2)}
                    </pre>
                  )}
                  {f.handled && f.resolution_note && (
                    <div style={{ color: "#6ee7b7" }}>הערת טיפול: {f.resolution_note}</div>
                  )}
                  <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                    {!f.handled ? (
                      <button type="button" disabled={busyId === f.id} onClick={() => void setHandled(f, true)} style={btnStyle("#065f46")}>
                        ✓ סמן כטופל
                      </button>
                    ) : (
                      <button type="button" disabled={busyId === f.id} onClick={() => void setHandled(f, false)} style={btnStyle()}>
                        ↩ פתח מחדש
                      </button>
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

// ── inline style helpers (match app dark theme) ──────────────────────────────
function btnStyle(bg?: string): React.CSSProperties {
  return {
    padding: "7px 12px", borderRadius: 9, border: "1px solid #1e293b",
    background: bg ?? "var(--card,#0f172a)", color: "#e2e8f0", fontSize: 13, fontWeight: 600,
    cursor: "pointer",
  };
}
function chipStyle(active: boolean): React.CSSProperties {
  return {
    padding: "6px 14px", borderRadius: 8, border: "none",
    background: active ? "#1d4ed8" : "transparent", color: active ? "#fff" : "#94a3b8",
    fontSize: 13, fontWeight: 700, cursor: "pointer",
  };
}
function selectStyle(): React.CSSProperties {
  return { padding: "7px 10px", borderRadius: 9, border: "1px solid #1e293b", background: "var(--card,#0f172a)", color: "#e2e8f0", fontSize: 13 };
}
function inputStyle(): React.CSSProperties {
  return { padding: "9px 11px", borderRadius: 9, border: "1px solid #1e293b", background: "#020617", color: "#e2e8f0", fontSize: 13, width: "100%", boxSizing: "border-box" };
}
