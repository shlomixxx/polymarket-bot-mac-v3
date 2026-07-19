import { useCallback, useEffect, useRef, useState } from "react";
import { api, isPageHidden } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { Button } from "./ui/Button";
import { Collapsible } from "./ui/Collapsible";
import { israelTime, israelDateTime } from "./timeFormat";

// ─── Types ────────────────────────────────────────────────────────────────────

type TriggerMode = "off" | "momentum" | "signal" | "dca_pulse";

type TriggerConfig = {
  mode: TriggerMode;
  momentum_pct: number;
  momentum_window_sec: number;
  momentum_direction: "auto" | "Up" | "Down";
  signal_confidence: number;
  signal_direction: "auto" | "Up" | "Down";
  dca_pulse_slices: number;
  dca_pulse_interval_sec: number;
  dca_pulse_direction: "auto" | "Up" | "Down";
  investment_usd: number;
  entry_price_cents: number;
  take_profit_pct: number;
  max_triggers_per_window: number;
  cooldown_sec: number;
  min_seconds_remaining: number;
  contract_max_drift_pct: number;
  auto_start: boolean;
  btc_window: "5m" | "15m";
  dca_sizing: "equal" | "pyramid" | "fixed_contracts";
  dca_min_step_pct: number;
};

type TriggerEvent = {
  ts: number;
  event_type: "executed" | "skipped" | "error" | "cooldown" | "triggered";
  trigger_mode: string;
  side: string | null;
  price: number | null;
  contract_ask: number | null;
  contracts: number | null;
  note: string;
};

type StatusEntry = { ts: number; msg: string };

type TriggerState = {
  active: boolean;
  mode: TriggerMode;
  status: string;
  status_log: StatusEntry[];
  last_trigger_ts: number;
  triggers_this_window: number;
  cooldown_remaining: number | null;
  current_btc_change_pct: number | null;
  current_signal_confidence: number | null;
  current_signal_rec: string;
  events: TriggerEvent[];
  config: TriggerConfig;
};

// ─── Defaults ─────────────────────────────────────────────────────────────────

const DEFAULT_CONFIG: TriggerConfig = {
  mode: "momentum",
  momentum_pct: 0.20,
  momentum_window_sec: 60,
  momentum_direction: "auto",
  signal_confidence: 0.68,
  signal_direction: "auto",
  dca_pulse_slices: 3,
  dca_pulse_interval_sec: 20,
  dca_pulse_direction: "Up",
  investment_usd: 5,
  entry_price_cents: 30,
  take_profit_pct: 15,
  max_triggers_per_window: 2,
  cooldown_sec: 60,
  min_seconds_remaining: 90,
  contract_max_drift_pct: 30,
  auto_start: false,
  btc_window: "5m",
  dca_sizing: "equal",
  dca_min_step_pct: 0,
};

// ─── Small helpers ────────────────────────────────────────────────────────────

function ts2time(ts: number) {
  return israelTime(ts);
}

function eventIcon(type: TriggerEvent["event_type"]) {
  return type === "executed" ? "✅"
    : type === "error" ? "⚠"
    : type === "skipped" ? "⏭"
    : "•";
}

function eventColor(type: TriggerEvent["event_type"]) {
  return type === "executed" ? "var(--up)"
    : type === "error" ? "var(--down)"
    : "var(--muted)";
}

// ─── Slider input ─────────────────────────────────────────────────────────────

function SliderField({
  label, value, min, max, step, unit, onChange,
}: {
  label: string; value: number; min: number; max: number; step: number; unit?: string;
  onChange: (v: number) => void;
}) {
  const [raw, setRaw] = useState(String(value));
  const [focused, setFocused] = useState(false);

  // sync raw when value changes externally (e.g. slider)
  useEffect(() => {
    if (!focused) setRaw(String(value));
  }, [value, focused]);

  const commit = (str: string) => {
    const n = parseFloat(str);
    if (!isNaN(n) && n >= 0) onChange(n);
    else setRaw(String(value));
  };

  return (
    <div style={{ marginBottom: "var(--s-4)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--s-1)" }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{label}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 2 }}>
          <input
            type="number"
            value={raw}
            min={0}
            step={step}
            onFocus={() => setFocused(true)}
            onChange={e => setRaw(e.target.value)}
            onBlur={e => { setFocused(false); commit(e.target.value); }}
            onKeyDown={e => { if (e.key === "Enter") { setFocused(false); commit(raw); } }}
            style={{
              width: 72, textAlign: "right", fontSize: 14, fontWeight: 700,
              fontVariantNumeric: "tabular-nums",
              background: "var(--bg-elevated)",
              border: "1px solid var(--border-strong)",
              borderRadius: "var(--radius-sm)", padding: "2px 6px",
              color: "var(--accent-bright)",
              outline: "none",
            }}
          />
          {unit && <span style={{ fontSize: 12, color: "var(--muted)", marginRight: 2 }}>{unit}</span>}
        </div>
      </div>
      <input
        type="range"
        min={min} max={Math.max(max, value)} step={step}
        value={Math.min(value, Math.max(max, value))}
        onChange={e => { const n = Number(e.target.value); onChange(n); setRaw(String(n)); }}
        style={{ width: "100%", accentColor: "var(--accent-bright)", cursor: "pointer" }}
      />
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--muted)", marginTop: 2 }}>
        <span>{min}{unit ?? ""}</span>
        <span style={{ color: value > max ? "var(--accent-bright)" : undefined }}>
          {value > max ? `${value}${unit ?? ""}` : `${max}${unit ?? ""}`}
        </span>
      </div>
    </div>
  );
}

