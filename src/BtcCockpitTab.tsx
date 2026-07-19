import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isPageHidden } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { Button } from "./ui/Button";
import { Collapsible } from "./ui/Collapsible";
import { israelDateHM } from "./timeFormat";

/**
 * BtcCockpitTab — "קוקפיט מסחר ידני" for the owner's REAL Binance USDⓈ-M Futures account.
 *
 * This is a RESPONSIBLE MANUAL-TRADING COCKPIT, not an auto-bot and not an edge claim.
 * The owner makes EVERY decision. The tool's job is SAFETY ENFORCEMENT + transparency:
 *   - every order routes through risk_engine.gate_order (the only approve path),
 *   - a stop-loss is ALWAYS attached on the exchange and verified,
 *   - daily/global loss caps are enforced,
 *   - the execute button is DISABLED until the preview is approved and every check is green.
 *
 * Backend contract:
 *   GET  /api/binance/state    -> account header + position + caps + live/testnet
 *   GET  /api/binance/trades   -> recent sidecar-ledger rows (net P&L)
 *   POST /api/binance/preview  -> qty/costs/net target/liquidation + itemised checks + approved
 *   POST /api/binance/trade    -> execute (gate + atomic stop); 409 = naked-position guard
 *   POST /api/binance/close    -> flatten + cancel resting orders + log exit
 */

const STATE_TIMEOUT_MS = 20_000;
const STATE_POLL_MS = 15_000; // slow loop — this is a manual cockpit, not a hot loop

// ── backend types ────────────────────────────────────────────────────────────
type LiveStatus = {
  live_enabled: boolean;
  binance_live_flag: boolean;
  testnet: boolean;
  has_keys: boolean;
  reason_blocked: string | null;
};
type Position = {
  symbol?: string;
  qty?: number;
  entry_price?: number;
  side?: "long" | "short" | "flat" | string;
  leverage?: number;
  unrealized_pnl?: number;
};
type Caps = { allow_new: boolean; flatten: boolean; halt: boolean; reason: string };
type BinanceState = {
  live: LiveStatus;
  symbol: string;
  testnet: boolean;
  balance_usdt: number | null;
  position: Position | null;
  liquidation_price: number | null;
  unrealized_pnl: number | null;
  caps: Caps | null;
  error: string | null;
};

type Check = { name: string; ok: boolean; reason: string };
type Preview = {
  approved: boolean;
  symbol?: string;
  side?: string;
  qty?: number;
  notional?: number;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
  fee_est?: number;
  slippage_est?: number;
  total_cost?: number;
  liquidation_price?: number | null;
  net_target?: number | null;
  net_if_stopped?: number | null;
  risk_dollars?: number;
  leverage?: number | null;
  rr?: number | null;
  checks?: Check[];
  reason?: string;
};

type TradeResult = {
  ok?: boolean;
  approved?: boolean;
  placed_order?: boolean;
  reason?: string;
  live_enabled?: boolean;
  testnet?: boolean;
  note?: string;
  naked_position_guard?: boolean;
  qty?: number;
  entry_price?: number;
  stop_price?: number;
  target_price?: number;
  stop_verified?: boolean;
};

type LedgerRow = {
  id: number;
  ts: number;
  mode: string;
  event: string; // 'entry' | 'exit' | 'fault'
  symbol: string | null;
  side: string | null;
  qty: number | null;
  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  exit_price: number | null;
  fee: number | null;
  realized_pnl: number | null;
  leverage: number | null;
  risk_dollars: number | null;
  stop_verified: number | null;
  context_json: string | null;
};
type LedgerResponse = { rows: LedgerRow[]; count: number };

