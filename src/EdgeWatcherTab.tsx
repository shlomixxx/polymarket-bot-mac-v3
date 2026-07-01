import { useCallback, useEffect, useState } from "react";
import { api, isPageHidden } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { Button } from "./ui/Button";

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

// confidence chip — same visual language as the Faults / Audit tabs.
// high/medium/low encode meaning (like SEV_META) — keep the semantic amber for medium.
const CONF_META: Record<string, { label: string; color: string; bg: string }> = {
  high: { label: "גבוה", color: "#6ee7b7", bg: "#065f46" },
  medium: { label: "בינוני", color: "#fde68a", bg: "#713f12" },
  low: { label: "נמוך", color: "var(--text-secondary)", bg: "var(--bg-elevated)" },
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
      return { label: "במעקב", accent: "var(--accent)", tone: "var(--accent-muted)" };
    default:
      return { label: "אוסף נתונים", accent: "var(--muted)", tone: "var(--bg-elevated)" };
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
    bars.push({ label: "סה\"כ עסקאות מתויגות", value: collected, need: totalNeeded, color: "var(--accent)" });
  }
  if (sliceTotalNeeded > 0) {
    bars.push({
      label: "נחוץ לניתוח מובהק לפי-פלח (החסם האמיתי)",
      value: collected,
      need: sliceTotalNeeded,
      color: "var(--accent-bright)",
    });
  }
  if (bars.length === 0) return null;

  return (
    <div style={{ display: "grid", gap: "var(--s-3)" }}>
      {bars.map((b) => {
        const frac = b.need > 0 ? clamp01(b.value / b.need) : 0;
        return (
          <div key={b.label}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.75rem", color: "var(--muted)", marginBottom: "var(--s-1)" }}>
              <span>{b.label}</span>
              <span style={{ color: "var(--text)", fontWeight: 700 }} className="tabular-nums">
                {b.value.toLocaleString("he-IL")} / {b.need.toLocaleString("he-IL")}
              </span>
            </div>
            <div style={{ height: 10, borderRadius: 999, background: "var(--bg-elevated)", border: "1px solid var(--border)", overflow: "hidden" }}>
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
    <Card style={{ borderInlineStart: `5px solid ${sm.accent}` }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", flexWrap: "wrap" }}>
        <span
          style={{
            fontSize: "0.75rem", fontWeight: 800, padding: "3px 10px", borderRadius: 999,
            color: "var(--text)", background: sm.tone, whiteSpace: "nowrap",
          }}
        >
          מצב: {sm.label}
        </span>
      </div>
      <div style={{ fontSize: "1.125rem", fontWeight: 800, color: "var(--text)", marginTop: "var(--s-3)", lineHeight: 1.5 }}>{headline}</div>
      {data.note && (
        <div style={{ fontSize: "0.8125rem", color: "var(--muted)", marginTop: "var(--s-2)", lineHeight: 1.55 }}>{data.note}</div>
      )}
    </Card>
  );
}

// ── EdgeCard — one candidate setup, plain Hebrew, always with the OOS verdict ────
function EdgeCardView({ card }: { card: EdgeCard }) {
  const cm = confMeta(card.confidence);
  const oosMark = card.oos_confirmed ? "✓ אומת קדימה" : "🧪 טרם אומת ✗";
  const oosColor = card.oos_confirmed ? "var(--up)" : "var(--down)";
  return (
    <div
      style={{
        borderRadius: "var(--radius-md)", background: "var(--bg-elevated)", border: "1px solid var(--border)",
        borderInlineStart: `4px solid ${cm.bg}`, padding: "var(--s-3) var(--s-4)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", flexWrap: "wrap" }}>
        <span style={{ fontSize: 11, fontWeight: 800, padding: "3px 8px", borderRadius: 999, color: cm.color, background: cm.bg, whiteSpace: "nowrap" }}>
          {cm.label}
        </span>
        <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 8px", borderRadius: 999, color: "var(--accent-bright)", background: "var(--accent-muted)", border: "1px solid var(--border-strong)", whiteSpace: "nowrap" }}>
          {edgeTypeLabel(card.edge_type)}
        </span>
        <span style={{ fontSize: "0.75rem", fontWeight: 800, color: oosColor, whiteSpace: "nowrap" }}>{oosMark}</span>
      </div>

      <div style={{ fontSize: "0.875rem", fontWeight: 800, color: "var(--text)", marginTop: "var(--s-2)", lineHeight: 1.5 }}>{card.setup_he}</div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-1)", marginTop: "var(--s-3)" }}>
        <Pill k="אחוז הצלחה" v={fmtPct(card.hit_rate_pct)} />
        <Pill k="בסיס (השלמה)" v={fmtPct(card.baseline_pct)} />
        <Pill k="עודף (lift)" v={`+${fmtPct(card.lift_pct)}`} />
        <Pill k="מדגם" v={Number.isFinite(card.sample_n) ? String(card.sample_n) : "—"} />
        <Pill k="רווח נטו/עסקה (אחרי עמלות אמיתיות)" v={fmtUsd(card.net_dollars_per_trade)} />
        <Pill k="אישורים קדימה" v={Number.isFinite(card.confirmations) ? String(card.confirmations) : "—"} />
      </div>

      {!card.oos_confirmed && card.more_trades_to_confirm > 0 && (
        <div style={{ fontSize: "0.75rem", color: "var(--muted)", marginTop: "var(--s-2)" }}>
          צריך עוד ~{card.more_trades_to_confirm} עסקאות + מבחן קדימה שטרם עבר.
        </div>
      )}
    </div>
  );
}

function Pill({ k, v }: { k: string; v: string }) {
  return (
    <span className="tabular-nums" style={{ fontSize: 11, fontWeight: 600, padding: "3px 8px", borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", background: "var(--card)", border: "1px solid var(--border)", whiteSpace: "nowrap" }}>
      {k}: <b style={{ color: "var(--text)" }}>{v}</b>
    </span>
  );
}

// ── CandidateGrid — the list of EdgeCards (forming + confirmed only) ─────────────
function CandidateGrid({ cards }: { cards: EdgeCard[] }) {
  if (!cards || cards.length === 0) return null;
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", flexWrap: "wrap", marginBottom: "var(--s-3)" }}>
        <span aria-hidden>🔬</span>
        <SectionTitle as="h3" className="section-title--reset">מועמדים שנבחנים</SectionTitle>
        <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>כל setup נבדק מול בסיס + מבחן קדימה</span>
      </div>
      <div style={{ display: "grid", gap: "var(--s-2)" }}>
        {cards.map((c, i) => (
          <EdgeCardView key={c.slice_key ?? `${c.setup_he}-${i}`} card={c} />
        ))}
      </div>
    </Card>
  );
}

// ── HonestyFooter — ALWAYS shown (spec §5) ──────────────────────────────────────
function HonestyFooter() {
  return (
    <div
      style={{
        padding: "var(--s-3) var(--s-4)", borderRadius: "var(--radius-md)",
        background: "var(--bg-elevated)", border: "1px solid var(--border)",
        fontSize: "0.75rem", color: "var(--muted)", lineHeight: 1.6, fontStyle: "italic",
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
    <div dir="rtl" style={{ display: "grid", gap: "var(--s-4)", padding: "var(--s-1) 2px var(--s-6)", maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: "var(--s-2)" }}>
        <div>
          <h2 style={{ margin: 0, fontSize: "1.5rem", fontWeight: 800, letterSpacing: "-0.02em" }}>🔭 גלאי edge</h2>
          <p
            style={{ margin: "var(--s-1) 0 0", color: "var(--muted)", fontSize: "0.8125rem", lineHeight: 1.55, maxWidth: 640 }}
            title="verdict סטטיסטי אחד וכן מתוך כל העסקאות — האם צמח edge אמיתי שכדאי לשקול. רק כלי תיעוד וייעוץ: לעולם לא משנה לבד את מצב ההחלטה."
          >
            verdict אחד וכן מכל העסקאות — כלי תיעוד וייעוץ בלבד, לעולם לא משנה לבד את מצב ההחלטה.
          </p>
        </div>
        <Button variant="ghost" onClick={() => void refresh()}>
          {loading ? "מרענן…" : "↻ רענן"}
        </Button>
      </div>

      {err && (
        <div className="alert-error">
          שגיאה בטעינה: {err}
        </div>
      )}

      {!data && !err && (
        <div style={{ padding: "var(--s-6)", textAlign: "center", color: "var(--muted)", border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}>
          טוען…
        </div>
      )}

      {data && (
        <>
          <VerdictHero data={data} />

          {/* progress meter — only meaningful while still collecting / watching */}
          {(data.state === "collecting" || data.state === "watching") && (
            <Card>
              <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", flexWrap: "wrap", marginBottom: "var(--s-3)" }}>
                <span aria-hidden>📊</span>
                <SectionTitle as="h3" className="section-title--reset">התקדמות עד לסף</SectionTitle>
                <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>כמה נתונים עוד צריך כדי להכריע</span>
              </div>
              <ProgressMeter data={data} />
            </Card>
          )}

          {/* directional diagnostic note — information only, never a tradeable signal */}
          {data.directional_note_he && (
            <div style={{ padding: "var(--s-3) var(--s-4)", borderRadius: "var(--radius-md)", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderInlineStart: "4px solid var(--muted)", fontSize: "0.8125rem", color: "var(--text-secondary)", lineHeight: 1.55 }}>
              {data.directional_note_he}
            </div>
          )}

          {/* candidate cards (forming shows unconfirmed; confirmed shows the survivors) */}
          <CandidateGrid cards={data.candidates ?? []} />

          {/* ── THE autonomy nudge — the ONLY button, navigates, never flips mode ── */}
          {mayNudgeAutonomy && best && (
            <Card style={{ border: "1px solid #065f46", borderInlineStart: "5px solid var(--up)" }}>
              <div style={{ fontSize: "0.9375rem", fontWeight: 800, color: "#6ee7b7" }}>שקול להפעיל אוטונומיה</div>
              <div style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", marginTop: "var(--s-2)", lineHeight: 1.6 }}>{evidenceSentence(best)}</div>
              <div style={{ marginTop: "var(--s-3)" }}>
                <Button variant="primary" onClick={goToDecisionMode}>
                  ← למסך מצב ההחלטה
                </Button>
              </div>
              <div style={{ fontSize: "0.75rem", color: "var(--muted)", marginTop: "var(--s-2)" }}>
                הכפתור רק מנווט — ההחלטה להפעיל אוטונומיה היא שלך, ידנית, במסך האסטרטגיה.
              </div>
            </Card>
          )}

          <HonestyFooter />
        </>
      )}
    </div>
  );
}
