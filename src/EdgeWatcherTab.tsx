import { useCallback, useEffect, useState } from "react";
import { api, isPageHidden } from "./api";

/**
 * לשונית "🔭 גלאי edge" — מציגה verdict סטטיסטי אחד וכן (collecting/watching/forming/
 * confirmed) שנכרה מ-audit.db דרך GET /api/audit/edge (recording-only, off-loop, cache 60s).
 *
 * INVARIANTS (load-bearing — see docs/superpowers/specs/2026-06-08-edge-watcher-design.md §5):
 *  - HUMAN-ONLY AUTONOMY: this tab NEVER writes the decision mode. The only
 *    button NAVIGATES to the existing strategy tab + scroll-anchors to the 🤖 מצב החלטה block.
 *  - The autonomy nudge appears in EXACTLY one place — the `mayNudgeAutonomy` guard below —
 *    and only in the `confirmed` state with a forward-OOS-confirmed, high-confidence,
 *    non-directional best candidate that has >=3 confirmations and enough in-slice samples.
 *  - Hero verb is always "שקול" (consider), never an imperative to act.
 */

const EDGE_TIMEOUT_MS = 45_000;

// ── EdgeResponse shape (spec §4) — every field tolerated as missing/null ─────────
type EdgeCard = {
  setup_he: string;
  edge_type: "tp_reach" | "abstention" | "directional" | string;
  hit_rate_pct: number;
  baseline_pct: number;
  lift_pct: number;
  sample_n: number;
  net_dollars_per_trade: number;
  oos_confirmed: boolean;
  confirmations: number;
  confidence: "low" | "medium" | "high" | string;
  more_trades_to_confirm: number;
  slice_key?: string;
};

type EdgeResponse = {
  state: "collecting" | "watching" | "forming" | "confirmed" | string;
  trades_collected: number;
  trades_min_needed: number;
  trades_min_needed_in_slice: number;
  trades_min_total_for_slice?: number;
  best_candidate: EdgeCard | null;
  candidates: EdgeCard[];
  directional_note_he: string | null;
  note: string;
};

// ── formatters (defensive — never throw on null/NaN) ────────────────────────────
function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${v.toFixed(digits)}%`;
}
function fmtUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toFixed(3)}`;
}
function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(1, v));
}

// confidence chip — same visual language as the Faults / Audit tabs
const CONF_META: Record<string, { label: string; color: string; bg: string }> = {
  high: { label: "גבוה", color: "#6ee7b7", bg: "#065f46" },
  medium: { label: "בינוני", color: "#fde68a", bg: "#713f12" },
  low: { label: "נמוך", color: "#cbd5e1", bg: "#334155" },
};
function confMeta(c: string) {
  return CONF_META[c] ?? CONF_META.low;
}

// state palette — calm greys/blues until "confirmed" (the only "loud" state)
function stateMeta(s: string): { label: string; accent: string; tone: string } {
  switch (s) {
    case "confirmed":
      return { label: "מאושר", accent: "#10b981", tone: "#065f46" };
    case "forming":
      return { label: "מתגבש", accent: "#f59e0b", tone: "#713f12" };
    case "watching":
      return { label: "במעקב", accent: "#3b82f6", tone: "#1e3a5f" };
    default:
      return { label: "אוסף נתונים", accent: "#475569", tone: "#334155" };
  }
}

function edgeTypeLabel(t: string): string {
  if (t === "tp_reach") return "פגיעה ב-TP";
  if (t === "abstention") return "הימנעות (לדלג)";
  if (t === "directional") return "כיווני";
  return t;
}