function DirectionPicker({
  value, onChange, showAuto = true,
}: {
  value: string; onChange: (v: string) => void; showAuto?: boolean;
}) {
  const opts = showAuto
    ? [["auto", "🔁 אוטו"], ["Up", "⬆ Up"], ["Down", "⬇ Down"]]
    : [["Up", "⬆ Up"], ["Down", "⬇ Down"]];
  return (
    <div style={{ display: "flex", gap: 6, marginBottom: "var(--s-3)" }}>
      {opts.map(([v, label]) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          style={{
            flex: 1, padding: "8px 4px", borderRadius: "var(--radius-sm)",
            border: `1px solid ${value === v ? "transparent" : "var(--border)"}`,
            fontWeight: 700, fontSize: 12, cursor: "pointer",
            background: value === v
              ? v === "Up" ? "var(--up)" : v === "Down" ? "var(--down)" : "var(--accent-muted)"
              : "var(--bg-elevated)",
            color: value === v
              ? v === "Up" || v === "Down" ? "#fff" : "var(--accent-bright)"
              : "var(--muted)",
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

// ─── Mode Cards ───────────────────────────────────────────────────────────────

const MODES: { id: TriggerMode; icon: string; title: string; desc: string }[] = [
  { id: "momentum", icon: "🚀", title: "מומנטום", desc: "BTC זז X% בY שנ׳ → כניסה אוטו" },
  { id: "signal",   icon: "📡", title: "סיגנל",   desc: "ביטחון > סף → כניסה לפי המלצה" },
  { id: "dca_pulse",icon: "🔄", title: "DCA פולס", desc: "N סלייסים מהירים ברצף" },
];

function ModeCard({
  mode, selected, onClick,
}: {
  mode: typeof MODES[0]; selected: boolean; onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        flex: 1, padding: "14px 10px", borderRadius: "var(--radius-lg)",
        border: `2px solid ${selected ? "var(--accent-bright)" : "var(--border)"}`,
        background: selected ? "var(--accent-muted)" : "var(--card)",
        cursor: "pointer", textAlign: "center",
        transition: "all 0.15s ease",
      }}
    >
      <div style={{ fontSize: 24, marginBottom: 4 }}>{mode.icon}</div>
      <div style={{ fontWeight: 700, fontSize: 13, color: selected ? "var(--accent-bright)" : "var(--text)" }}>
        {mode.title}
      </div>
      <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2, lineHeight: 1.4 }}>
        {mode.desc}
      </div>
    </button>
  );
}

// ─── Live Momentum Gauge ──────────────────────────────────────────────────────

function MomentumGauge({
  changePct, threshold,
}: {
  changePct: number | null; threshold: number;
}) {
  if (changePct == null) {
    return <div style={{ textAlign: "center", color: "var(--muted)", fontSize: 12, padding: "8px 0" }}>בונה נתונים…</div>;
  }
  const abs = Math.abs(changePct);
  const fill = Math.min(abs / Math.max(threshold, 0.01), 1);
  const isUp = changePct >= 0;
  const color = isUp ? "var(--up)" : "var(--down)";
  return (
    <div style={{ padding: "8px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <span style={{ fontSize: 13, color: "var(--muted)" }}>שינוי BTC:</span>
        <strong style={{ fontSize: 18, color, fontVariantNumeric: "tabular-nums" }}>
          {changePct > 0 ? "+" : ""}{changePct.toFixed(3)}%
        </strong>
        <span style={{ fontSize: 11, color: "var(--muted)" }}>/ סף {threshold.toFixed(2)}%</span>
      </div>
      <div style={{ height: 8, borderRadius: 4, background: "var(--border)", overflow: "hidden" }}>
        <div style={{
          height: "100%", borderRadius: 4,
          width: `${fill * 100}%`,
          background: fill >= 1 ? color : "var(--accent-bright)",
          transition: "width 0.3s ease",
        }} />
      </div>
    </div>
  );
}

// ─── Signal Confidence Bar ────────────────────────────────────────────────────

function SignalConfBar({
  confidence, rec, threshold,
}: {
  confidence: number | null; rec: string; threshold: number;
}) {
  if (confidence == null) return <div style={{ color: "var(--muted)", fontSize: 12 }}>ממתין לסיגנל…</div>;
  const pct = Math.round(confidence * 100);
  const thPct = Math.round(threshold * 100);
  const color = rec === "Up" ? "var(--up)" : rec === "Down" ? "var(--down)" : "var(--muted)";
  return (
    <div style={{ padding: "8px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 13, color: "var(--muted)" }}>סיגנל:</span>
        <strong style={{ fontSize: 18, color }}>
          {rec === "Up" ? "⬆ Up" : rec === "Down" ? "⬇ Down" : "≡ ניטרלי"}
        </strong>
        <strong style={{ fontSize: 16, color, fontVariantNumeric: "tabular-nums" }}>{pct}%</strong>
        <span style={{ fontSize: 11, color: "var(--muted)" }}>/ סף {thPct}%</span>
      </div>
      <div style={{ height: 8, borderRadius: 4, background: "var(--border)", overflow: "hidden", position: "relative" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 4,
          background: pct >= thPct ? color : "var(--accent-bright)",
          transition: "width 0.3s ease",
        }} />
        {/* threshold marker */}
        <div style={{
          position: "absolute", top: 0, bottom: 0, left: `${thPct}%`,
          width: 2, background: "rgba(255,255,255,0.4)",
        }} />
      </div>
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

export default function TriggerTrader() {
  const [state, setState] = useState<TriggerState | null>(null);
  const [cfg, setCfg] = useState<TriggerConfig>(DEFAULT_CONFIG);
  const [saving, setSaving] = useState(false);
  const [activating, setActivating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [shareCopied, setShareCopied] = useState(false);
  const [expandedLog, setExpandedLog] = useState<number | null>(null);
  const [autoSaveStatus, setAutoSaveStatus] = useState<"idle" | "saving" | "saved">("idle");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // הסשן צריך לדעת אם cfg הגיע מהשרת (אחרי טעינה ראשונה) — רק אז מותר לבצע auto-save.
  const initialSyncDoneRef = useRef(false);

  // ── Polling ──────────────────────────────────────────────────
  const poll = useCallback(async () => {
    try {
      const s = await api<TriggerState>("/api/trigger/state");
      setState(s);
      // Sync local config from server on first load
      setCfg(prev => {
        if (prev === DEFAULT_CONFIG) {
          initialSyncDoneRef.current = true;
          return s.config;
        }
        return prev;
      });
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "שגיאת חיבור");
    }
  }, []);

  useEffect(() => {
    poll();
    pollRef.current = setInterval(() => {
      if (!isPageHidden()) poll();
    }, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [poll]);

  /**
   * שמירה אוטומטית של ה-cfg לדיסק (debounced ~600ms) — כדי שכל שינוי שהמשתמש עושה
   * ב"מסחר מהיר" ישרוד יציאה והפעלה מחדש של האפליקציה גם בלי "שמור" ידני.
   */
  useEffect(() => {
    if (!initialSyncDoneRef.current) return;
    setAutoSaveStatus("saving");
    const handle = window.setTimeout(async () => {
      try {
        await api("/api/trigger/config", {
          method: "POST", body: JSON.stringify(cfg),
        });
        setAutoSaveStatus("saved");
        window.setTimeout(() => setAutoSaveStatus((s) => (s === "saved" ? "idle" : s)), 1200);
      } catch (e) {
        setAutoSaveStatus("idle");
        setErr(e instanceof Error ? e.message : "שגיאה בשמירה");
      }
    }, 600);
    return () => window.clearTimeout(handle);
  }, [cfg]);

  // ── Actions ───────────────────────────────────────────────────
  const saveAndActivate = useCallback(async () => {
    setActivating(true); setErr(null);
    try {
      await api("/api/trigger/config", {
        method: "POST", body: JSON.stringify(cfg),
      });
      await api("/api/trigger/activate", { method: "POST" });
      await poll();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "שגיאה");
    } finally { setActivating(false); }
  }, [cfg, poll]);

  const deactivate = useCallback(async () => {
    setSaving(true);
    try {
      await api("/api/trigger/deactivate", { method: "POST" });
      await poll();
    } finally { setSaving(false); }
  }, [poll]);

  const saveConfig = useCallback(async () => {
    setSaving(true); setErr(null);
    try {
      await api("/api/trigger/config", {
        method: "POST", body: JSON.stringify(cfg),
      });
      await poll();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "שגיאה");
    } finally { setSaving(false); }
  }, [cfg, poll]);

  const clearEvents = useCallback(async () => {
    await api("/api/trigger/events", { method: "DELETE" });
    await poll();
  }, [poll]);

  const rearmDcaPulse = useCallback(async () => {
    setErr(null);
    try {
      await api("/api/trigger/rearm", { method: "POST" });
      await poll();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "שגיאה");
    }
  }, [poll]);

  const copyShareBundle = useCallback(async () => {
    setShareCopied(false);
    setErr(null);
    try {
      const r = await api<{ ok: boolean; text: string }>("/api/trigger/share-bundle");
      const text = r?.text ?? "";
      if (!text) {
        setErr("אין טקסט לחבילת שיתוף");
        return;
      }

      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // fallback ל-browser ישן / בלי הרשאות clipboard
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        ta.style.top = "-9999px";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }

      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 1600);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "שגיאה בהעתקה");
    }
  }, []);

  const isActive = state?.active ?? false;
  const selectedMode = cfg.mode;

  // ── Render ────────────────────────────────────────────────────
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--s-4)" }}>

      {/* ── Title ──────────────────────────────────────────────── */}
      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-3)", flexWrap: "wrap" }}>
        <h2 style={{ margin: 0, fontSize: "1.15rem", fontFamily: "var(--font-display)", fontWeight: 700 }}>
          ⚡ מסחר אוטומטי מהיר
        </h2>
        <span style={{
          fontSize: 11, fontWeight: 700, padding: "3px 10px", borderRadius: 20,
          background: isActive ? "var(--up-muted)" : "rgba(120,130,150,0.15)",
          color: isActive ? "var(--up)" : "var(--muted)",
          border: `1px solid ${isActive ? "var(--up)" : "var(--border)"}`,
        }}>
          {isActive ? "● פעיל" : "○ כבוי"}
        </span>
        <span
          title="ההגדרות נשמרות אוטומטית לדיסק ומשוחזרות בהפעלה הבאה"
          style={{
            fontSize: 11, fontWeight: 600, padding: "3px 10px", borderRadius: 20,
            color: autoSaveStatus === "saving" ? "var(--accent-bright)" : "var(--muted)",
            background: autoSaveStatus === "saving" ? "var(--accent-muted)" : "rgba(120,130,150,0.10)",
            border: "1px solid var(--border)",
            transition: "color 0.18s, background 0.18s",
          }}
        >
          {autoSaveStatus === "saving"
            ? "💾 שומר…"
            : autoSaveStatus === "saved"
              ? "✓ נשמר"
              : "💾 שמירה אוטומטית"}
        </span>
      </div>

      {err && (
        <div style={{
          background: "var(--down-muted)", border: "1px solid var(--down)",
          borderRadius: "var(--radius-sm)", padding: "8px 14px", color: "var(--down)", fontSize: 13,
        }}>{err}</div>
      )}

      {/* ── Window selector ────────────────────────────────────── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", marginBottom: "var(--s-3)", flexWrap: "wrap" }}>
          <SectionTitle as="h3">0 · ⏱ חלון מסחר Polymarket</SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>בחר את משך הסבב שבו הבוט נכנס וסוגר</span>
        </div>
        <div style={{ display: "flex", gap: "var(--s-2)" }}>
          {([["5m", "⏱ 5 דקות", "חלון קצר — תנועות מהירות, רווח מהיר יותר"],
             ["15m", "⏳ 15 דקות", "חלון ארוך — יותר זמן להמתין לTP"]] as const).map(([val, label, desc]) => (
            <button
              key={val}
              type="button"
              onClick={() => setCfg(p => ({ ...p, btc_window: val }))}
              style={{
                flex: 1, padding: "var(--s-3) var(--s-2)", borderRadius: "var(--radius-sm)", cursor: "pointer",
                border: `2px solid ${cfg.btc_window === val ? "var(--accent-bright)" : "var(--border)"}`,
                background: cfg.btc_window === val ? "var(--accent-muted)" : "var(--card)",
                textAlign: "center",
              }}
            >
              <div style={{ fontWeight: 800, fontSize: 18, color: cfg.btc_window === val ? "var(--accent-bright)" : "var(--text)" }}>
                {label}
              </div>
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: "var(--s-1)", lineHeight: 1.4 }}>{desc}</div>
            </button>
          ))}
        </div>
      </Card>

      {/* ── Mode selector ──────────────────────────────────────── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", marginBottom: "var(--s-3)", flexWrap: "wrap" }}>
          <SectionTitle as="h3">1 · בחר מצב טריגר</SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>מה יגרום לבוט להיכנס לעסקה</span>
        </div>
        <div style={{ display: "flex", gap: "var(--s-2)" }}>
          {MODES.map(m => (
            <ModeCard
              key={m.id}
              mode={m}
              selected={selectedMode === m.id}
              onClick={() => setCfg(p => ({ ...p, mode: m.id }))}
            />
          ))}
        </div>
      </Card>

      {/* ── Mode-specific settings ────────────────────────────── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", marginBottom: "var(--s-4)", flexWrap: "wrap" }}>
          <SectionTitle as="h3">
            2 · הגדרות{" "}
            {selectedMode === "momentum" ? "🚀 מומנטום"
              : selectedMode === "signal" ? "📡 סיגנל"
              : "🔄 DCA פולס"}
          </SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>כוונון הטריגר שבחרת</span>
        </div>

        {selectedMode === "momentum" && (
          <>
            <SliderField
              label="שינוי מינימלי BTC (%)"
              value={cfg.momentum_pct} min={0.05} max={1.0} step={0.05} unit="%"
              onChange={v => setCfg(p => ({ ...p, momentum_pct: v }))}
            />
            <SliderField
              label="חלון זמן (שניות)"
              value={cfg.momentum_window_sec} min={30} max={180} step={15} unit="ש׳"
              onChange={v => setCfg(p => ({ ...p, momentum_window_sec: v }))}
            />
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>כיוון כניסה:</div>
            <DirectionPicker
              value={cfg.momentum_direction}
              onChange={v => setCfg(p => ({ ...p, momentum_direction: v as "auto" | "Up" | "Down" }))}
            />
            <div style={{
              background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", padding: "var(--s-3)",
              fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.6,
            }}>
              <strong>איך עובד:</strong> הבוט בודק כל 2 שניות אם BTC זז{" "}
              <strong>{cfg.momentum_pct}%</strong> תוך <strong>{cfg.momentum_window_sec} שניות</strong>.
              אם כן — נכנס {cfg.momentum_direction === "auto" ? "בכיוון התנועה" : `ל-${cfg.momentum_direction}`}.
            </div>
          </>
        )}

        {selectedMode === "signal" && (
          <>
            <SliderField
              label="מינימום ביטחון סיגנל"
              value={Math.round(cfg.signal_confidence * 100)} min={55} max={90} step={1} unit="%"
              onChange={v => setCfg(p => ({ ...p, signal_confidence: v / 100 }))}
            />
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>כיוון (auto = לפי המלצת הסיגנל):</div>
            <DirectionPicker
              value={cfg.signal_direction}
              onChange={v => setCfg(p => ({ ...p, signal_direction: v as "auto" | "Up" | "Down" }))}
            />
            <div style={{
              background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", padding: "var(--s-3)",
              fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.6,
            }}>
              <strong>איך עובד:</strong> כל ~2 שניות הבוט בודק את הסיגנלים.
              אם ביטחון ≥ <strong>{Math.round(cfg.signal_confidence * 100)}%</strong> — נכנס{" "}
              {cfg.signal_direction === "auto" ? "לפי כיוון המלצה" : `ל-${cfg.signal_direction}`}.
            </div>
          </>
        )}

        {selectedMode === "dca_pulse" && (
          <>
            <SliderField
              label="מספר סלייסים"
              value={cfg.dca_pulse_slices} min={2} max={6} step={1} unit=" סלייסים"
              onChange={v => setCfg(p => ({ ...p, dca_pulse_slices: v }))}
            />
            <SliderField
              label="מרווח בין סלייסים (שניות)"
              value={cfg.dca_pulse_interval_sec} min={10} max={60} step={5} unit="ש׳"
              onChange={v => setCfg(p => ({ ...p, dca_pulse_interval_sec: v }))}
            />
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>כיוון:</div>
            <DirectionPicker
              value={cfg.dca_pulse_direction} showAuto={true}
              onChange={v => setCfg(p => ({ ...p, dca_pulse_direction: v as "auto" | "Up" | "Down" }))}
            />
            {/* DCA Sizing selector */}
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>אופן חלוקת ההשקעה:</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {([
                  ["equal", "⚖ שווה", `${(cfg.investment_usd / cfg.dca_pulse_slices).toFixed(2)}$ לכל סלייס`, "חלוקה קבועה"],
                  ["pyramid", "📈 פירמידה", "יותר כסף בסלייסים מאוחרים", "ממוצע עולה כשהמחיר יורד"],
                  ["fixed_contracts", "🔢 חוזים קבועים", `${Math.max(1, Math.floor(cfg.investment_usd / (cfg.entry_price_cents / 100) / cfg.dca_pulse_slices))} חוזים לכל סלייס`, "$ משתנה לפי מחיר ברגע הכניסה"],
                ] as const).map(([val, label, sub, hint]) => (
                  <div
                    key={val}
                    onClick={() => setCfg(p => ({ ...p, dca_sizing: val }))}
                    style={{
                      display: "flex", alignItems: "center", gap: 10,
                      padding: "var(--s-2) var(--s-3)", borderRadius: "var(--radius-sm)", cursor: "pointer",
                      border: `1px solid ${cfg.dca_sizing === val ? "var(--accent-bright)" : "var(--border)"}`,
                      background: cfg.dca_sizing === val ? "var(--accent-muted)" : "var(--bg-elevated)",
                    }}
                  >
                    <div style={{ fontSize: 16 }}>{label.split(" ")[0]}</div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: cfg.dca_sizing === val ? "var(--accent-bright)" : "var(--text)" }}>
                        {label.split(" ").slice(1).join(" ")}
                      </div>
                      <div style={{ fontSize: 11, color: "var(--muted)" }}>{sub} — {hint}</div>
                    </div>
                    {cfg.dca_sizing === val && (
                      <div style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--accent-bright)" }} />
                    )}
                  </div>
                ))}
              </div>

              {/* ויזואליזציה של חלוקה לפי sizing */}
              {cfg.dca_sizing !== "fixed_contracts" && (() => {
                const n = cfg.dca_pulse_slices;
                const weights = cfg.dca_sizing === "pyramid"
                  ? Array.from({ length: n }, (_, j) => j + 1)   // 1,2,3,...,N
                  : Array.from({ length: n }, () => 1);
                const totalW = weights.reduce((a, b) => a + b, 0);
                const maxW = Math.max(...weights);
                const BAR_MAX_H = 40; // px — גובה הבר הגבוה ביותר
                return (
                  <div style={{ marginTop: 10 }}>
                    <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 6 }}>
                      חלוקה לפי סלייסים:
                    </div>
                    <div style={{ display: "flex", gap: 4, alignItems: "flex-end" }}>
                      {weights.map((w, i) => {
                        const usd = cfg.investment_usd * w / totalW;
                        const barH = Math.max(6, Math.round((w / maxW) * BAR_MAX_H));
                        return (
                          <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 3 }}>
                            <div style={{ fontSize: 9, color: "var(--accent-bright)", fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
                              {usd.toFixed(1)}$
                            </div>
                            <div style={{
                              width: "100%", borderRadius: "3px 3px 0 0",
                              height: barH,
                              background: `linear-gradient(to top, var(--accent-bright), var(--up))`,
                              opacity: 0.55 + (w / maxW) * 0.45,
                            }} />
                            <div style={{ fontSize: 9, color: "var(--muted)" }}>{i + 1}</div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })()}
            </div>

            <SliderField
              label="שינוי מינימלי בין סלייסים (%)"
              value={cfg.dca_min_step_pct} min={0} max={50} step={1} unit="%"
              onChange={v => setCfg(p => ({ ...p, dca_min_step_pct: v }))}
            />

            {(() => {
              const sliceUsd = cfg.investment_usd / cfg.dca_pulse_slices;
              const contractsPerSlice = Math.max(5, Math.floor(sliceUsd / (cfg.entry_price_cents / 100)));
              const actualCost = contractsPerSlice * (cfg.entry_price_cents / 100);
              const tpProfit = actualCost * (cfg.take_profit_pct / 100);
              const totalSec = Math.round((cfg.dca_pulse_slices - 1) * cfg.dca_pulse_interval_sec);
              return (
                <div style={{
                  background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", padding: "var(--s-3)",
                  fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.8,
                }}>
                  <div>
                    <strong>כל סלייס:</strong>{" "}
                    <strong style={{ color: "var(--text)" }}>{contractsPerSlice} חוזים</strong>{" "}
                    × <strong style={{ color: "var(--text)" }}>{cfg.entry_price_cents}¢</strong>{" "}
                    = <strong style={{ color: "var(--accent-bright)" }}>{actualCost.toFixed(2)}$</strong>
                  </div>
                  <div>
                    <strong>רווח בTP +{cfg.take_profit_pct}%:</strong>{" "}
                    <strong style={{ color: "var(--up)" }}>+{tpProfit.toFixed(2)}$ לסלייס</strong>{" "}
                    (<strong style={{ color: "var(--up)" }}>+{(tpProfit * cfg.dca_pulse_slices).toFixed(2)}$</strong> סה"כ)
                  </div>
                  <div>
                    <strong>לוח זמנים:</strong>{" "}
                    {cfg.dca_pulse_slices} סלייסים × {cfg.dca_pulse_interval_sec}ש׳ מרווח = {totalSec}ש׳ סה"כ
                  </div>
                  {cfg.dca_min_step_pct > 0 && (
                    <div style={{ color: "var(--accent-bright)", marginTop: 2 }}>
                      📉 סלייס הבא רק אם המחיר ירד לפחות {cfg.dca_min_step_pct}% מהכניסה הקודמת
                    </div>
                  )}
                  {cfg.dca_pulse_direction === "auto" && (
                    <div style={{ color: "var(--accent-bright)", marginTop: 2, fontWeight: 600 }}>
                      🤖 אוטו — הסיגנל יחליט Up/Down לפני כל סלייס
                    </div>
                  )}
                  <div style={{ color: "var(--muted)", marginTop: 2 }}>
                    ⚡ חד-פעמי — כיבוי אוטומטי לאחר כל הסלייסים
                  </div>
                </div>
              );
            })()}
          </>
        )}
      </Card>

      {/* ── Common settings ────────────────────────────────────── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", marginBottom: "var(--s-4)", flexWrap: "wrap" }}>
          <SectionTitle as="h3">3 · הגדרות כניסה ויציאה</SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>כמה להשקיע, באיזה מחיר, ומתי לקחת רווח</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 20px" }}>
          <SliderField
            label="סכום השקעה ($)"
            value={cfg.investment_usd} min={1} max={50} step={0.5} unit="$"
            onChange={v => setCfg(p => ({ ...p, investment_usd: v }))}
          />
          <SliderField
            label="מחיר כניסה (¢)"
            value={cfg.entry_price_cents} min={5} max={70} step={1} unit="¢"
            onChange={v => setCfg(p => ({ ...p, entry_price_cents: v }))}
          />
          <SliderField
            label="יעד רווח (TP %)"
            value={cfg.take_profit_pct} min={5} max={60} step={1} unit="%"
            onChange={v => setCfg(p => ({ ...p, take_profit_pct: v }))}
          />
          <SliderField
            label="המתנה בין טריגרים (שניות)"
            value={cfg.cooldown_sec} min={15} max={300} step={15} unit="ש׳"
            onChange={v => setCfg(p => ({ ...p, cooldown_sec: v }))}
          />
        </div>

        {/* Summary pill */}
        <div style={{
          display: "flex", gap: "var(--s-2)", flexWrap: "wrap", marginTop: "var(--s-1)", marginBottom: "var(--s-4)",
        }}>
          {[
            ["השקעה", `$${cfg.investment_usd}`],
            ["מחיר", `${cfg.entry_price_cents}¢`],
            ["TP", `${cfg.take_profit_pct}%`],
            ["חוזים ~", `${Math.max(5, Math.floor(cfg.investment_usd / (cfg.entry_price_cents / 100)))}`],
          ].map(([k, v]) => (
            <div key={k} style={{
              background: "var(--bg-elevated)", border: "1px solid var(--border)",
              borderRadius: "var(--radius-sm)", padding: "3px 10px", fontSize: 12,
            }}>
              <span style={{ color: "var(--muted)" }}>{k}: </span>
              <strong style={{ color: "var(--accent-bright)" }}>{v}</strong>
            </div>
          ))}
        </div>

        {/* ── Advanced (power-user) entry/exit guards ────────────── */}
        <Collapsible
          title="הגדרות מתקדמות"
          subtitle="הגבלות בטיחות — כמה טריגרים לחלון, זמן מינימלי שנותר, וסחיפת מחיר"
          icon="⚙"
          style={{ background: "var(--bg-elevated)", boxShadow: "none" }}
        >
          <SliderField
            label="מקסימום טריגרים לחלון"
            value={cfg.max_triggers_per_window} min={1} max={5} step={1}
            onChange={v => setCfg(p => ({ ...p, max_triggers_per_window: v }))}
          />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 20px" }}>
            <SliderField
              label="לא להיכנס אם נשאר פחות מ (שניות)"
              value={cfg.min_seconds_remaining} min={30} max={240} step={15} unit="ש׳"
              onChange={v => setCfg(p => ({ ...p, min_seconds_remaining: v }))}
            />
            <SliderField
              label={`דלג אם החוזה כבר עלה ב-% ${cfg.contract_max_drift_pct === 0 ? "(כבוי)" : ""}`}
              value={cfg.contract_max_drift_pct} min={0} max={80} step={5} unit="%"
              onChange={v => setCfg(p => ({ ...p, contract_max_drift_pct: v }))}
            />
          </div>
        </Collapsible>
      </Card>

      {/* ── Activate / Deactivate ─────────────────────────────── */}
      <div style={{ display: "flex", gap: "var(--s-2)" }}>
        {!isActive ? (
          <>
            <button
              type="button"
              onClick={saveAndActivate}
              disabled={activating || cfg.mode === "off"}
              style={{
                flex: 1, padding: "14px", borderRadius: "var(--radius-lg)", border: "none",
                background: cfg.mode === "off" ? "var(--bg-elevated)" : "var(--up)",
                color: "#fff", fontWeight: 800, fontSize: 15, cursor: "pointer",
                opacity: activating || cfg.mode === "off" ? 0.6 : 1,
                letterSpacing: "0.02em",
              }}
            >
              {activating ? "מפעיל…" : "⚡ הפעל טריגר"}
            </button>
            <button
              type="button"
              onClick={saveConfig}
              disabled={saving}
              style={{
                padding: "14px 20px", borderRadius: "var(--radius-lg)",
                border: "1px solid var(--border-strong)",
                background: "var(--bg-elevated)", color: "var(--text-secondary)",
                fontWeight: 600, fontSize: 13, cursor: "pointer",
                opacity: saving ? 0.6 : 1,
              }}
            >
              {saving ? "שומר…" : "שמור"}
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={deactivate}
            disabled={saving}
            style={{
              flex: 1, padding: "14px", borderRadius: "var(--radius-lg)", border: "none",
              background: "var(--down)", color: "#fff",
              fontWeight: 800, fontSize: 15, cursor: "pointer",
              opacity: saving ? 0.6 : 1,
            }}
          >
            {saving ? "מכבה…" : "■ כבה טריגר"}
          </button>
        )}
      </div>

      {/* ── Auto-start toggle ────────────────────────────────── */}
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--s-3)",
        padding: "10px 14px", borderRadius: "var(--radius-sm)",
        background: cfg.auto_start ? "var(--accent-muted)" : "var(--bg-elevated)",
        border: `1px solid ${cfg.auto_start ? "var(--accent-bright)" : "var(--border)"}`,
        cursor: "pointer",
      }}
        onClick={() => setCfg(p => ({ ...p, auto_start: !p.auto_start }))}
      >
        <div style={{
          width: 36, height: 20, borderRadius: 10, position: "relative",
          background: cfg.auto_start ? "var(--accent-bright)" : "var(--border)",
          transition: "background 0.2s",
          flexShrink: 0,
        }}>
          <div style={{
            position: "absolute", top: 2,
            left: cfg.auto_start ? 18 : 2,
            width: 16, height: 16, borderRadius: "50%",
            background: "#fff", transition: "left 0.2s",
            boxShadow: "0 1px 3px rgba(0,0,0,0.3)",
          }} />
        </div>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: cfg.auto_start ? "var(--accent-bright)" : "var(--text)" }}>
            הפעל אוטומטית בכל הפעלה
          </div>
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 1 }}>
            {cfg.auto_start
              ? "✅ הטריגר יופעל אוטומטית כשהמנוע עולה — ללא לחיצה"
              : "כבוי — צריך לחיצה ידנית בכל הפעלה"}
          </div>
        </div>
      </div>

      {/* ── Live status card ──────────────────────────────────── */}
      {state && (
        <Card padding="md">
          <div style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "var(--s-3)",
            marginBottom: "var(--s-3)",
            flexWrap: "wrap",
          }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", flexWrap: "wrap" }}>
              <SectionTitle as="h3">📊 מצב בזמן אמת</SectionTitle>
              <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>מה הבוט רואה ועושה עכשיו</span>
            </div>
            {state.mode === "dca_pulse" && (
              <Button
                variant="ghost"
                onClick={rearmDcaPulse}
                title="משחרר נעילת חלון ומאפשר להתחיל DCA Pulse מחדש מיד"
                style={{ fontSize: 11, fontWeight: 800, padding: "6px 10px" }}
              >
                הרץ שוב עכשיו
              </Button>
            )}
          </div>
          <div style={{
            fontSize: 13, fontFamily: "monospace",
            color: isActive ? "var(--text)" : "var(--muted)",
            marginBottom: "var(--s-3)", minHeight: 20,
          }}>
            {state.status}
          </div>

          {/* Momentum gauge */}
          {isActive && state.mode === "momentum" && (
            <MomentumGauge
              changePct={state.current_btc_change_pct}
              threshold={state.config.momentum_pct}
            />
          )}

          {/* Signal confidence bar */}
          {isActive && state.mode === "signal" && (
            <SignalConfBar
              confidence={state.current_signal_confidence}
              rec={state.current_signal_rec}
              threshold={state.config.signal_confidence}
            />
          )}

          {/* Stats row */}
          <div style={{
            display: "flex", gap: 12, marginTop: 10, flexWrap: "wrap",
          }}>
            {[
              ["טריגרים בחלון", String(state.triggers_this_window)],
              ["אחרון", ts2time(state.last_trigger_ts)],
              ["המתנה", state.cooldown_remaining != null && state.cooldown_remaining > 0
                ? `${state.cooldown_remaining}ש׳` : "—"],
            ].map(([k, v]) => (
              <div key={k} style={{ fontSize: 12 }}>
                <span style={{ color: "var(--muted)" }}>{k}: </span>
                <strong style={{ fontVariantNumeric: "tabular-nums" }}>{v}</strong>
              </div>
            ))}
          </div>

          {/* Live feed */}
          {state.status_log && state.status_log.length > 0 && (
            <div style={{ marginTop: "var(--s-3)" }}>
              <div style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, marginBottom: 6 }}>
                📡 פיד חי
              </div>
              <div style={{
                maxHeight: 160, overflowY: "auto",
                background: "var(--bg-elevated)",
                borderRadius: "var(--radius-sm)", padding: "6px 10px",
                display: "flex", flexDirection: "column-reverse",
                gap: 2,
              }}>
                {[...state.status_log].reverse().map((entry, i) => {
                  const isWaiting = entry.msg.startsWith("⏳");
                  const isOk = entry.msg.startsWith("✅");
                  const isWarn = entry.msg.startsWith("⏰") || entry.msg.startsWith("⛔");
                  const color = isOk ? "var(--up)"
                    : isWarn ? "var(--down)"
                    : isWaiting ? "var(--accent-bright)"
                    : "var(--text-secondary)";
                  return (
                    <div key={i} style={{
                      display: "flex", gap: 8, alignItems: "baseline",
                      padding: "2px 0",
                      borderBottom: i < state.status_log.length - 1 ? "1px solid rgba(255,255,255,0.04)" : "none",
                    }}>
                      <span style={{
                        fontSize: 10, color: "var(--muted)",
                        fontVariantNumeric: "tabular-nums", flexShrink: 0,
                      }}>
                        {ts2time(entry.ts)}
                      </span>
                      <span style={{ fontSize: 12, color, lineHeight: 1.4 }}>{entry.msg}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </Card>
      )}

      {/* ── Event log ────────────────────────────────────────── */}
      {state && state.events.length > 0 && (
        <Card padding="md">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "var(--s-3)", marginBottom: "var(--s-3)", flexWrap: "wrap" }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", flexWrap: "wrap" }}>
              <SectionTitle as="h3">🧾 לוג אירועים</SectionTitle>
              <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>כל כניסה, דילוג או שגיאה של הטריגר</span>
            </div>
            <div style={{ display: "flex", gap: "var(--s-2)", alignItems: "center" }}>
              <Button
                variant="ghost"
                onClick={copyShareBundle}
                style={{ fontSize: 11, fontWeight: 700, padding: "4px 10px", color: shareCopied ? "var(--up)" : undefined }}
              >
                {shareCopied ? "הועתק" : "העתק שיתוף"}
              </Button>
              <Button
                variant="ghost"
                onClick={clearEvents}
                style={{ fontSize: 11, padding: "4px 10px" }}
              >
                נקה
              </Button>
            </div>
          </div>
          <Collapsible
            title="יומן אירועים"
            subtitle={`${state.events.length} רשומות · לחיצה על שורה פותחת פירוט מלא`}
            icon="📜"
            defaultOpen={state.events.length <= 12}
            style={{ background: "var(--bg-elevated)", boxShadow: "none" }}
          >
          <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
            {[...state.events].reverse().map((ev, i) => {
              const isExpanded = expandedLog === i;
              return (
                <div key={i}>
                  <div
                    onClick={() => setExpandedLog(isExpanded ? null : i)}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "16px 60px 40px 52px 1fr 14px",
                      gap: 8,
                      padding: "6px 4px",
                      borderBottom: isExpanded ? "none" : "1px solid var(--border)",
                      fontSize: 12,
                      alignItems: "start",
                      cursor: "pointer",
                      borderRadius: isExpanded ? "var(--radius-sm) var(--radius-sm) 0 0" : "var(--radius-sm)",
                      background: isExpanded ? "var(--card)" : "transparent",
                    }}
                  >
                    <span>{eventIcon(ev.event_type)}</span>
                    <span style={{ color: "var(--muted)", fontSize: 11, fontVariantNumeric: "tabular-nums" }}>
                      {ts2time(ev.ts)}
                    </span>
                    <span style={{
                      fontWeight: 700,
                      color: ev.side === "Up" ? "var(--up)" : ev.side === "Down" ? "var(--down)" : "var(--muted)",
                    }}>
                      {ev.side ?? "—"}
                    </span>
                    <span style={{ color: "var(--muted)", fontSize: 11, fontVariantNumeric: "tabular-nums" }}>
                      {ev.contract_ask != null ? `${Math.round(ev.contract_ask * 100)}¢` : "—"}
                    </span>
                    <span style={{ color: eventColor(ev.event_type), lineHeight: 1.4 }}>
                      {ev.note}
                    </span>
                    <span style={{ color: "var(--muted)", fontSize: 10 }}>{isExpanded ? "▲" : "▼"}</span>
                  </div>

                  {/* פירוט מלא בלחיצה */}
                  {isExpanded && (
                    <div style={{
                      background: "var(--card)",
                      border: "1px solid var(--border)",
                      borderTop: "none",
                      borderRadius: "0 0 var(--radius-sm) var(--radius-sm)",
                      padding: "var(--s-3)",
                      marginBottom: "var(--s-1)",
                      fontSize: 12,
                    }}>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 20px" }}>
                        {[
                          ["סוג אירוע", ev.event_type],
                          ["מצב טריגר", ev.trigger_mode],
                          ["צד", ev.side ?? "—"],
                          ["cap (מחיר מקס)", ev.price != null ? `${(ev.price * 100).toFixed(1)}¢` : "—"],
                          ["ask בפועל", ev.contract_ask != null ? `${(ev.contract_ask * 100).toFixed(1)}¢` : "—"],
                          ["חוזים", ev.contracts != null ? String(ev.contracts) : "—"],
                          ["שעה", israelDateTime(ev.ts)],
                        ].map(([k, v]) => (
                          <div key={k}>
                            <span style={{ color: "var(--muted)" }}>{k}: </span>
                            <strong style={{ color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>{v}</strong>
                          </div>
                        ))}
                      </div>
                      <div style={{ marginTop: 8, color: "var(--text-secondary)", lineHeight: 1.6, wordBreak: "break-word" }}>
                        <span style={{ color: "var(--muted)" }}>הערה: </span>{ev.note}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          </Collapsible>
        </Card>
      )}

      {state && state.events.length === 0 && isActive && (
        <div style={{ textAlign: "center", color: "var(--muted)", fontSize: 13, padding: "16px 0" }}>
          הטריגר פעיל — ממתין לתנאים…
        </div>
      )}

    </div>
  );
}