// ── formatters ───────────────────────────────────────────────────────────────
function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toFixed(digits)}`;
}
function fmtPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}
function fmtNum(v: number | null | undefined, digits = 4): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}
function fmtTime(ts: number | null | undefined): string {
  if (!ts || !Number.isFinite(ts)) return "—";
  // sidecar ledger ts is in SECONDS (time.time()) — render in Israel time (DD/MM HH:MM)
  return israelDateHM(ts);
}
function pnlColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "var(--muted)";
  return v >= 0 ? "var(--up)" : "var(--down)";
}
function sideColor(side: string | null | undefined): string {
  const s = String(side || "").toLowerCase();
  if (s === "long" || s === "buy") return "var(--up)";
  if (s === "short" || s === "sell") return "var(--down)";
  return "var(--text)";
}
function sideLabel(side: string | null | undefined): string {
  const s = String(side || "").toLowerCase();
  if (s === "long" || s === "buy") return "לונג";
  if (s === "short" || s === "sell") return "שורט";
  if (s === "flat") return "אין פוזיציה";
  return side ? String(side) : "—";
}

// human-readable Hebrew labels for the safety checks (keys come from binance_cockpit)
const CHECK_LABELS: Record<string, string> = {
  inputs: "קלט תקין (צד/כניסה/סטופ/הון)",
  stop_direction: "סטופ בצד המגן של הכניסה",
  risk_gate: "שער הסיכון (gate_order)",
  exchange_filters: "מסנני הבורסה נקראו",
  lot_step: "כמות עוגלה לפי צעד הלוט",
  min_notional: "מעל המינימום הנדרש לעסקה",
  liquidation_vs_stop: "הסטופ מופעל לפני הליקווידציה",
  internal: "תקינות פנימית",
};
function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? name;
}

// ── small UI atoms ───────────────────────────────────────────────────────────
function Bar({ label, value, max, danger, color }: {
  label: string; value: number; max: number; danger: boolean; color: string;
}) {
  const pct = Math.max(0, Math.min(100, (Math.abs(value) / (max || 1)) * 100));
  return (
    <div style={{ flex: "1 1 160px", minWidth: 150 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: "var(--s-1)" }}>
        <span style={{ color: "var(--muted)", fontWeight: 700 }}>{label}</span>
        <span style={{ color: danger ? "var(--down)" : color, fontWeight: 700 }}>{value.toFixed(2)}%</span>
      </div>
      <div style={{ height: 8, borderRadius: 999, background: "var(--bg-elevated)", overflow: "hidden" }}>
        <div style={{
          width: `${pct}%`, height: "100%", borderRadius: 999,
          background: danger ? "var(--down)" : color, transition: "width .3s",
        }} />
      </div>
      <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>סף: {max}%</div>
    </div>
  );
}

function Pill({ children, color, bg }: { children: React.ReactNode; color: string; bg: string }) {
  return (
    <span style={{
      fontSize: 11, fontWeight: 800, padding: "3px 9px", borderRadius: 999,
      color, background: bg, whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

export default function BtcCockpitTab() {
  const [state, setState] = useState<BinanceState | null>(null);
  const [stateErr, setStateErr] = useState<string | null>(null);
  const [ledger, setLedger] = useState<LedgerRow[]>([]);

  // manual trade form
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [side, setSide] = useState<"long" | "short">("long");
  const [riskDollars, setRiskDollars] = useState<string>("10"); // default SMALL
  const [entry, setEntry] = useState<string>("");
  const [stop, setStop] = useState<string>("");
  const [target, setTarget] = useState<string>("");
  const [leverage, setLeverage] = useState<string>("3");

  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  const [executing, setExecuting] = useState(false);
  const [tradeMsg, setTradeMsg] = useState<{ kind: "ok" | "err" | "warn"; text: string } | null>(null);
  const [closing, setClosing] = useState(false);

  // any change to the trade inputs invalidates a stale preview — the execute
  // button must never act on a preview that no longer matches the form.
  const invalidatePreview = useCallback(() => {
    setPreview(null);
    setPreviewErr(null);
  }, []);

  // ── state poll (slow loop) ─────────────────────────────────────────────────
  const refreshState = useCallback(async () => {
    try {
      const res = await api<BinanceState>(
        `/api/binance/state?symbol=${encodeURIComponent(symbol)}`,
        { timeoutMs: STATE_TIMEOUT_MS },
      );
      setState(res);
      setStateErr(res.error ?? null);
    } catch (e) {
      setStateErr(e instanceof Error ? e.message : String(e));
    }
  }, [symbol]);

  const refreshLedger = useCallback(async () => {
    try {
      const res = await api<LedgerResponse>("/api/binance/trades?limit=100", { timeoutMs: STATE_TIMEOUT_MS });
      setLedger(res.rows ?? []);
    } catch {
      // ledger failure must never break the cockpit
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshState(), refreshLedger()]);
  }, [refreshState, refreshLedger]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    const id = setInterval(() => { if (!isPageHidden()) void refreshAll(); }, STATE_POLL_MS);
    const onVisible = () => { if (!isPageHidden()) void refreshAll(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refreshAll]);

  // derived equity for the risk_pct conversion (risk $ -> % of equity)
  const equity = state?.balance_usdt ?? null;

  // ── preview ────────────────────────────────────────────────────────────────
  const previewSeq = useRef(0);
  const doPreview = useCallback(async () => {
    setTradeMsg(null);
    setPreviewErr(null);
    const enN = parseFloat(entry);
    const stN = parseFloat(stop);
    const tgN = target.trim() === "" ? null : parseFloat(target);
    const lev = parseInt(leverage, 10) || 3;
    const risk = parseFloat(riskDollars);

    if (!Number.isFinite(enN) || !Number.isFinite(stN) || !Number.isFinite(risk) || risk <= 0) {
      setPreviewErr("מלא מחיר כניסה, סטופ וסכום סיכון תקין לפני התצוגה המקדימה");
      setPreview(null);
      return;
    }
    // convert the human's risk-$ to a risk_pct of equity (gate_order sizes by %).
    // if equity is unknown we send no risk_pct and let the backend read balance.
    const risk_pct = equity && equity > 0 ? (risk / equity) * 100 : undefined;

    const seq = ++previewSeq.current;
    setPreviewing(true);
    try {
      const res = await api<Preview>("/api/binance/preview", {
        method: "POST",
        body: JSON.stringify({
          symbol: symbol.trim().toUpperCase(),
          side,
          entry: enN,
          stop: stN,
          target: tgN,
          leverage: lev,
          risk_pct,
        }),
        timeoutMs: STATE_TIMEOUT_MS,
      });
      if (seq !== previewSeq.current) return; // a newer preview superseded this one
      setPreview(res);
    } catch (e) {
      if (seq !== previewSeq.current) return;
      setPreviewErr(e instanceof Error ? e.message : String(e));
      setPreview(null);
    } finally {
      if (seq === previewSeq.current) setPreviewing(false);
    }
  }, [symbol, side, entry, stop, target, leverage, riskDollars, equity]);

  // the execute button is ENABLED only when the preview is approved AND every
  // single safety check is green. This is the hard gate on the UI side; the
  // backend re-runs gate_order regardless, but we never even offer the button.
  const allChecksGreen = useMemo(() => {
    const ch = preview?.checks ?? [];
    return ch.length > 0 && ch.every((c) => c.ok);
  }, [preview]);
  const canExecute = !!preview?.approved && allChecksGreen && !executing && !previewing;

  // ── execute ────────────────────────────────────────────────────────────────
  const doExecute = useCallback(async () => {
    if (!canExecute || !preview) return;
    const enN = parseFloat(entry);
    const stN = parseFloat(stop);
    const tgN = target.trim() === "" ? null : parseFloat(target);
    const lev = parseInt(leverage, 10) || 3;
    const risk = parseFloat(riskDollars);
    const risk_pct = equity && equity > 0 ? (risk / equity) * 100 : undefined;

    setExecuting(true);
    setTradeMsg(null);
    try {
      const res = await api<TradeResult>("/api/binance/trade", {
        method: "POST",
        body: JSON.stringify({
          symbol: symbol.trim().toUpperCase(),
          side, entry: enN, stop: stN, target: tgN, leverage: lev, risk_pct,
        }),
        timeoutMs: STATE_TIMEOUT_MS,
      });
      if (res.ok && res.placed_order) {
        const venue = res.live_enabled ? "חשבון אמיתי" : "TESTNET (לייב כבוי)";
        setTradeMsg({
          kind: "ok",
          text: `העסקה בוצעה ב-${venue} · כמות ${fmtNum(res.qty)} · כניסה ${fmtPrice(res.entry_price)} · סטופ ${fmtPrice(res.stop_price)} ${res.stop_verified ? "(סטופ אומת ✅)" : ""}`,
        });
      } else if (res.naked_position_guard) {
        setTradeMsg({
          kind: "err",
          text: `מנגנון הגנת פוזיציה חשופה הופעל: ${res.reason ?? "הסטופ לא אומת — הפוזיציה נסגרה אוטומטית"}`,
        });
      } else {
        setTradeMsg({ kind: "err", text: `העסקה נדחתה: ${res.reason ?? "סיבה לא ידועה"}` });
      }
    } catch (e) {
      setTradeMsg({ kind: "err", text: `שגיאה בביצוע: ${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setExecuting(false);
      setPreview(null); // force a fresh preview before any next order
      void refreshAll();
    }
  }, [canExecute, preview, symbol, side, entry, stop, target, leverage, riskDollars, equity, refreshAll]);

  // ── close position ─────────────────────────────────────────────────────────
  const pos = state?.position ?? null;
  const hasOpenPosition = !!pos && (pos.side === "long" || pos.side === "short") && Math.abs(pos.qty ?? 0) > 0;

  const doClose = useCallback(async () => {
    if (!hasOpenPosition) return;
    if (!window.confirm("לסגור את הפוזיציה הפתוחה עכשיו? פעולה זו סוגרת בשוק ומבטלת את הסטופ/יעד.")) return;
    setClosing(true);
    setTradeMsg(null);
    try {
      const res = await api<TradeResult & { flat?: boolean; realized_pnl?: number; exit_price?: number }>(
        "/api/binance/close",
        { method: "POST", body: JSON.stringify({ symbol: symbol.trim().toUpperCase() }), timeoutMs: STATE_TIMEOUT_MS },
      );
      if (res.ok) {
        setTradeMsg({
          kind: "ok",
          text: `הפוזיציה נסגרה · יציאה ${fmtPrice(res.exit_price)} · רווח/הפסד נטו ${fmtUsd(res.realized_pnl)}`,
        });
      } else {
        setTradeMsg({ kind: "err", text: `הסגירה נכשלה: ${res.reason ?? "סיבה לא ידועה"}` });
      }
    } catch (e) {
      setTradeMsg({ kind: "err", text: `שגיאה בסגירה: ${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setClosing(false);
      void refreshAll();
    }
  }, [hasOpenPosition, symbol, refreshAll]);

  const live = state?.live ?? null;
  const isLive = !!live?.live_enabled;
  const caps = state?.caps ?? null;

  // net P&L on the open position = gross unrealized − round-trip fee estimate.
  // fees are shown SEPARATELY; this is honest, not the gross number.
  const grossUpnl = pos?.unrealized_pnl ?? null;
  const posNotional = pos && pos.entry_price && pos.qty ? Math.abs(pos.entry_price * pos.qty) : null;
  const feeEst = posNotional != null ? posNotional * 0.0005 * 2 : null; // 0.05%/side round-trip
  const netUpnl = grossUpnl != null && feeEst != null ? grossUpnl - feeEst : grossUpnl;

  // recent ledger rows: show entries + exits + faults, newest first
  const ledgerRows = ledger.slice(0, 40);

  return (
    <div dir="rtl" style={{ display: "grid", gap: "var(--s-4)", padding: "4px 2px 40px", maxWidth: 1000, margin: "0 auto" }}>
      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: "var(--s-2)" }}>
        <div>
          <SectionTitle as="h2">₿ קוקפיט מסחר ידני — Binance Futures</SectionTitle>
          <p style={{ margin: "var(--s-1) 0 0", color: "var(--muted)", fontSize: "0.8125rem" }}>
            אתה מקבל כל החלטה — הכלי אוכף בטיחות ושקיפות בלבד, לא בוט אוטומטי ולא הבטחת רווח.
          </p>
        </div>
        <Button variant="ghost" onClick={() => void refreshAll()}>↻ רענן</Button>
      </div>

      {/* ── Live / testnet banner ── */}
      <div style={{
        padding: "var(--s-3) var(--s-4)", borderRadius: "var(--radius-md)", display: "flex",
        alignItems: "center", gap: "var(--s-3)", flexWrap: "wrap",
        background: isLive ? "var(--down-muted)" : "var(--accent-muted)",
        border: `1px solid ${isLive ? "var(--down)" : "var(--accent)"}`,
      }}>
        {isLive
          ? <Pill color="var(--down)" bg="var(--down-muted)">🔴 לייב — כסף אמיתי</Pill>
          : <Pill color="var(--accent-bright)" bg="var(--accent-muted)">🧪 TESTNET — בלי כסף אמיתי</Pill>}
        <span style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>
          {isLive
            ? "פקודות שתבצע ירוצו על החשבון האמיתי שלך."
            : (live?.reason_blocked ?? "לייב כבוי — פקודות ירוצו על TESTNET בלבד.")}
        </span>
      </div>

      {stateErr && (
        <div style={{ color: "var(--down)", background: "var(--down-muted)", padding: "var(--s-3)", borderRadius: "var(--radius-sm)", fontSize: "0.8125rem", border: "1px solid var(--down)" }}>
          שגיאת קריאה מהבורסה: {stateErr}
        </div>
      )}

      {/* ── Account header card ── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginBottom: "var(--s-3)", flexWrap: "wrap" }}>
          <span aria-hidden>💼</span>
          <SectionTitle as="h3" className="section-title--reset">חשבון</SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>יתרה זמינה ומרחק מתקרות ההפסד</span>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-5)", alignItems: "flex-end" }}>
          <div>
            <div style={labelStyle()}>יתרה (USDT)</div>
            <div style={{ fontSize: "1.75rem", fontWeight: 800, lineHeight: 1.1, fontVariantNumeric: "tabular-nums" }}>{fmtUsd(equity)}</div>
          </div>
          <div style={{ flex: 1 }} />
          {/* loss-cap bars */}
          <Bar label="הפסד יומי" value={0} max={3} danger={!!caps && !caps.allow_new} color="var(--live)" />
          <Bar label="ירידה כוללת" value={0} max={10} danger={!!caps?.halt} color="var(--accent)" />
        </div>
        {caps && !caps.allow_new && (
          <div style={{ marginTop: "var(--s-3)", fontSize: "0.8125rem", color: "var(--down)", fontWeight: 700 }}>
            ⛔ תקרת הפסד הופעלה — אין כניסות חדשות. {caps.reason}
          </div>
        )}
      </Card>

      {/* ── How safety works (long explanation, collapsed by default) ── */}
      <Collapsible
        title="איך הבטיחות עובדת"
        subtitle="מה הכלי אוכף מאחורי הקלעים לפני ואחרי כל פקודה"
        icon="🛡️"
      >
        <p style={{ margin: 0, color: "var(--text-secondary)", fontSize: "0.8125rem", lineHeight: 1.7 }}>
          התפקיד של הכלי הוא <b>אכיפת בטיחות ושקיפות</b> — לא בוט אוטומטי ולא הבטחת רווח.
          כל פקודה עוברת דרך שער הסיכון (gate_order), שהוא מסלול האישור היחיד.
          סטופ-לוס תמיד מוצמד בבורסה ומאומת אחרי הביצוע — אם הסטופ לא אומת, מנגנון
          הגנת פוזיציה חשופה סוגר את הפוזיציה אוטומטית. יש תקרות הפסד יומית וכוללת
          שחוסמות כניסות חדשות, וכפתור הביצוע נפתח רק כשהתצוגה המקדימה מאושרת וכל
          בדיקות הבטיחות ירוקות.
        </p>
      </Collapsible>

      {/* ── Current position card ── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginBottom: hasOpenPosition ? "var(--s-4)" : 0, flexWrap: "wrap" }}>
          <span aria-hidden>📈</span>
          <SectionTitle as="h3" className="section-title--reset">פוזיציה נוכחית · {state?.symbol ?? symbol}</SectionTitle>
          {hasOpenPosition
            ? <Pill color={sideColor(pos?.side)} bg="var(--bg-elevated)">{sideLabel(pos?.side)}</Pill>
            : <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>אין פוזיציה פתוחה</span>}
        </div>

        {hasOpenPosition && (
          <>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-3)" }}>
              <Stat label="כמות" value={fmtNum(pos?.qty)} />
              <Stat label="כניסה" value={fmtPrice(pos?.entry_price)} />
              <Stat label="מינוף" value={pos?.leverage != null ? `×${pos.leverage}` : "—"} />
              <Stat label="ליקווידציה" value={fmtPrice(state?.liquidation_price)} valueColor="var(--down)" />
              <Stat label="P&L ברוטו" value={fmtUsd(grossUpnl)} valueColor={pnlColor(grossUpnl)} />
              <Stat label="עמלות (אומדן)" value={feeEst != null ? `-${fmtUsd(feeEst).replace("-", "")}` : "—"} valueColor="var(--down)" />
              <Stat label="P&L נטו" value={fmtUsd(netUpnl)} valueColor={pnlColor(netUpnl)} big />
            </div>
            <button
              type="button"
              onClick={() => void doClose()}
              disabled={closing}
              style={{
                marginTop: "var(--s-4)", width: "100%", padding: "14px 16px", borderRadius: "var(--radius-md)",
                border: "1px solid var(--down)", background: "var(--down)", color: "#fff",
                fontSize: "1.0625rem", fontWeight: 800, cursor: closing ? "wait" : "pointer",
                opacity: closing ? 0.7 : 1,
              }}
            >
              {closing ? "סוגר…" : "✕ סגור פוזיציה עכשיו"}
            </button>
          </>
        )}
      </Card>

      {/* ── Manual trade panel ── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginBottom: "var(--s-4)", flexWrap: "wrap" }}>
          <span aria-hidden>🎯</span>
          <SectionTitle as="h3" className="section-title--reset">פאנל מסחר ידני</SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>בחר כיוון, מלא פרטים, הרץ תצוגה מקדימה — ואז בצע</span>
        </div>

        {/* LONG / SHORT */}
        <div style={{ display: "flex", gap: "var(--s-2)", marginBottom: "var(--s-3)" }}>
          {(["long", "short"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => { setSide(s); invalidatePreview(); }}
              style={{
                flex: 1, padding: "var(--s-3)", borderRadius: "var(--radius-sm)", fontSize: "0.9375rem", fontWeight: 800, cursor: "pointer",
                border: `1px solid ${side === s ? (s === "long" ? "var(--up)" : "var(--down)") : "var(--border)"}`,
                background: side === s ? (s === "long" ? "var(--up-muted)" : "var(--down-muted)") : "var(--card)",
                color: side === s ? (s === "long" ? "var(--up)" : "var(--down)") : (s === "long" ? "var(--up)" : "var(--down)"),
              }}
            >
              {s === "long" ? "▲ לונג" : "▼ שורט"}
            </button>
          ))}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: "var(--s-3)" }}>
          <Field label="סימבול">
            <input value={symbol} onChange={(e) => { setSymbol(e.target.value.toUpperCase()); invalidatePreview(); }} style={inputStyle()} />
          </Field>
          <Field label="סיכון ($)">
            <input type="number" inputMode="decimal" value={riskDollars}
              onChange={(e) => { setRiskDollars(e.target.value); invalidatePreview(); }} style={inputStyle()} />
          </Field>
          <Field label="מחיר כניסה">
            <input type="number" inputMode="decimal" value={entry} placeholder="0.00"
              onChange={(e) => { setEntry(e.target.value); invalidatePreview(); }} style={inputStyle()} />
          </Field>
          <Field label="סטופ-לוס">
            <input type="number" inputMode="decimal" value={stop} placeholder="0.00"
              onChange={(e) => { setStop(e.target.value); invalidatePreview(); }} style={inputStyle()} />
          </Field>
          <Field label="יעד (רשות)">
            <input type="number" inputMode="decimal" value={target} placeholder="ריק = ללא"
              onChange={(e) => { setTarget(e.target.value); invalidatePreview(); }} style={inputStyle()} />
          </Field>
          <Field label="מינוף">
            <input type="number" inputMode="numeric" value={leverage} min={1}
              onChange={(e) => { setLeverage(e.target.value); invalidatePreview(); }} style={inputStyle()} />
          </Field>
        </div>

        <button
          type="button"
          onClick={() => void doPreview()}
          disabled={previewing}
          style={{
            marginTop: "var(--s-4)", width: "100%", padding: "var(--s-3)", borderRadius: "var(--radius-sm)",
            border: "1px solid var(--accent)", background: "var(--accent)", color: "var(--text-on-accent)",
            fontSize: "0.9375rem", fontWeight: 800, cursor: previewing ? "wait" : "pointer", opacity: previewing ? 0.7 : 1,
          }}
        >
          {previewing ? "מחשב תצוגה מקדימה…" : "🔍 תצוגה מקדימה + בדיקות בטיחות"}
        </button>

        {previewErr && (
          <div style={{ marginTop: "var(--s-3)", color: "var(--down)", background: "var(--down-muted)", padding: "var(--s-3)", borderRadius: "var(--radius-sm)", fontSize: "0.8125rem", border: "1px solid var(--down)" }}>
            {previewErr}
          </div>
        )}

        {/* ── Preview output ── */}
        {preview && (
          <div style={{ marginTop: "var(--s-4)", borderRadius: "var(--radius-md)", background: "var(--bg-elevated)", border: "1px solid var(--border)", padding: "var(--s-4)" }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-3)", marginBottom: "var(--s-4)" }}>
              <Stat label="כמות" value={fmtNum(preview.qty)} />
              <Stat label="נפח עסקה" value={fmtUsd(preview.notional)} />
              <Stat label="סיכון בפועל" value={fmtUsd(preview.risk_dollars)} />
              <Stat label="מינוף" value={preview.leverage != null ? `×${Number(preview.leverage).toFixed(1)}` : "—"} />
              <Stat label="יחס R:R" value={preview.rr != null ? `${Number(preview.rr).toFixed(2)}` : "—"} />
              <Stat label="עמלות (סבב)" value={fmtUsd(preview.fee_est)} valueColor="var(--down)" />
              <Stat label="סליפג' (אומדן)" value={fmtUsd(preview.slippage_est)} valueColor="var(--down)" />
              <Stat label="ליקווידציה" value={fmtPrice(preview.liquidation_price)} valueColor="var(--down)" />
              <Stat label="יעד נטו" value={fmtUsd(preview.net_target)} valueColor={pnlColor(preview.net_target)} />
              <Stat label="הפסד בסטופ (נטו)" value={fmtUsd(preview.net_if_stopped)} valueColor={pnlColor(preview.net_if_stopped)} />
            </div>

            {/* safety checks — ✅/❌ each */}
            <div style={{ fontSize: "0.8125rem", fontWeight: 800, color: "var(--accent-bright)", marginBottom: "var(--s-2)" }}>בדיקות בטיחות</div>
            <div style={{ display: "grid", gap: "var(--s-1)" }}>
              {(preview.checks ?? []).map((c) => (
                <div key={c.name} style={{ display: "flex", gap: "var(--s-2)", alignItems: "flex-start", fontSize: "0.8125rem" }}>
                  <span style={{ flexShrink: 0 }}>{c.ok ? "✅" : "❌"}</span>
                  <span style={{ color: c.ok ? "var(--text-secondary)" : "var(--down)", fontWeight: c.ok ? 600 : 700 }}>
                    {checkLabel(c.name)}
                    {c.reason ? <span style={{ color: "var(--muted)", fontWeight: 400 }}> — {c.reason}</span> : null}
                  </span>
                </div>
              ))}
            </div>

            {/* overall verdict */}
            <div style={{
              marginTop: "var(--s-4)", padding: "var(--s-2) var(--s-3)", borderRadius: "var(--radius-sm)", fontSize: "0.875rem", fontWeight: 800,
              color: preview.approved && allChecksGreen ? "var(--up)" : "var(--down)",
              background: preview.approved && allChecksGreen ? "var(--up-muted)" : "var(--down-muted)",
              border: `1px solid ${preview.approved && allChecksGreen ? "var(--up)" : "var(--down)"}`,
            }}>
              {preview.approved && allChecksGreen
                ? "✅ כל הבדיקות עברו — מותר לבצע"
                : `❌ לא מאושר${preview.reason ? ` — ${preview.reason}` : ""}`}
            </div>

            {/* execute — disabled until approved && all checks green */}
            <button
              type="button"
              onClick={() => void doExecute()}
              disabled={!canExecute}
              style={{
                marginTop: "var(--s-4)", width: "100%", padding: "15px 16px", borderRadius: "var(--radius-md)",
                border: `1px solid ${canExecute ? "var(--up)" : "var(--border)"}`,
                background: canExecute ? "var(--up)" : "var(--bg-elevated)",
                color: canExecute ? "#fff" : "var(--muted)",
                fontSize: "1.0625rem", fontWeight: 800, cursor: canExecute ? "pointer" : "not-allowed",
              }}
            >
              {executing ? "מבצע…" : (isLive ? "בצע עסקה (כסף אמיתי)" : "בצע עסקה (TESTNET)")}
            </button>
            {!canExecute && (
              <div style={{ fontSize: 12, color: "var(--muted)", marginTop: "var(--s-2)", textAlign: "center" }}>
                הכפתור נפתח רק כשהתצוגה המקדימה מאושרת וכל הבדיקות ירוקות.
              </div>
            )}
          </div>
        )}

        {/* trade result message */}
        {tradeMsg && (
          <div style={{
            marginTop: "var(--s-3)", padding: "var(--s-3)", borderRadius: "var(--radius-sm)", fontSize: "0.875rem", fontWeight: 700,
            color: tradeMsg.kind === "ok" ? "var(--up)" : tradeMsg.kind === "warn" ? "#fde68a" : "var(--down)",
            background: tradeMsg.kind === "ok" ? "var(--up-muted)" : tradeMsg.kind === "warn" ? "#713f1233" : "var(--down-muted)",
            border: `1px solid ${tradeMsg.kind === "ok" ? "var(--up)" : tradeMsg.kind === "warn" ? "#713f12" : "var(--down)"}`,
          }}>
            {tradeMsg.text}
          </div>
        )}
      </Card>

      {/* ── Recent trades (sidecar ledger) ── */}
      <Card padding="md">
        <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", marginBottom: "var(--s-3)", flexWrap: "wrap" }}>
          <span aria-hidden>🧾</span>
          <SectionTitle as="h3" className="section-title--reset">עסקאות אחרונות</SectionTitle>
          <span style={{ fontSize: "0.8125rem", color: "var(--muted)" }}>ספר ביקורת — כניסות, יציאות ותקלות</span>
        </div>
        {ledgerRows.length === 0 ? (
          <div style={{ padding: "var(--s-5)", textAlign: "center", color: "var(--muted)", border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}>
            אין עדיין רישומים
          </div>
        ) : (
          <div style={{ display: "grid", gap: "var(--s-2)" }}>
            {ledgerRows.map((r) => {
              const isFault = r.event === "fault";
              const isExit = r.event === "exit";
              const accent = isFault ? "var(--down)" : isExit ? "var(--border-strong)" : "var(--up)";
              const eventLabel = isFault ? "תקלה" : isExit ? "יציאה" : "כניסה";
              return (
                <div key={r.id} style={{
                  display: "flex", gap: "var(--s-3)", alignItems: "center", flexWrap: "wrap",
                  padding: "9px var(--s-3)", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)",
                  border: "1px solid var(--border)", borderInlineStart: `4px solid ${accent}`, overflowX: "auto",
                }}>
                  <Pill color="var(--text-secondary)" bg="var(--card)">{eventLabel}</Pill>
                  <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>{fmtTime(r.ts)}</span>
                  <span style={{ fontSize: 12, color: "var(--text)", whiteSpace: "nowrap" }}>{r.symbol ?? "—"}</span>
                  {r.side && <span style={{ fontSize: 12, fontWeight: 800, color: sideColor(r.side), whiteSpace: "nowrap" }}>{sideLabel(r.side)}</span>}
                  {r.qty != null && <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>×{fmtNum(r.qty)}</span>}
                  {r.entry_price != null && <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>כניסה {fmtPrice(r.entry_price)}</span>}
                  {r.exit_price != null && <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>יציאה {fmtPrice(r.exit_price)}</span>}
                  {r.stop_price != null && <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>סטופ {fmtPrice(r.stop_price)}</span>}
                  {r.fee != null && <span style={{ fontSize: 12, color: "var(--down)", whiteSpace: "nowrap" }}>עמלה {fmtUsd(r.fee)}</span>}
                  {r.event === "entry" && (
                    <span style={{ fontSize: 12, color: r.stop_verified ? "var(--up)" : "var(--down)", whiteSpace: "nowrap" }}>
                      {r.stop_verified ? "סטופ אומת ✅" : "סטופ לא אומת ❌"}
                    </span>
                  )}
                  <span style={{ flex: 1 }} />
                  {r.realized_pnl != null && (
                    <span style={{ fontSize: 13, fontWeight: 800, color: pnlColor(r.realized_pnl), whiteSpace: "nowrap" }}>
                      נטו {fmtUsd(r.realized_pnl)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}

// ── small layout helpers ─────────────────────────────────────────────────────
// Standard stat tile: label + big value, on an elevated bordered surface.
function Stat({ label, value, valueColor, big }: {
  label: string; value: string; valueColor?: string; big?: boolean;
}) {
  return (
    <div style={{
      background: "var(--bg-elevated)", border: "1px solid var(--border)",
      borderRadius: "var(--radius-md)", padding: "var(--s-3)", minWidth: 110,
    }}>
      <div style={{ fontSize: "0.75rem", color: "var(--muted)", fontWeight: 600, marginBottom: "var(--s-1)" }}>{label}</div>
      <div style={{
        fontSize: big ? "1.5rem" : "1.125rem", fontWeight: 700,
        color: valueColor ?? "var(--text)", lineHeight: 1.2, fontVariantNumeric: "tabular-nums",
      }}>{value}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "block" }}>
      <div style={labelStyle()}>{label}</div>
      {children}
    </label>
  );
}

function labelStyle(): React.CSSProperties {
  return { fontSize: "0.75rem", color: "var(--muted)", fontWeight: 600, marginBottom: "var(--s-1)" };
}
function inputStyle(): React.CSSProperties {
  return {
    width: "100%", boxSizing: "border-box", padding: "9px 10px", borderRadius: "var(--radius-sm)",
    border: "1px solid var(--border-strong)", background: "var(--bg-elevated)", color: "var(--text)", fontSize: 14,
  };
}