// ── ProgressMeter — honest progress toward the binding gate (spec §3.3 / fix M4) ──
// We surface TWO requirements honestly: total labeled trades (TOTAL_MIN), and the
// per-slice effective sample that is the REAL binding constraint.
function ProgressMeter({ data }: { data: EdgeResponse }) {
  const collected = Number.isFinite(data.trades_collected) ? data.trades_collected : 0;
  const totalNeeded = Number.isFinite(data.trades_min_needed) ? data.trades_min_needed : 0;
  const sliceTotalNeeded =
    data.trades_min_total_for_slice && Number.isFinite(data.trades_min_total_for_slice)
      ? data.trades_min_total_for_slice
      : 0;

  const bars: Array<{ label: string; value: number; need: number; color: string }> = [];
  if (totalNeeded > 0) {
    bars.push({ label: "סה\"כ עסקאות מתויגות", value: collected, need: totalNeeded, color: "#3b82f6" });
  }
  if (sliceTotalNeeded > 0) {
    bars.push({
      label: "נחוץ לניתוח מובהק לפי-פלח (החסם האמיתי)",
      value: collected,
      need: sliceTotalNeeded,
      color: "#8b5cf6",
    });
  }
  if (bars.length === 0) return null;

  return (
    <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
      {bars.map((b) => {
        const frac = b.need > 0 ? clamp01(b.value / b.need) : 0;
        return (
          <div key={b.label}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--muted,#94a3b8)", marginBottom: 4 }}>
              <span>{b.label}</span>
              <span style={{ color: "#e2e8f0", fontWeight: 700 }}>
                {b.value.toLocaleString("he-IL")} / {b.need.toLocaleString("he-IL")}
              </span>
            </div>
            <div style={{ height: 10, borderRadius: 999, background: "#0b1220", border: "1px solid #1e293b", overflow: "hidden" }}>
              <div style={{ height: "100%", width: `${(frac * 100).toFixed(1)}%`, background: b.color, transition: "width .3s" }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── VerdictHero — the one-glance headline. Calm unless confirmed. ────────────────
function VerdictHero({ data }: { data: EdgeResponse }) {
  const sm = stateMeta(data.state);
  let headline: string;
  if (data.state === "confirmed") {
    // softened headline (fix M3) — verb is "שקול", never an imperative.
    headline = "סימן ל-edge שעבר את כל הבדיקות — שקול להפעיל אוטונומיה";
  } else if (data.state === "forming") {
    headline = "סימן מקדים ל-edge — עדיין לא מאושר. אל תפעל על סמך זה.";
  } else {
    headline =
      "אין עדיין edge מובהק — ממשיכים לאסוף נתונים. הכול תקין… אל תפעיל אוטונומיה עכשיו — אין מה להפעיל.";
  }
  return (
    <div
      style={{
        borderRadius: 14,
        background: "var(--card,#0f172a)",
        border: "1px solid #1e293b",
        borderInlineStart: `5px solid ${sm.accent}`,
        padding: "16px 18px",
        marginTop: 4,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span
          style={{
            fontSize: 12, fontWeight: 800, padding: "3px 10px", borderRadius: 999,
            color: "#e2e8f0", background: sm.tone, whiteSpace: "nowrap",
          }}
        >
          מצב: {sm.label}
        </span>
      </div>
      <div style={{ fontSize: 18, fontWeight: 800, color: "#e2e8f0", marginTop: 10, lineHeight: 1.5 }}>{headline}</div>
      {data.note && (
        <div style={{ fontSize: 13, color: "var(--muted,#94a3b8)", marginTop: 8, lineHeight: 1.55 }}>{data.note}</div>
      )}
    </div>
  );
}

// ── EdgeCard — one candidate setup, plain Hebrew, always with the OOS verdict ────
function EdgeCardView({ card }: { card: EdgeCard }) {
  const cm = confMeta(card.confidence);
  const oosMark = card.oos_confirmed ? "✓ אומת קדימה" : "🧪 טרם אומת ✗";
  const oosColor = card.oos_confirmed ? "#6ee7b7" : "#fca5a5";
  return (
    <div
      style={{
        borderRadius: 12, background: "#0b1220", border: "1px solid #1e293b",
        borderInlineStart: `4px solid ${cm.bg}`, padding: "12px 14px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: 11, fontWeight: 800, padding: "3px 8px", borderRadius: 999, color: cm.color, background: cm.bg, whiteSpace: "nowrap" }}>
          {cm.label}
        </span>
        <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 8px", borderRadius: 999, color: "#93c5fd", background: "#1e3a5f55", border: "1px solid #1e3a5f", whiteSpace: "nowrap" }}>
          {edgeTypeLabel(card.edge_type)}
        </span>
        <span style={{ fontSize: 12, fontWeight: 800, color: oosColor, whiteSpace: "nowrap" }}>{oosMark}</span>
      </div>

      <div style={{ fontSize: 14, fontWeight: 800, color: "#e2e8f0", marginTop: 8, lineHeight: 1.5 }}>{card.setup_he}</div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>
        <Pill k="אחוז הצלחה" v={fmtPct(card.hit_rate_pct)} />
        <Pill k="בסיס (השלמה)" v={fmtPct(card.baseline_pct)} />
        <Pill k="עודף (lift)" v={`+${fmtPct(card.lift_pct)}`} />
        <Pill k="מדגם" v={Number.isFinite(card.sample_n) ? String(card.sample_n) : "—"} />
        <Pill k="רווח נטו/עסקה (אחרי עמלות אמיתיות)" v={fmtUsd(card.net_dollars_per_trade)} />
        <Pill k="אישורים קדימה" v={Number.isFinite(card.confirmations) ? String(card.confirmations) : "—"} />
      </div>

      {!card.oos_confirmed && card.more_trades_to_confirm > 0 && (
        <div style={{ fontSize: 12, color: "var(--muted,#94a3b8)", marginTop: 8 }}>
          צריך עוד ~{card.more_trades_to_confirm} עסקאות + מבחן קדימה שטרם עבר.
        </div>
      )}
    </div>
  );
}

function Pill({ k, v }: { k: string; v: string }) {
  return (
    <span style={{ fontSize: 11, fontWeight: 600, padding: "3px 8px", borderRadius: 8, color: "#cbd5e1", background: "#0f172a", border: "1px solid #1e293b", whiteSpace: "nowrap" }}>
      {k}: <b style={{ color: "#e2e8f0" }}>{v}</b>
    </span>
  );
}

// ── CandidateGrid — the list of EdgeCards (forming + confirmed only) ─────────────
function CandidateGrid({ cards }: { cards: EdgeCard[] }) {
  if (!cards || cards.length === 0) return null;
  return (
    <div style={{ marginTop: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: "#93c5fd", marginBottom: 8 }}>מועמדים שנבחנים</div>
      <div style={{ display: "grid", gap: 8 }}>
        {cards.map((c, i) => (
          <EdgeCardView key={c.slice_key ?? `${c.setup_he}-${i}`} card={c} />
        ))}
      </div>
    </div>
  );
}

// ── HonestyFooter — ALWAYS shown (spec §5) ──────────────────────────────────────
function HonestyFooter() {
  return (
    <div
      style={{
        marginTop: 20, padding: "12px 14px", borderRadius: 10,
        background: "#0b1220", border: "1px solid #1e293b",
        fontSize: 12, color: "var(--muted,#94a3b8)", lineHeight: 1.6, fontStyle: "italic",
      }}
    >
      'edge' נחשב אמיתי רק אחרי מבחן קדימה (out-of-sample בזמן אמת), תיקון לריבוי-בדיקות, וסף
      רווחיות אחרי עמלות אמיתיות (~3-4%).
    </div>
  );
}

export default function EdgeWatcherTab({ onGoToStrategy }: { onGoToStrategy: () => void }) {
  const [data, setData] = useState<EdgeResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await api<EdgeResponse>("/api/audit/edge", { timeoutMs: EDGE_TIMEOUT_MS });
      setData(res);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

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

  // ── THE single autonomy-nudge guard (spec §5) — the ONLY place a button appears. ──
  // It NAVIGATES only (onGoToStrategy maps to the strategy tab in App) — it NEVER flips the mode.
  const best = data?.best_candidate ?? null;
  const mayNudgeAutonomy =
    data?.state === "confirmed" &&
    best?.oos_confirmed === true &&
    best?.confidence === "high" &&
    best?.edge_type !== "directional" &&
    (best?.confirmations ?? 0) >= 3 &&
    (best?.sample_n ?? 0) >= (data?.trades_min_needed_in_slice ?? Infinity);

  // navigate to the strategy tab and scroll-anchor to the 🤖 מצב החלטה block.
  // NOTE: this NEVER writes the decision mode — the human chooses there.
  const goToDecisionMode = () => {
    onGoToStrategy();
    // wait a tick for the strategy tab to mount, then scroll the anchor into view.
    setTimeout(() => {
      try {
        const el = document.getElementById("decision-mode-block");
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch {
        /* scrolling is best-effort — never break the nudge */
      }
    }, 60);
  };

  // evidence sentence for the confirmed hero (one plain sentence — fix M3).
  const evidenceSentence = (c: EdgeCard): string =>
    `אחוז הצלחה ${fmtPct(c.hit_rate_pct)} מול בסיס ${fmtPct(c.baseline_pct)} ` +
    `(עודף +${fmtPct(c.lift_pct)}), מדגם ${c.sample_n}, רווח נטו ${fmtUsd(c.net_dollars_per_trade)}/עסקה ` +
    `אחרי עמלות אמיתיות, מבחן קדימה ✓, ${c.confirmations} אישורים.`;

  return (
    <div dir="rtl" style={{ padding: "4px 2px 40px", maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 24, fontWeight: 800, letterSpacing: "-0.02em" }}>🔭 גלאי edge</h2>
          <p style={{ margin: "4px 0 0", color: "var(--muted, #94a3b8)", fontSize: 13, lineHeight: 1.55 }}>
            verdict סטטיסטי אחד וכן מתוך כל העסקאות — האם צמח edge אמיתי שכדאי לשקול. רק כלי תיעוד
            וייעוץ: לעולם לא משנה לבד את מצב ההחלטה.
          </p>
        </div>
        <button type="button" onClick={() => void refresh()} style={btnStyle()}>
          {loading ? "מרענן…" : "↻ רענן"}
        </button>
      </div>

      {err && (
        <div style={{ marginTop: 14, color: "#fecaca", background: "#7f1d1d33", padding: 12, borderRadius: 10 }}>
          שגיאה בטעינה: {err}
        </div>
      )}

      {!data && !err && (
        <div style={{ marginTop: 16, padding: 40, textAlign: "center", color: "var(--muted,#94a3b8)", border: "1px dashed #1e293b", borderRadius: 12 }}>
          טוען…
        </div>
      )}

      {data && (
        <>
          <VerdictHero data={data} />

          {/* progress meter — only meaningful while still collecting / watching */}
          {(data.state === "collecting" || data.state === "watching") && <ProgressMeter data={data} />}

          {/* directional diagnostic note — information only, never a tradeable signal */}
          {data.directional_note_he && (
            <div style={{ marginTop: 14, padding: "10px 14px", borderRadius: 10, background: "#0b1220", border: "1px solid #1e293b", borderInlineStart: "4px solid #475569", fontSize: 13, color: "#cbd5e1", lineHeight: 1.55 }}>
              {data.directional_note_he}
            </div>
          )}

          {/* candidate cards (forming shows unconfirmed; confirmed shows the survivors) */}
          <CandidateGrid cards={data.candidates ?? []} />

          {/* ── THE autonomy nudge — the ONLY button, navigates, never flips mode ── */}
          {mayNudgeAutonomy && best && (
            <div
              style={{
                marginTop: 18, padding: "16px 18px", borderRadius: 14,
                background: "var(--card,#0f172a)", border: "1px solid #065f46",
                borderInlineStart: "5px solid #10b981",
              }}
            >
              <div style={{ fontSize: 15, fontWeight: 800, color: "#6ee7b7" }}>שקול להפעיל אוטונומיה</div>
              <div style={{ fontSize: 13, color: "#cbd5e1", marginTop: 8, lineHeight: 1.6 }}>{evidenceSentence(best)}</div>
              <div style={{ marginTop: 12 }}>
                <button type="button" onClick={goToDecisionMode} style={btnStyle("#065f46")}>
                  ← למסך מצב ההחלטה
                </button>
              </div>
              <div style={{ fontSize: 12, color: "var(--muted,#94a3b8)", marginTop: 8 }}>
                הכפתור רק מנווט — ההחלטה להפעיל אוטונומיה היא שלך, ידנית, במסך האסטרטגיה.
              </div>
            </div>
          )}

          <HonestyFooter />
        </>
      )}
    </div>
  );
}

// ── inline style helpers (match app dark theme, mirrors AuditTab) ────────────────
function btnStyle(bg?: string): React.CSSProperties {
  return {
    padding: "7px 12px", borderRadius: 9, border: "1px solid #1e293b",
    background: bg ?? "var(--card,#0f172a)", color: "#e2e8f0", fontSize: 13, fontWeight: 600,
    cursor: "pointer",
  };
}
