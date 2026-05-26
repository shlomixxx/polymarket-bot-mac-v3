import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer } from "recharts";
import {
  api,
  engineUrl,
  isPageHidden,
  TIMEOUT_MS_MARKET_CURRENT,
  TIMEOUT_MS_ORDERBOOK_SUMMARY,
  TIMEOUT_MS_DEMO_STATE,
} from "./api";
import {
  chartAxisTick,
  chartTooltipStyle,
  chartStroke,
  smoothCurveType,
  computeBtcPriceChartYDomain,
} from "./chartConstants";
import { formatPnlAxisTime, formatPctAxisTick } from "./pnlChartFormatters";
import { useChartAnimationGate } from "./hooks/useChartAnimationGate";
import { usePriceStream } from "./hooks/usePriceStream";
import { Button } from "./ui/Button";
import { Card } from "./ui/Card";
import { ChartCard } from "./ui/ChartCard";
import { SectionTitle } from "./ui/SectionTitle";
import { PnlOpenAreaChart, PnlClosedAreaChart } from "./ui/PnlSessionAreaCharts";
import TipsV2 from "./TipsV2";
import SignalsPanel from "./SignalsPanel";
import TriggerTrader from "./TriggerTrader";
import AnalyticsV3 from "./AnalyticsV3";

type Market = {
  slug: string;
  epoch: number;
  title: string;
  token_up: string;
  token_down: string;
  order_min_size: number;
  /** clob = מינימום מספר CLOB (/book); gamma = רק מטא־דאטה Gamma לפני CLOB */
  order_min_size_source?: "clob" | "gamma";
  window_sec?: number;
  btc_window?: string;
  seconds_left: number;
  price_to_beat: number | null;
  price_to_beat_note: string;
  /** chainlink_polygon_window | binance_1m_fallback — מקור ייחוס לעומת Polymarket */
  price_to_beat_source?: string;
  /** מ-Gamma API — קישור למקור הרזולוציה (אין ב-API מחיר ייחוס מספרי) */
  polymarket_resolution_source?: string | null;
};

type SideSummary = { bid: number | null; ask: number | null; mid: number | null };
type OrderbookSummary = {
  slug: string;
  up: SideSummary;
  down: SideSummary;
};

type Tab = "dash" | "strategy" | "signals" | "trigger" | "stats" | "stats_live" | "tips_v2" | "analytics_v3" | "help";

type Trade = {
  id?: string;
  ts?: number;
  type?: string;
  side?: string;
  contracts?: number;
  price?: number;
  fee_est?: number;
  token_id?: string;
  session_id?: string;
  reconcile_origin?: boolean;
  realized_pnl?: number;
  trade_num?: number;
  peak_unrealized_pct?: number;
  trough_unrealized_pct?: number;
  potential_peak_unrealized_pct?: number;
  potential_trough_unrealized_pct?: number;
  peak_mark_bid?: number;
  trough_mark_bid?: number;
  peak_ts?: number;
  trough_ts?: number;
  pnl_path?: { ts: number; upnl_pct: number; bid?: number }[];
  epoch?: number;
  slug?: string;
  window_sec?: number;
  /** פירוק מחלון (ממנוע הדמו — פרוקסי Binance, לא Chainlink) */
  settlement_btc_start?: number;
  settlement_btc_end?: number;
  settlement_won?: boolean;
  resolved_outcome?: string;
  settlement_price_source?: string;
  [k: string]: unknown;
};

/** תואם ל־FEE_RATE במנוע הדמו (עמלת מכירה במימוש) */
const ENGINE_FEE_RATE = 0.002;

function usdToCentsLabel(usd: number): string {
  if (!Number.isFinite(usd)) return "—";
  return `${(usd * 100).toFixed(1)}¢`;
}

/** מחיר BTC בדולרים לתצוגת פירוק (ייחוס / סוף חלון) */
function formatBtcUsdLabel(usd: number | null | undefined): string {
  if (usd == null || !Number.isFinite(Number(usd))) return "—";
  const n = Number(usd);
  return `$${n.toLocaleString("en-US", { maximumFractionDigits: 0, minimumFractionDigits: 0 })}`;
}

/** תאי טבלה — פירוק BTC כששמור בעסקה (ניצחון/הפסד מוצגים באותה בולטות) */
function settlementBtcTableCells(t: Trade): [string, string, string, string] {
  const a = t.settlement_btc_start;
  const b = t.settlement_btc_end;
  if (a == null || b == null || !Number.isFinite(Number(a)) || !Number.isFinite(Number(b))) {
    return ["—", "—", "—", "—"];
  }
  const af = Number(a);
  const bf = Number(b);
  const res =
    typeof t.resolved_outcome === "string" && t.resolved_outcome
      ? t.resolved_outcome
      : bf >= af
        ? "Up"
        : "Down";
  let match: string;
  if (typeof t.settlement_won === "boolean") {
    match = t.settlement_won ? "ניצחון" : "הפסד";
  } else if (t.side === "Up" || t.side === "Down") {
    const up = bf >= af;
    const won = t.side === "Up" ? up : !up;
    match = won ? "ניצחון" : "הפסד";
  } else {
    match = "—";
  }
  return [formatBtcUsdLabel(af), formatBtcUsdLabel(bf), res, match];
}

const TIP_SETTLEMENT_BTC =
  "פירוק Up/Down לפי Polymarket: אם מחיר ה-BTC בסוף החלון ≥ מחיר בתחילת החלון — מנצח Up; אחרת Down. " +
  "המספרים כאן מהמנוע (פרוקסי Binance 1m), לא Chainlink הרשמי לפירוק.";

const TIP_OPEN_BTC_LIVE =
  "ייחוס פתיחת החלון (Price to Beat) מול מחיר BTC חי מהמסך. " +
  "בסוף החלון: אם סוף ≥ ייחוס — מנצח Up; אחרת Down. במהלך החלון זה רק מצב נוכחי, לא פירוק סופי.";

/** עלות הרגל לפי יציאת TP: proceeds − realized (כמו ב־demo_engine) */
function legCostFromTpExit(t: Trade): number | null {
  const c = Number(t.contracts);
  const px = Number(t.price);
  const rp = t.realized_pnl;
  if (!c || c <= 0 || !Number.isFinite(px) || rp == null || !Number.isFinite(Number(rp))) return null;
  const proceeds = px * c * (1 - ENGINE_FEE_RATE);
  const legCost = proceeds - Number(rp);
  return legCost > 0 ? legCost : null;
}

/** bid (דולרים) שמתאים לתשואה % היפותטית מול אותה עלות */
function bidFromHypotheticalPct(legCost: number, contracts: number, pct: number): number | null {
  if (!Number.isFinite(pct) || contracts <= 0 || legCost <= 0) return null;
  const legVal = legCost * (1 + pct / 100);
  return legVal / (contracts * (1 - ENGINE_FEE_RATE));
}

/** שינוי % במחיר הביד לעומת מחיר יציאת TP (לא מול עלות) */
function pctVsExitPrice(exitUsd: number, bidUsd: number): number | null {
  if (!Number.isFinite(exitUsd) || exitUsd <= 0 || !Number.isFinite(bidUsd)) return null;
  return ((bidUsd - exitUsd) / exitUsd) * 100;
}

/** אחרי איפוס לוח/סטטיסטיקה — רק עסקאות מהסשן הנוכחי בתצוגה; ההיסטוריה המלאה נשמרת בקובץ (ניתוח v3). */
function tradesForSessionStats(trades: Trade[], demoState: Record<string, unknown> | null | undefined): Trade[] {
  if (!trades?.length) return [];
  const ts0 = demoState?.stats_epoch_ts;
  if (typeof ts0 !== "number" || !Number.isFinite(ts0)) return trades;
  return trades.filter((t) => Number(t.ts || 0) >= ts0);
}

/** רק עסקאות שנרשמו כמסחר חי (CLOB אמיתי) — ללשונית סטטיסטיקה לייב. */
function tradesLiveOnly(trades: Trade[]): Trade[] {
  return trades.filter((t) => String(t.execution || "") === "live");
}

/** סנכרון יומן צל ↔ CLOB — לא «עסקת מסחר»; realized_pnl שם הוא דלתא חשבונאית ומזייף גרף PnL. */
function isReconcileLedgerEntry(t: Trade): boolean {
  return t.type === "RECONCILE";
}

/**
 * פירוק/סגירה לפי מודל סוף-חלון (יומן צל) — לא בהכרח תנועת CLOB.
 * בלייב, אחרי rollover עם drift, סכימת SETTLE_* על עשרות «פוזיציות» ישנות + reconcile
 * יוצרת קפיצות PnL שלא תואמות יתרה — לכן לא נכנסות לגרף/יחס ניצחונות בלייב.
 */
function isShadowWindowSettlementTrade(t: Trade): boolean {
  const ty = String(t.type || "");
  return (
    ty === "SETTLE_WIN" ||
    ty === "SETTLE_LOSS" ||
    ty === "SETTLE_UNKNOWN" ||
    ty === "EXPIRE_0"
  );
}

type SessionGroup = { sessionId: string; trades: Trade[] };

function groupTradesBySession(trades: Trade[]): SessionGroup[] {
  /** יציאות (EXPIRE/SELL) בלי session_id — מצמידים ל-session של ה-BUY באותו token_id */
  // פוזיציות שנטענו מה-chain (reconcile_origin) לא נפתחו ב-BUY של הריצה → לא מציגים
  // אותן כ«עסקה» ריקה בהיסטוריית הריצה. היתרה מתעדכנת רגיל דרך SETTLE/RECONCILE.
  // תאימות אחורה: רשומות ישנות שנוצרו לפני הוספת הדגל — נזהה אותן כ-SETTLE/EXPIRE ללא
  // session_id ושאין להן BUY תואם באותו token_id ברשימה.
  const buysByToken = new Set<string>();
  for (const t of trades) {
    if (t.type === "BUY" && t.token_id) buysByToken.add(t.token_id);
  }
  const isLegacyOrphanSettle = (t: Trade) => {
    const isSettleOrExpire =
      t.type === "SETTLE_WIN" ||
      t.type === "SETTLE_LOSS" ||
      t.type === "SETTLE_UNKNOWN" ||
      t.type === "EXPIRE_0";
    if (!isSettleOrExpire) return false;
    if (t.session_id) return false;
    return !t.token_id || !buysByToken.has(t.token_id);
  };
  const filtered = trades.filter((t) => !t.reconcile_origin && !isLegacyOrphanSettle(t));
  const sorted = [...filtered].sort((a, b) => (Number(a.ts) || 0) - (Number(b.ts) || 0));
  const sessionByToken = new Map<string, string>();
  for (const t of sorted) {
    if (t.type === "BUY" && t.token_id) {
      const sid = t.session_id || t.id;
      if (sid) sessionByToken.set(t.token_id, sid);
    }
  }
  const bySession = new Map<string, Trade[]>();
  for (const t of filtered) {
    let sid = t.session_id;
    if (!sid && t.token_id) {
      const fromTok = sessionByToken.get(t.token_id);
      if (fromTok) sid = fromTok;
    }
    if (!sid) {
      sid = (t.type === "BUY" ? t.id : null) || `orphan-${t.id || Math.random()}`;
    }
    const key = sid || "none";
    if (!bySession.has(key)) bySession.set(key, []);
    bySession.get(key)!.push(t);
  }
  const groups: SessionGroup[] = [];
  for (const [sessionId, list] of bySession) {
    list.sort((a, b) => (Number(a.ts) || 0) - (Number(b.ts) || 0));
    groups.push({ sessionId, trades: list });
  }
  groups.sort((a, b) => {
    const tsA = a.trades[0]?.ts ?? 0;
    const tsB = b.trades[0]?.ts ?? 0;
    return (tsB as number) - (tsA as number);
  });
  return groups;
}

type LogEntry = { ts: number; msg: string; type: string; session_id?: string };
type Leg = {
  token_id?: string;
  peak_unrealized_pct?: number;
  trough_unrealized_pct?: number;
  peak_ts?: number;
  trough_ts?: number;
  /** תשואה % מול עלות — עדכני מ־mark_to_market (כל ~1s עם רענון מסך) */
  unrealized_pct?: number;
  /** כש־CLOB לא החזיר bid — רגל מסומנת כלא עדכנית; המנוע לא מוסיף דגימות חדשות ל־pnl_path */
  book_stale?: boolean;
  pnl_path?: { ts: number; upnl_pct: number }[];
};
type LastMark = { legs?: Leg[]; book_stale?: boolean; ts?: number } | null;

/** הסבר ל-tooltips: אחוזים = תשואה מול עלות, לא מחיר ליחידה (0.01–0.99) */
const TIP_PCT_ROI =
  "אחוז תשואה ביחס לעלות הממוצעת (רווח או הפסד לא ממומשים). אין לבלבל עם מחיר החוזה: מחיר ליחידה נשמר בדרך כלל בטווח 0.01–0.99 דולר. תשואה העולה על 100% אפשרית כאשר הערך ביחס לעלות מוכפל.";
const TIP_PCT_POTENTIAL_AFTER_TP =
  "מדד משני: תשואה באחוזים ביחס לעלות הכניסה, בנקודות הביקוש לאחר יציאה ביעד רווח — ולא ביחס למחיר היציאה.";
const TIP_AFTER_TP_VS_EXIT =
  "לאחר יציאה ביעד רווח: השוואה בין מחיר הביקוש הנוכחי למחיר היציאה (בסנטים ובאחוזים). המדד משמש להערכת תנודה ביחס ליציאה בפועל.";

/** ציר Y: כולל שיא/שפל מסומני מים + מרווח נשימה */
function yDomainForPnlChart(
  series: { upnl: number }[],
  peak?: number | null,
  trough?: number | null,
): [number, number] {
  const vals: number[] = [];
  for (const p of series) {
    if (Number.isFinite(p.upnl)) vals.push(p.upnl);
  }
  if (peak != null && Number.isFinite(peak)) vals.push(peak);
  if (trough != null && Number.isFinite(trough)) vals.push(trough);
  if (vals.length === 0) return [-5, 5];
  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  const pad = Math.max((hi - lo) * 0.06, 0.5);
  return [lo - pad, hi + pad];
}

/**
 * מוסיף נקודות שיא/שפל מסומני מים אם חסרות במסלול (הדגימה פספסה קצה בין טיקים).
 */
function enrichPnlSeriesWithExtrema(
  series: { ts: number; upnl: number; t: string }[],
  opts: {
    peak?: number | null;
    peak_ts?: number | null;
    trough?: number | null;
    trough_ts?: number | null;
  },
): { ts: number; upnl: number; t: string }[] {
  const out = series.map((p) => ({ ...p }));
  const pushIf = (ts: number | null | undefined, upnl: number | null | undefined) => {
    if (ts == null || !Number.isFinite(Number(ts)) || upnl == null || !Number.isFinite(Number(upnl))) return;
    const t = Number(ts);
    const u = Number(upnl);
    const covered = out.some(
      (p) => Math.abs(p.ts - t) < 2.0 && Math.abs(p.upnl - u) < 0.25,
    );
    if (covered) return;
    out.push({ ts: t, upnl: u, t: formatPnlAxisTime(t) });
  };
  pushIf(opts.peak_ts, opts.peak);
  pushIf(opts.trough_ts, opts.trough);
  out.sort((a, b) => a.ts - b.ts);
  for (let i = 1; i < out.length; i++) {
    if (out[i].ts <= out[i - 1].ts) {
      out[i].ts = out[i - 1].ts + 1e-6;
      out[i].t = formatPnlAxisTime(out[i].ts);
    }
  }
  return out;
}

/**
 * נתוני גרף תשואה: נקודות מהשרת + זנב חי לפי unrealized_pct.
 * חשוב: ציר X חייב להיות **זמן מספרי (epoch seconds)** — לא מחרוזת שעה.
 * אין דדופ לפי ts בלבד — באותה שנייה יכולות להיות שתי רמות שונות (קפיצה).
 */
function buildPnlChartSeries(
  pnlPath: { ts: number; upnl_pct: number }[] | undefined,
  opts: {
    isOpen: boolean;
    liveUnrealizedPct: number | null | undefined;
    nowSec: number;
    /** זמן last_mark מהמנוע — לזנב כש־stale / נקודת ייחוס */
    lastMarkTs?: number | null;
    bookStale?: boolean;
  },
): { ts: number; upnl: number; t: string }[] {
  const raw = (pnlPath ?? [])
    .map((p) => ({
      ts: Number(p.ts),
      upnl: p.upnl_pct,
    }))
    .filter((p) => Number.isFinite(p.ts) && Number.isFinite(p.upnl));
  raw.sort((a, b) => a.ts - b.ts);
  const deduped: typeof raw = [];
  for (const p of raw) {
    const prev = deduped[deduped.length - 1];
    if (prev && prev.ts === p.ts && prev.upnl === p.upnl) continue;
    deduped.push(p);
  }
  let base = deduped.map((p) => ({ ts: p.ts, upnl: p.upnl, t: formatPnlAxisTime(p.ts) }));

  const u = opts.liveUnrealizedPct;
  const canLive = opts.isOpen && u != null && Number.isFinite(u);
  const stale = Boolean(opts.bookStale);

  if (!canLive) return base;

  if (stale) {
    if (base.length > 0) return base;
    // בלי pnl_path אין מה להציג — לא מייצרים שתי נקודות עם אותו upnl (נראה כמו קו אופקי "שבור")
    return [];
  }

  if (base.length === 0) {
    // עד דגימת path ראשונה מהמנוע (~POSITION_TRACKING_PATH_INTERVAL) — placeholder בממשק, לא קו מזויף
    return [];
  }
  const last = base[base.length - 1];
  /**
   * תשואה חיה מוצגת רק בעדכון **Y** על זמן הדגימה האחרונה של pnl_path (last.ts).
   * לא מותחים X ל־last_mark.ts / עכשיו — כי mark_to_market יכול להתקדם בלי דגימת path חדשה,
   * ואז נוצר קטע אופקי ארוך + גמגום ברינדורים מהירים.
   * האחוז למעלה בממשק עדיין חי מ־last_mark.legs.
   */
  return [...base.slice(0, -1), { ts: last.ts, upnl: u, t: formatPnlAxisTime(last.ts) }];
}

/** אורך חלון BTC לעסקה — לטבלה/גרף (5m=300, 15m=900) */
function windowSecForTrade(t: Trade, fallbackSec: number): number {
  if (typeof t.window_sec === "number" && t.window_sec > 0) {
    return t.window_sec;
  }
  const slug = typeof t.slug === "string" ? t.slug : "";
  if (slug.includes("15m")) return 900;
  if (slug.includes("5m")) return 300;
  return fallbackSec > 0 ? fallbackSec : 300;
}

function formatWindowFromTrade(t: Trade): string {
  const epoch = t.epoch != null ? Number(t.epoch) : undefined;
  const dur = windowSecForTrade(t, 300);
  const ts = Number(t.ts) || 0;
  const windowStartSec = epoch ?? (ts ? Math.floor(ts / dur) * dur : 0);
  if (!windowStartSec) return "";
  const start = new Date(windowStartSec * 1000);
  const end = new Date((windowStartSec + dur) * 1000);
  return `${start.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })}–${end.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })}`;
}

/** משך זמן מחזור: מכניסה ראשונה (או עסקה ראשונה) עד יציאה אחרונה */
function formatDurationSec(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  if (h > 0) return `${h} שע׳ ${m} דק׳`;
  if (m > 0) return `${m} דק׳ ${s} שנ׳`;
  return `${s} שנ׳`;
}

function formatHms(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  const pad2 = (n: number) => String(n).padStart(2, "0");
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
}

function isSessionExitTrade(t: Trade): boolean {
  const ty = t.type;
  return (
    ty === "SELL_TP" ||
    ty === "EXPIRE_0" ||
    ty === "SETTLE_WIN" ||
    ty === "SETTLE_LOSS" ||
    ty === "SETTLE_UNKNOWN" ||
    (typeof ty === "string" && ty.startsWith("SELL"))
  );
}

/** תווית קצרה לטבלת מחזורים: TP / EXPIRE / SETTLE_WIN */
function sessionExitLabel(lastType: string | undefined): string {
  if (!lastType) return "";
  if (lastType === "SETTLE_WIN") return "SETTLE_WIN";
  if (lastType === "EXPIRE_0" || lastType === "SETTLE_LOSS" || lastType === "SETTLE_UNKNOWN") {
    return "EXPIRE";
  }
  return "TP";
}

function sessionEntryExitTimes(g: { trades: Trade[] }): {
  startSec: number;
  endSec: number | null;
} {
  const buys = g.trades.filter((t) => t.type === "BUY");
  const exits = g.trades.filter(isSessionExitTrade);
  const tsList = (arr: Trade[]) =>
    arr.map((t) => Number(t.ts) || 0).filter((x) => x > 0);
  const buyTs = tsList(buys);
  const allTs = tsList(g.trades);
  const exitTs = tsList(exits);
  let startSec = 0;
  if (buyTs.length > 0) startSec = Math.min(...buyTs);
  else if (allTs.length > 0) startSec = Math.min(...allTs);
  const endSec = exitTs.length > 0 ? Math.max(...exitTs) : null;
  return { startSec, endSec };
}

function TradesBySession({
  trades,
  logEntries = [],
  lastMark,
  fallbackWindowSec = 300,
  liveBtcUsd = null,
  priceToBeatUsd = null,
  marketEpoch = null,
  priceToBeatNote = "",
}: {
  trades: Trade[];
  logEntries?: LogEntry[];
  lastMark?: LastMark;
  /** כשאין window_sec בעסקה (היסטוריה ישנה) — לפי השוק הנוכחי / הגדרת btc_window */
  fallbackWindowSec?: number;
  /** מחיר BTC חי (לשורת עסקה פתוחה) */
  liveBtcUsd?: number | null;
  /** ייחוס פתיחת החלון מהשוק הנוכחי — `/api/market/current` */
  priceToBeatUsd?: number | null;
  /** epoch של השוק הפעיל — להתאמה לכניסה (שדה epoch בעסקת BUY) */
  marketEpoch?: number | null;
  priceToBeatNote?: string;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  /** מסלול PnL אחרון לפי session — כשהשרת מחזיר רגעית pnl_path ריק (throttle / מרוץ) לא נוריד את הגרף */
  const lastOpenPnlPathBySessionRef = useRef<Record<string, { ts: number; upnl_pct: number }[]>>({});
  const groups = useMemo(() => groupTradesBySession(trades.slice().reverse().slice(0, 5000)), [trades]);
  /** מחירי חלון רטרואקטיביים כשחסרים settlement_btc_* בעסקה (היסטוריה לפני השמירה ב-TP) */
  const [retroBtcByKey, setRetroBtcByKey] = useState<
    Record<string, { start: number; end: number } | "loading" | "fail">
  >({});
  const retroFetchStartedRef = useRef(new Set<string>());
  /** ניסיון רטרו חוזר אוטומטי — לכל מפתח epoch-window לכל היותר פעם אחת (מונע לולאה אם השרת למטה). */
  const retroAutoRetryOnceRef = useRef(new Set<string>());
  const scheduleRetroRetry = useCallback((key: string) => {
    if (retroAutoRetryOnceRef.current.has(key)) return;
    retroAutoRetryOnceRef.current.add(key);
    window.setTimeout(() => {
      retroFetchStartedRef.current.delete(key);
      setRetroBtcByKey((prev) => {
        if (prev[key] !== "fail") return prev;
        const n = { ...prev };
        delete n[key];
        return n;
      });
    }, 3500);
  }, []);
  useEffect(() => {
    for (const g of groups) {
      const hasStored = g.trades.some(
        (t) =>
          typeof t.settlement_btc_start === "number" &&
          Number.isFinite(t.settlement_btc_start) &&
          typeof t.settlement_btc_end === "number" &&
          Number.isFinite(t.settlement_btc_end),
      );
      if (hasStored) continue;
      const buys = g.trades.filter((t) => t.type === "BUY");
      const epRaw = buys[0]?.epoch ?? g.trades.find((t) => t.epoch != null)?.epoch;
      const ep = epRaw != null && Number.isFinite(Number(epRaw)) ? Number(epRaw) : null;
      if (ep == null) continue;
      const wsRaw = buys[0]?.window_sec;
      const ws = typeof wsRaw === "number" && wsRaw > 0 ? wsRaw : fallbackWindowSec;
      const windowEndSec = ep + ws;
      // עד סיום החלון אין נר סוף ב-Binance — קריאת רטרו תיכשל סתם; המילוי בא מ-/api/demo/state
      if (Date.now() / 1000 < windowEndSec - 0.5) continue;
      const key = `${ep}-${ws}`;
      if (retroFetchStartedRef.current.has(key)) continue;
      retroFetchStartedRef.current.add(key);
      setRetroBtcByKey((p) => (p[key] !== undefined ? p : { ...p, [key]: "loading" }));
      void api<{ start: number | null; end: number | null }>(
        `/api/btc/window-prices?epoch=${ep}&window_sec=${ws}`,
      )
        .then((r) => {
          if (
            r.start != null &&
            r.end != null &&
            Number.isFinite(r.start) &&
            Number.isFinite(r.end)
          ) {
            setRetroBtcByKey((prev) => ({ ...prev, [key]: { start: r.start!, end: r.end! } }));
          } else {
            setRetroBtcByKey((prev) => ({ ...prev, [key]: "fail" }));
            scheduleRetroRetry(key);
          }
        })
        .catch(() => {
          setRetroBtcByKey((prev) => ({ ...prev, [key]: "fail" }));
          scheduleRetroRetry(key);
        });
    }
  }, [groups, fallbackWindowSec, scheduleRetroRetry]);
  const toggle = (sid: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid);
      else next.add(sid);
      return next;
    });
  };
  return (
    <>
      <p style={{ fontSize: 12, opacity: 0.85, marginBottom: 8 }}>
        שיא/שפל % — יחסית לעלות הרגל (כולל עמלת כניסה), לא מסך יתרת החשבון.
      </p>
      <div
        className="table-scroll"
        style={{
          maxHeight: 400,
          overflow: "auto",
          borderRadius: 10,
        }}
      >
      {groups.map((g, idx) => {
        const buys = g.trades.filter((t) => t.type === "BUY");
        const windowSecForRetro =
          typeof buys[0]?.window_sec === "number" && buys[0].window_sec > 0
            ? buys[0].window_sec
            : fallbackWindowSec;
        const epochForRetro =
          buys[0]?.epoch != null && Number.isFinite(Number(buys[0].epoch))
            ? Number(buys[0].epoch)
            : (() => {
                const t = g.trades.find((x) => x.epoch != null);
                return t != null && Number.isFinite(Number(t.epoch)) ? Number(t.epoch) : null;
              })();
        const retroKey = epochForRetro != null ? `${epochForRetro}-${windowSecForRetro}` : null;
        const retroEntry = retroKey ? retroBtcByKey[retroKey] : undefined;
        const retroPair =
          retroEntry && typeof retroEntry === "object" && "start" in retroEntry
            ? retroEntry
            : undefined;
        const retroLoading = retroEntry === "loading";
        const retroFailed = retroEntry === "fail";
        const exits = g.trades.filter(isSessionExitTrade);
        const isOpen = exits.length === 0;
        const side = buys[0]?.side || "—";
        const exitType = exits.length ? sessionExitLabel(exits[exits.length - 1]?.type) : "";
        const realized = g.trades.reduce((s, t) => s + (Number(t.realized_pnl) || 0), 0);
        const lastExit = exits[exits.length - 1];
        const tokenId = buys[0]?.token_id;
        const liveLeg = tokenId
          ? lastMark?.legs?.find((l) => l.token_id === tokenId)
          : undefined;
        const peak = lastExit?.peak_unrealized_pct ?? liveLeg?.peak_unrealized_pct;
        const trough = lastExit?.trough_unrealized_pct ?? liveLeg?.trough_unrealized_pct;
        const potentialPeak = lastExit?.potential_peak_unrealized_pct;
        const potentialTrough = lastExit?.potential_trough_unrealized_pct;
        const sid = g.sessionId;
        const isExp = expanded.has(sid);
        const firstTrade = g.trades[0];
        const windowStr = firstTrade ? formatWindowFromTrade(firstTrade) : "";
        const { startSec: sessStart, endSec: sessEnd } = sessionEntryExitTimes(g);
        const durationClosedSec =
          sessEnd != null && sessStart > 0 && sessEnd >= sessStart ? sessEnd - sessStart : null;
        const durationOpenSec =
          isOpen && sessStart > 0 ? Math.max(0, Date.now() / 1000 - sessStart) : null;
        const durationLabel =
          durationClosedSec != null
            ? formatDurationSec(durationClosedSec)
            : durationOpenSec != null
              ? `${formatDurationSec(durationOpenSec)} (עד עכשיו)`
              : null;
        const entryClock =
          sessStart > 0
            ? new Date(sessStart * 1000).toLocaleTimeString("he-IL", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })
            : null;
        const exitClock =
          sessEnd != null && sessEnd > 0
            ? new Date(sessEnd * 1000).toLocaleTimeString("he-IL", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })
            : null;
        // trade_num קבוע מהשרת (נשמר לדיסק); אם חסר (היסטוריה ישנה) — idx+1 כגיבוי
        const tradeNum = buys.find(b => b.trade_num != null)?.trade_num ?? (idx + 1);
        const summaryMainBase =
          `עסקה #${tradeNum} — ${side} ` +
          (buys.length > 1 ? `DCA ×${buys.length} ` : "") +
          (exitType ? `→ ${exitType}` : "(פתוחה)") +
          (windowStr ? ` | חלון ${windowStr}` : "") +
          (durationLabel ? ` | משך ${durationLabel}` : "");
        const showRealized = realized !== 0 && Number.isFinite(realized);
        const hasExit = exits.length > 0;
        const peakStr = peak != null ? `${peak.toFixed(1)}%` : "—";
        const troughStr = trough != null ? `${trough.toFixed(1)}%` : "—";
        const potentialPeakStr = potentialPeak != null ? `${potentialPeak.toFixed(1)}%` : "—";
        const potentialTroughStr = potentialTrough != null ? `${potentialTrough.toFixed(1)}%` : "—";
        const tpExit = lastExit?.type === "SELL_TP" ? lastExit : undefined;
        const legCost = tpExit ? legCostFromTpExit(tpExit) : null;
        const exitBidUsd = tpExit && tpExit.price != null ? Number(tpExit.price) : null;
        const contractsN = tpExit && tpExit.contracts != null ? Number(tpExit.contracts) : 0;
        const bidPeakHyp =
          legCost != null && contractsN > 0 && potentialPeak != null
            ? bidFromHypotheticalPct(legCost, contractsN, potentialPeak)
            : null;
        const bidTroughHyp =
          legCost != null && contractsN > 0 && potentialTrough != null
            ? bidFromHypotheticalPct(legCost, contractsN, potentialTrough)
            : null;
        const canShowCentsPanel =
          Boolean(tpExit) &&
          legCost != null &&
          (potentialPeak != null || potentialTrough != null) &&
          exitBidUsd != null &&
          contractsN > 0;
        const deltaCents = (bidUsd: number | null) => {
          if (bidUsd == null || exitBidUsd == null || !Number.isFinite(bidUsd) || !Number.isFinite(exitBidUsd)) return null;
          return (bidUsd - exitBidUsd) * 100;
        };
        const dPeakC = bidPeakHyp != null ? deltaCents(bidPeakHyp) : null;
        const dTroughC = bidTroughHyp != null ? deltaCents(bidTroughHyp) : null;
        const pctVsExitPeak =
          exitBidUsd != null && bidPeakHyp != null ? pctVsExitPrice(exitBidUsd, bidPeakHyp) : null;
        const pctVsExitTrough =
          exitBidUsd != null && bidTroughHyp != null ? pctVsExitPrice(exitBidUsd, bidTroughHyp) : null;
        /** פירוק BTC יכול לשבת על SETTLE_* גם כשהיציאה האחרונה כרונולוגית היא TP (שני סוגי יציאה בסשן) */
        const tradeWithSettlementBtc = [...g.trades]
          .slice()
          .reverse()
          .find(
            (t) =>
              typeof t.settlement_btc_start === "number" &&
              Number.isFinite(t.settlement_btc_start) &&
              typeof t.settlement_btc_end === "number" &&
              Number.isFinite(t.settlement_btc_end),
          );
        const btcRefStored = tradeWithSettlementBtc?.settlement_btc_start;
        const btcEndStored = tradeWithSettlementBtc?.settlement_btc_end;
        const btcRef =
          typeof btcRefStored === "number" && Number.isFinite(btcRefStored)
            ? btcRefStored
            : retroPair?.start;
        const btcEndPx =
          typeof btcEndStored === "number" && Number.isFinite(btcEndStored)
            ? btcEndStored
            : retroPair?.end;
        const hasSettlementBtc =
          typeof btcRef === "number" &&
          Number.isFinite(btcRef) &&
          typeof btcEndPx === "number" &&
          Number.isFinite(btcEndPx);
        let resolvedMarket: string | undefined =
          tradeWithSettlementBtc?.resolved_outcome ?? lastExit?.resolved_outcome;
        if (
          resolvedMarket == null &&
          hasSettlementBtc &&
          btcRef != null &&
          btcEndPx != null
        ) {
          resolvedMarket = btcEndPx >= btcRef ? "Up" : "Down";
        }
        let settleWon: boolean | undefined;
        if (tradeWithSettlementBtc?.settlement_won !== undefined) {
          settleWon = tradeWithSettlementBtc.settlement_won;
        } else if (
          hasSettlementBtc &&
          btcRef != null &&
          btcEndPx != null &&
          (side === "Up" || side === "Down")
        ) {
          const marketUp = btcEndPx >= btcRef;
          settleWon = side === "Up" ? marketUp : !marketUp;
        } else {
          settleWon = lastExit?.settlement_won;
        }
        const btcPanelFromTp = tradeWithSettlementBtc?.type === "SELL_TP";
        const windowEndSecBtc =
          epochForRetro != null ? epochForRetro + windowSecForRetro : null;
        const awaitingWindowCloseForBtc =
          lastExit?.type === "SELL_TP" &&
          windowEndSecBtc != null &&
          Date.now() / 1000 < windowEndSecBtc - 0.5 &&
          !hasSettlementBtc &&
          retroPair == null;
        const firstBuyEpoch =
          buys[0]?.epoch != null && Number.isFinite(Number(buys[0].epoch))
            ? Number(buys[0].epoch)
            : null;
        const openWindowMatchesDashboard =
          marketEpoch != null &&
          firstBuyEpoch != null &&
          marketEpoch === firstBuyEpoch;
        const ptbOpen = priceToBeatUsd;
        const liveSpot = liveBtcUsd;
        const openHasRef =
          isOpen &&
          openWindowMatchesDashboard &&
          ptbOpen != null &&
          Number.isFinite(ptbOpen);
        const openHasLive =
          liveSpot != null && Number.isFinite(liveSpot) && liveSpot > 0;
        let liveVsRefHint: string | null = null;
        if (openHasRef && openHasLive && ptbOpen != null && liveSpot != null) {
          const d = liveSpot - ptbOpen;
          if (side === "Up") {
            liveVsRefHint = d >= 0 ? "מול ייחוס: כרגע לכיוון Up" : "מול ייחוס: כרגע לכיוון Down";
          } else if (side === "Down") {
            liveVsRefHint = d < 0 ? "מול ייחוס: כרגע לכיוון Down" : "מול ייחוס: כרגע לכיוון Up";
          }
        }
        return (
          <div key={sid} style={{ borderBottom: "1px solid #334155" }}>
            <div dir="rtl" style={{ display: "flex", alignItems: "stretch", width: "100%" }}>
              <button
                type="button"
                onClick={() => toggle(sid)}
                style={{
                  flex: 1,
                  textAlign: "right",
                  padding: "10px 12px",
                  background: isExp ? "#1e293b" : "#0b1220",
                  border: "none",
                  color: "#fff",
                  cursor: "pointer",
                  fontSize: 13,
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "flex-end",
                  gap: 4,
                  minWidth: 0,
                  width: "100%",
                }}
              >
                <span style={{ width: "100%" }}>
                  {isExp ? "▼ " : "▶ "}
                  {summaryMainBase}
                  {showRealized && (
                    <span
                      style={{
                        fontWeight: 700,
                        color: realized >= 0 ? "var(--up)" : "var(--down)",
                        marginInlineStart: 4,
                      }}
                    >
                      {` ${realized >= 0 ? "+" : "-"}$${Math.abs(realized).toFixed(2)}`}
                    </span>
                  )}
                </span>
                {isOpen && (
                  <span
                    style={{
                      display: "flex",
                      gap: 8,
                      flexWrap: "wrap",
                      justifyContent: "flex-end",
                      width: "100%",
                    }}
                  >
                    {openHasRef && openHasLive ? (
                      <span
                        title={`${TIP_OPEN_BTC_LIVE}${priceToBeatNote ? ` — ${priceToBeatNote}` : ""}`}
                        style={{
                          fontSize: 11,
                          color: "#e2e8f0",
                          padding: "2px 8px",
                          background: "rgba(59, 130, 246, 0.15)",
                          borderRadius: 6,
                          border: "1px solid rgba(59, 130, 246, 0.35)",
                          textAlign: "right",
                          maxWidth: "100%",
                        }}
                      >
                        <span style={{ fontSize: 10, color: "var(--muted)", display: "block" }}>
                          פתוח — ייחוס חלון (לניצחון Up בסוף: סוף ≥ ייחוס)
                        </span>
                        ייחוס {formatBtcUsdLabel(ptbOpen)} · BTC עכשיו {formatBtcUsdLabel(liveSpot)}
                        {liveVsRefHint ? (
                          <span style={{ color: "var(--muted)", marginInlineStart: 4 }}>· {liveVsRefHint}</span>
                        ) : null}
                      </span>
                    ) : null}
                    {isOpen && openHasRef && !openHasLive ? (
                      <span
                        style={{ fontSize: 11, color: "var(--muted)", padding: "2px 8px" }}
                        title={TIP_OPEN_BTC_LIVE}
                      >
                        ייחוס {formatBtcUsdLabel(ptbOpen)} · מחיר חי: טוען…
                      </span>
                    ) : null}
                    {isOpen && !openWindowMatchesDashboard && openHasLive ? (
                      <span style={{ fontSize: 11, color: "var(--muted)", padding: "2px 8px" }}>
                        BTC עכשיו {formatBtcUsdLabel(liveSpot)}
                        <span title="השוואת ייחוס דורשת שדה epoch בכניסה וחלון זהה לשוק המוצג">
                          {" "}
                          · ייחוס חלון הכניסה לא מוצג (epoch לא תואם או חסר)
                        </span>
                      </span>
                    ) : null}
                  </span>
                )}
                {hasExit && (
                  <span style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
                    <span
                      title={`שיא/שפל תשואה בזמן החזקה — ${TIP_PCT_ROI}`}
                      style={{
                        fontSize: 11,
                        color: "var(--muted)",
                        padding: "2px 8px",
                        background: "rgba(100, 116, 139, 0.2)",
                        borderRadius: 6,
                      }}
                    >
                      בזמן החזקה (מול עלות): {peakStr} | {troughStr}
                    </span>
                    {(potentialPeak != null || potentialTrough != null) &&
                      (canShowCentsPanel && (dPeakC != null || dTroughC != null) ? (
                        <span
                          title={TIP_AFTER_TP_VS_EXIT}
                          style={{
                            fontSize: 11,
                            color: "#e2e8f0",
                            padding: "2px 8px",
                            background: "rgba(148, 163, 184, 0.15)",
                            borderRadius: 6,
                            border: "1px dashed #475569",
                            textAlign: "right",
                            maxWidth: "100%",
                          }}
                        >
                          <span style={{ fontSize: 10, color: "var(--muted)", display: "block" }}>
                            אחרי TP — מול יציאה ({exitBidUsd != null ? usdToCentsLabel(exitBidUsd) : "—"})
                          </span>
                          <span>
                            {dPeakC != null && (
                              <>
                                שיא Δ{dPeakC >= 0 ? "+" : ""}
                                {dPeakC.toFixed(1)}¢
                                {pctVsExitPeak != null && (
                                  <span style={{ color: "var(--muted)", fontSize: 10 }}> ({pctVsExitPeak >= 0 ? "+" : ""}
                                  {pctVsExitPeak.toFixed(1)}%)</span>
                                )}
                              </>
                            )}
                            {dPeakC != null && dTroughC != null ? " · " : ""}
                            {dTroughC != null && (
                              <>
                                שפל Δ{dTroughC >= 0 ? "+" : ""}
                                {dTroughC.toFixed(1)}¢
                                {pctVsExitTrough != null && (
                                  <span style={{ color: "var(--muted)", fontSize: 10 }}> ({pctVsExitTrough >= 0 ? "+" : ""}
                                  {pctVsExitTrough.toFixed(1)}%)</span>
                                )}
                              </>
                            )}
                          </span>
                          <span style={{ fontSize: 10, color: "var(--muted)", display: "block", marginTop: 3 }}>
                            מול עלות (משני): {potentialPeakStr} | {potentialTroughStr}
                          </span>
                        </span>
                      ) : (
                        <span
                          title={TIP_PCT_POTENTIAL_AFTER_TP}
                          style={{
                            fontSize: 11,
                            color: "var(--muted)",
                            padding: "2px 8px",
                            background: "rgba(148, 163, 184, 0.15)",
                            borderRadius: 6,
                            border: "1px dashed #475569",
                          }}
                        >
                          אחרי TP — מול עלות בלבד: {potentialPeakStr} | {potentialTroughStr}
                        </span>
                      ))}
                  </span>
                )}
              </button>
              {hasExit &&
                (hasSettlementBtc || retroLoading || retroFailed || awaitingWindowCloseForBtc) && (
                <div
                  title={TIP_SETTLEMENT_BTC}
                  dir="rtl"
                  style={{
                    flex: "0 0 auto",
                    width: "min(260px, 36vw)",
                    minWidth: 168,
                    padding: "8px 10px",
                    boxSizing: "border-box",
                    borderInlineStart: "1px solid #334155",
                    background:
                      realized >= 0
                        ? "rgba(34, 197, 94, 0.07)"
                        : "rgba(34, 197, 94, 0.12)",
                    display: "flex",
                    flexDirection: "column",
                    justifyContent: "center",
                    alignItems: "stretch",
                    textAlign: "right",
                  }}
                >
                  <span style={{ fontSize: 10, color: "var(--muted)", display: "block", marginBottom: 4 }}>
                    פירוק BTC (ייחוס פתיחה → סוף חלון)
                    {btcPanelFromTp ? (
                      <span style={{ color: "#94a3b8" }}> · נשמר ביציאת TP</span>
                    ) : tradeWithSettlementBtc ? null : retroPair ? (
                      <span style={{ color: "#94a3b8" }}> · חישוב רטרו (Binance proxy)</span>
                    ) : null}
                  </span>
                  {awaitingWindowCloseForBtc ? (
                    <span style={{ fontSize: 11, color: "var(--muted)" }}>
                      החלון עדיין לא נסגר — מחיר סוף (לפירוק Up/Down) יתמלא אוטומטית במנוע אחרי סיום החלון.
                      רענן את המסך אחרי סוף החלון; אין צורך ברטרו מהדפדפן.
                    </span>
                  ) : retroLoading && !hasSettlementBtc ? (
                    <span style={{ fontSize: 11, color: "var(--muted)" }}>טוען פירוק BTC…</span>
                  ) : retroFailed && !hasSettlementBtc ? (
                    <span style={{ fontSize: 11, color: "var(--muted)" }}>
                      לא ניתן לטעון מחירי חלון (רטרו). ודא שהמנוע רץ על 127.0.0.1:8767 — הנתונים אמורים
                      להגיע גם מהשמירה אחרי רענון (מילוי אוטומטי בשרת).
                    </span>
                  ) : (
                    <span style={{ fontSize: 11, color: "#e2e8f0", lineHeight: 1.45 }}>
                      ייחוס {formatBtcUsdLabel(btcRef)} · סוף {formatBtcUsdLabel(btcEndPx)}
                      {resolvedMarket ? (
                        <span style={{ color: "var(--muted)" }}> · מנצח השוק: {resolvedMarket}</span>
                      ) : null}
                      {settleWon != null ? (
                        <span
                          style={{
                            color: settleWon ? "#f8fafc" : "var(--down)",
                            fontWeight: 700,
                            display: "block",
                            marginTop: 4,
                            padding: "5px 8px",
                            borderRadius: 6,
                            background: settleWon ? "rgba(15, 23, 42, 0.65)" : "transparent",
                            border: settleWon ? "1px solid rgba(34, 197, 94, 0.5)" : "none",
                            textShadow: settleWon ? "0 1px 2px rgba(0,0,0,0.45)" : undefined,
                          }}
                        >
                          הימור {side}: {settleWon ? "ניצחון" : "הפסד"}
                        </span>
                      ) : null}
                    </span>
                  )}
                </div>
              )}
            </div>
            {isExp && (
              <div style={{ padding: "0 12px 12px", background: "#111827" }}>
                {(entryClock || sessStart > 0) && (
                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--muted)",
                      padding: "10px 0 4px",
                      textAlign: "right",
                      lineHeight: 1.5,
                    }}
                  >
                    <strong style={{ color: "#cbd5e1" }}>זמני מחזור:</strong> כניסה ראשונה{" "}
                    <span style={{ color: "#fff" }}>{entryClock ?? "—"}</span>
                    {exitClock ? (
                      <>
                        {" "}
                        → יציאה <span style={{ color: "#fff" }}>{exitClock}</span>
                        {durationClosedSec != null && (
                          <span>
                            {" "}
                            · משך <span style={{ color: "#fff" }}>{formatDurationSec(durationClosedSec)}</span>
                          </span>
                        )}
                      </>
                    ) : isOpen ? (
                      <span>
                        {" "}
                        · פתוחה · משך מצטבר{" "}
                        <span style={{ color: "#fff" }}>
                          {durationOpenSec != null ? formatDurationSec(durationOpenSec) : "—"}
                        </span>{" "}
                        <span style={{ fontSize: 10 }}>(מתעדכן ברענון המסך)</span>
                      </span>
                    ) : null}
                  </div>
                )}
                {(() => {
                  const serverPnlRaw =
                    ((lastExit?.pnl_path ?? liveLeg?.pnl_path) as
                      | { ts: number; upnl_pct: number; bid?: number; balance?: number; equity?: number }[]
                      | undefined) || [];
                  const validServerPoints = serverPnlRaw.filter(
                    (p) => Number.isFinite(Number(p.ts)) && Number.isFinite(Number(p.upnl_pct)),
                  );
                  if (isOpen) {
                    if (validServerPoints.length > 0) {
                      lastOpenPnlPathBySessionRef.current[sid] = validServerPoints.map((p) => ({
                        ts: Number(p.ts),
                        upnl_pct: Number(p.upnl_pct),
                      }));
                    }
                  } else {
                    delete lastOpenPnlPathBySessionRef.current[sid];
                  }
                  let pnlPath = serverPnlRaw;
                  if (
                    isOpen &&
                    validServerPoints.length === 0 &&
                    liveLeg?.unrealized_pct != null &&
                    Number.isFinite(liveLeg.unrealized_pct)
                  ) {
                    const retained = lastOpenPnlPathBySessionRef.current[sid];
                    if (retained && retained.length > 0) {
                      pnlPath = retained;
                    }
                  }
                  const nowSec = Date.now() / 1000;
                  const lastMarkTs =
                    lastMark != null && typeof (lastMark as { ts?: number }).ts === "number"
                      ? (lastMark as { ts: number }).ts
                      : null;
                  const bookStale = Boolean(lastMark?.book_stale || liveLeg?.book_stale);
                  const chartForOpen = buildPnlChartSeries(pnlPath, {
                    isOpen: true,
                    liveUnrealizedPct: liveLeg?.unrealized_pct,
                    nowSec,
                    lastMarkTs,
                    bookStale,
                  });
                  const chartForClosed = buildPnlChartSeries(pnlPath, {
                    isOpen: false,
                    liveUnrealizedPct: undefined,
                    nowSec,
                    lastMarkTs,
                    bookStale: false,
                  });
                  const chartForOpenDisplay = enrichPnlSeriesWithExtrema(chartForOpen, {
                    peak: liveLeg?.peak_unrealized_pct,
                    peak_ts: liveLeg?.peak_ts,
                    trough: liveLeg?.trough_unrealized_pct,
                    trough_ts: liveLeg?.trough_ts,
                  });
                  const chartForClosedDisplay = enrichPnlSeriesWithExtrema(chartForClosed, {
                    peak: lastExit?.peak_unrealized_pct,
                    peak_ts: lastExit?.peak_ts,
                    trough: lastExit?.trough_unrealized_pct,
                    trough_ts: lastExit?.trough_ts,
                  });
                  const yDomainOpen = yDomainForPnlChart(
                    chartForOpenDisplay,
                    liveLeg?.peak_unrealized_pct,
                    liveLeg?.trough_unrealized_pct,
                  );
                  const yDomainClosed = yDomainForPnlChart(
                    chartForClosedDisplay,
                    lastExit?.peak_unrealized_pct,
                    lastExit?.trough_unrealized_pct,
                  );
                  const showPnlEmptyPlaceholder = isOpen && chartForOpen.length === 0;
                  const hasLiveUnrealized =
                    liveLeg?.unrealized_pct != null && Number.isFinite(liveLeg.unrealized_pct);
                  const sessionLogs = logEntries.filter((e) => e.session_id === sid);
                  return (
                    <>
                      {hasExit &&
                        (peak != null || trough != null || potentialPeak != null || potentialTrough != null) && (
                        <div
                          style={{
                            marginBottom: 12,
                            padding: "10px 12px",
                            background: "#1e293b",
                            borderRadius: 8,
                          }}
                        >
                          {(peak != null || trough != null) && (
                            <>
                          <div
                            style={{
                              display: "flex",
                              gap: 24,
                              justifyContent: "flex-end",
                              flexWrap: "wrap",
                            }}
                          >
                            <span title={TIP_PCT_ROI}>
                              <span style={{ color: "var(--muted)", fontSize: 11 }}>שיא (בזמן החזקה, מול עלות)</span>{" "}
                              <strong style={{ color: peak != null && peak >= 0 ? "var(--up)" : "inherit" }}>
                                {peakStr}
                              </strong>
                            </span>
                            <span title={TIP_PCT_ROI}>
                              <span style={{ color: "var(--muted)", fontSize: 11 }}>שפל (מול עלות)</span>{" "}
                              <strong style={{ color: trough != null && trough < 0 ? "var(--down)" : "inherit" }}>
                                {troughStr}
                              </strong>
                            </span>
                          </div>
                          <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 6, textAlign: "right", lineHeight: 1.45 }}>
                            מחושב על כל הפוזיציה — ממוצע משוקלל (DCA). האחוזים הם <strong>תשואה מול עלות</strong>, לא מחיר ליחידה —
                            מעל 100% אפשרי; מחיר החוזה בפולימרקט נשאר בדרך כלל בטווח 0.01–0.99$.
                          </div>
                            </>
                          )}
                          {(potentialPeak != null || potentialTrough != null) && (
                            <div
                              style={{
                                marginTop: 10,
                                paddingTop: 10,
                                borderTop: "1px dashed #334155",
                                textAlign: "right",
                              }}
                            >
                              {(bidPeakHyp != null || bidTroughHyp != null) && (
                                <>
                              <div style={{ color: "#86efac", fontWeight: 700, fontSize: 12, marginBottom: 6 }}>
                                אחרי TP — מול מחיר יציאה ({exitBidUsd != null ? usdToCentsLabel(exitBidUsd) : "—"})
                              </div>
                              <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 8, lineHeight: 1.45 }}>
                                {TIP_AFTER_TP_VS_EXIT}
                              </div>
                              {bidPeakHyp != null && dPeakC != null && pctVsExitPeak != null && (
                                <div style={{ marginBottom: 4 }}>
                                  <span style={{ color: "var(--muted)", fontSize: 11 }}>שיא bid: </span>
                                  <strong style={{ color: "var(--up)" }}>{usdToCentsLabel(bidPeakHyp)}</strong>
                                  <span style={{ color: "var(--muted)", fontSize: 11 }}>
                                    {" "}
                                    (Δ {dPeakC >= 0 ? "+" : ""}
                                    {dPeakC.toFixed(1)}¢ · {pctVsExitPeak >= 0 ? "+" : ""}
                                    {pctVsExitPeak.toFixed(1)}% מול יציאה)
                                  </span>
                                </div>
                              )}
                              {bidTroughHyp != null && dTroughC != null && pctVsExitTrough != null && (
                                <div style={{ marginBottom: 8 }}>
                                  <span style={{ color: "var(--muted)", fontSize: 11 }}>שפל bid: </span>
                                  <strong
                                    style={{
                                      color: potentialTrough != null && potentialTrough < 0 ? "var(--down)" : "inherit",
                                    }}
                                  >
                                    {usdToCentsLabel(bidTroughHyp)}
                                  </strong>
                                  <span style={{ color: "var(--muted)", fontSize: 11 }}>
                                    {" "}
                                    (Δ {dTroughC >= 0 ? "+" : ""}
                                    {dTroughC.toFixed(1)}¢ · {pctVsExitTrough >= 0 ? "+" : ""}
                                    {pctVsExitTrough.toFixed(1)}% מול יציאה)
                                  </span>
                                </div>
                              )}
                                </>
                              )}
                              <div style={{ color: "#93c5fd", fontWeight: 700, fontSize: 12, marginTop: 8, marginBottom: 4 }}>
                                מול עלות כניסה (להשוואה — אותו בסיס כמו בזמן החזקה)
                              </div>
                              <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 6 }} title={TIP_PCT_POTENTIAL_AFTER_TP}>
                                {TIP_PCT_POTENTIAL_AFTER_TP}
                              </div>
                              <div>
                                <span style={{ color: "var(--muted)", fontSize: 11 }}>שיא: </span>
                                <strong style={{ color: potentialPeak != null && potentialPeak >= 0 ? "var(--up)" : "inherit" }}>
                                  {potentialPeakStr}
                                </strong>
                                <span style={{ color: "var(--muted)", fontSize: 11 }}> · שפל: </span>
                                <strong
                                  style={{
                                    color: potentialTrough != null && potentialTrough < 0 ? "var(--down)" : "inherit",
                                  }}
                                >
                                  {potentialTroughStr}
                                </strong>
                                <span style={{ color: "var(--muted)", fontSize: 11 }}> (מול עלות)</span>
                              </div>
                              <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 6, lineHeight: 1.45 }}>
                                שני המדדים מתארים אותו מסלול bid אחרי TP; הראשון קריא כ&quot;עוד כמה ¢ מהיציאה&quot;, השני כמו לפני המכירה.
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                      {isOpen && lastMark?.book_stale && (
                        <div className="alert-warn" style={{ marginBottom: 8, textAlign: "right" }}>
                          ספר ההזמנות (CLOB) אינו זמין כרגע; מוצגים הנתונים האחרונים שנשמרו כדי לשמור על רצף הגרף. העדכון יתחדש עם חזרת נתוני הביקוש.
                        </div>
                      )}
                      {/* תא קבוע — לא מסתירים לגמרי כשאין unrealized_pct (מונע "קפיצות" של גרף/יומן) */}
                      {isOpen && (
                        <div
                          style={{
                            marginBottom: 10,
                            padding: "8px 10px",
                            minHeight: 58,
                            boxSizing: "border-box",
                            background: "#0f172a",
                            borderRadius: 8,
                            textAlign: "right",
                            border: "1px solid #334155",
                            contain: "layout",
                          }}
                          title={TIP_PCT_ROI}
                        >
                          <span style={{ color: "var(--muted)", fontSize: 12 }}>תשואה נוכחית (ביחס לעלות): </span>
                          {liveLeg?.unrealized_pct != null && Number.isFinite(liveLeg.unrealized_pct) ? (
                            <>
                              {/* מספרים ב־LTR + tabular-nums + רוחב מינימלי — מונעים הזזת שורה/גרף בכל עדכון רענון */}
                              <strong
                                dir="ltr"
                                style={{
                                  display: "inline-block",
                                  fontSize: 15,
                                  minWidth: "7.5ch",
                                  fontVariantNumeric: "tabular-nums",
                                  color: liveLeg.unrealized_pct >= 0 ? "var(--up)" : "var(--down)",
                                  verticalAlign: "baseline",
                                }}
                              >
                                {liveLeg.unrealized_pct >= 0 ? "+" : ""}
                                {liveLeg.unrealized_pct.toFixed(1)}%
                              </strong>
                              <span
                                style={{
                                  display: "block",
                                  marginTop: 4,
                                  fontSize: 10,
                                  color: "var(--muted)",
                                }}
                              >
                                מתעדכן בדומה לקצב רענון המסך וסימון השוק
                              </span>
                            </>
                          ) : (
                            <span style={{ fontSize: 13, color: "var(--muted)" }}>
                              — (ממתין לסימון שוק)
                            </span>
                          )}
                        </div>
                      )}
                      {isOpen && (
                        <div style={{ marginBottom: 12, minHeight: 178 }}>
                          <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 2 }}>מסלול תשואה באחוזים (ביחס לעלות, לאורך העסקה)</div>
                          <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 6 }}>
                            העקומה מחושבת בין נקודות הדגימה; קווים מקווקווים ירוק ואדום מסמנים שיא ושפל מסומנים. עדכון אחוזי התשואה נשען על זמן הדגימה האחרונה.
                          </div>
                          {!showPnlEmptyPlaceholder ? (
                            <PnlOpenAreaChart
                              sessionId={sid}
                              data={chartForOpenDisplay}
                              yDomain={yDomainOpen}
                              peakTrough={{ peak, trough }}
                            />
                          ) : (
                            <div
                              style={{
                                height: 140,
                                display: "flex",
                                flexDirection: "column",
                                alignItems: "center",
                                justifyContent: "center",
                                gap: 6,
                                padding: "8px 12px",
                                textAlign: "center",
                                background: "#0b1220",
                                borderRadius: 8,
                                border: "1px dashed #334155",
                              }}
                              title={
                                bookStale
                                  ? "ספר ההזמנות אינו עדכני; לא יתקבלו דגימות מסלול חדשות עד לחזרת נתוני הביקוש."
                                  : hasLiveUnrealized
                                    ? "המנוע אוגר דגימות מסלול בתדירות קבועה; הקו יופיע לאחר הכניסה או עם שחזור הספר."
                                    : "ממתין לסימון שוק מהמנוע (תשואה בשוטף)."
                              }
                            >
                              <span style={{ color: "#475569", fontSize: 22, letterSpacing: 2, userSelect: "none" }} aria-hidden>
                                ···
                              </span>
                              <span style={{ color: "#64748b", fontSize: 11, lineHeight: 1.35, maxWidth: 280 }}>
                                {bookStale
                                  ? "ספר ההזמנות אינו עדכני; המסלול יתעדכן עם חזרת נתוני הביקוש."
                                  : hasLiveUnrealized
                                    ? "ממתין לדגימת המסלול הראשונה מהמנוע."
                                    : "ממתין לסימון שוק (תשואה בשוטף)."}
                              </span>
                            </div>
                          )}
                        </div>
                      )}
                      {!isOpen && chartForClosed.length > 0 && (
                        <div style={{ marginBottom: 12 }}>
                          <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 2 }}>מסלול תשואה באחוזים (ביחס לעלות, לאורך העסקה)</div>
                          <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 6 }}>
                            עקומה חלקה בין נקודות דגימה; קווים מקווקווים מסמנים שיא ושפל. ציר הערכים כולל את טווח השיא והשפל גם כשנקודה בודדת חסרה במסלול.
                          </div>
                          <PnlClosedAreaChart
                            sessionId={sid}
                            data={chartForClosedDisplay}
                            yDomain={yDomainClosed}
                            peakTrough={{ peak, trough }}
                          />
                        </div>
                      )}
                      {sessionLogs.length > 0 && (
                        <div style={{ marginBottom: 12 }}>
                          <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>יומן העסקה</div>
                          <div style={{ maxHeight: 120, overflow: "auto", fontSize: 11, background: "#0b1220", padding: 8, borderRadius: 6 }}>
                            {sessionLogs.slice(-20).map((e, i) => (
                              <div key={i} style={{ marginBottom: 2 }}>
                                <span style={{ color: e.type === "event" ? "var(--up)" : "var(--muted)" }}>
                                  {new Date(e.ts * 1000).toLocaleTimeString("he-IL")}
                                </span>
                                {" — "}
                                {e.msg}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </>
                  );
                })()}
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr>
                    {(
                      [
                        [
                          `תחילת חלון (${Math.round(fallbackWindowSec / 60)} דק׳ נוכחי)`,
                          "מיושר לפי אורך החלון של העסקה (5 או 15 דק׳); עסקאות ישנות לפי ברירת המסך",
                        ],
                        ["זמן", undefined],
                        ["פעולה", undefined],
                        ["צד", undefined],
                        ["חוזים", undefined],
                        ["מחיר", "מחיר ליחידה (למשל 0.01–0.99)"],
                        [
                          "עלות הכניסה",
                          "מחיר × חוזים — כמה דולרים משקיעים בכניסה זו (ברוטו לפני עמלה; העמלה בעמודה נפרדת)",
                        ],
                        ["עמלה", undefined],
                        ["רווח", undefined],
                        ["שיא %", TIP_PCT_ROI],
                        ["שפל %", TIP_PCT_ROI],
                        [
                          "ייחוס BTC",
                          "מחיר ייחוס בתחילת החלון (כשהמנוע שמר פירוק)",
                        ],
                        ["סוף BTC", "מחיר בסוף החלון (פרוקסי Binance במנוע)"],
                        ["מנצח שוק", "Up אם סוף ≥ ייחוס"],
                        [
                          "פירוק vs הימור",
                          "התאמה בין כיוון השוק לבין הצד שנכנסת — לא לפי $ רווח מהמסחר",
                        ],
                        ["Gate", undefined],
                        ["סיבה", undefined],
                      ] as const
                    ).map(([h, tip]) => (
                        <th
                          key={h}
                          title={tip}
                          style={{ textAlign: "right", padding: "6px 10px", borderBottom: "1px solid #334155" }}
                        >
                          {h}
                        </th>
                      ))}
                  </tr>
                </thead>
                <tbody>
                  {g.trades.map((t, i) => (
                    <tr key={String(t.id || i)} style={{ borderBottom: "1px solid #111827" }}>
                      <td style={{ padding: "6px 10px", color: "var(--muted)" }}>
                        {t.ts
                          ? (() => {
                              const tsNum = Number(t.ts) * 1000;
                              const durMs = windowSecForTrade(t, fallbackWindowSec) * 1000;
                              const winMs = Math.floor(tsNum / durMs) * durMs;
                              return new Date(winMs).toLocaleTimeString("he-IL", {
                                hour: "2-digit",
                                minute: "2-digit",
                              });
                            })()
                          : "—"}
                      </td>
                      <td style={{ padding: "6px 10px", color: "var(--muted)" }}>
                        {t.ts ? new Date((t.ts as number) * 1000).toLocaleTimeString("he-IL") : "—"}
                      </td>
                      <td style={{ padding: "6px 10px" }}>
                        {t.type === "BUY" ? "כניסה" : t.type === "SELL_TP" ? "יציאה (TP)" : t.type || ""}
                      </td>
                      <td style={{ padding: "6px 10px" }}>
                        <span style={{
                          color: t.side === "Up" ? "var(--up)"
                               : t.side === "Down" ? "var(--down)"
                               : "var(--muted)",
                        }}>
                          {t.side === "neutral" || t.side === "auto" ? "?" : (t.side ?? "—")}
                        </span>
                      </td>
                      <td style={{ padding: "6px 10px" }}>{Number(t.contracts || 0).toFixed(0)}</td>
                      <td style={{ padding: "6px 10px" }}>{t.price != null ? Number(t.price).toFixed(2) : "—"}</td>
                      <td
                        style={{ padding: "6px 10px", color: "var(--muted)" }}
                        title={
                          t.type === "BUY" &&
                          t.price != null &&
                          t.contracts != null &&
                          Number.isFinite(Number(t.price)) &&
                          Number.isFinite(Number(t.contracts))
                            ? `${Number(t.price).toFixed(2)} × ${Number(t.contracts).toFixed(0)} חוזים`
                            : undefined
                        }
                      >
                        {(() => {
                          if (t.type !== "BUY") return "—";
                          const p = t.price != null ? Number(t.price) : NaN;
                          const n = t.contracts != null ? Number(t.contracts) : NaN;
                          if (!Number.isFinite(p) || !Number.isFinite(n)) return "—";
                          return `$${(p * n).toFixed(2)}`;
                        })()}
                      </td>
                      <td style={{ padding: "6px 10px", color: "var(--muted)" }}>
                        {t.fee_est != null ? Number(t.fee_est).toFixed(4) : "—"}
                      </td>
                      <td style={{ padding: "6px 10px" }}>
                        {t.realized_pnl != null ? (
                          <span
                            style={{
                              color: Number(t.realized_pnl) >= 0 ? "var(--up)" : "var(--down)",
                            }}
                          >
                            {Number(t.realized_pnl) >= 0 ? "+" : "-"}$
                            {Math.abs(Number(t.realized_pnl)).toFixed(2)}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td style={{ padding: "6px 10px", color: "var(--muted)", fontSize: 11 }}>
                        {t.peak_unrealized_pct != null ? `${Number(t.peak_unrealized_pct).toFixed(1)}%` : "—"}
                      </td>
                      <td style={{ padding: "6px 10px", color: "var(--muted)", fontSize: 11 }}>
                        {t.trough_unrealized_pct != null ? `${Number(t.trough_unrealized_pct).toFixed(1)}%` : "—"}
                      </td>
                      {(() => {
                        const [refC, endC, winM, betM] = settlementBtcTableCells(t);
                        const strong = betM === "ניצחון";
                        return (
                          <>
                            <td
                              style={{ padding: "6px 10px", color: "var(--muted)", fontSize: 11 }}
                              title="פירוק BTC — ייחוס"
                            >
                              {refC}
                            </td>
                            <td
                              style={{ padding: "6px 10px", color: "var(--muted)", fontSize: 11 }}
                              title="פירוק BTC — סוף חלון"
                            >
                              {endC}
                            </td>
                            <td style={{ padding: "6px 10px", fontSize: 11 }}>{winM}</td>
                            <td
                              style={{
                                padding: "6px 10px",
                                fontSize: 11,
                                fontWeight: betM !== "—" ? 600 : 400,
                                color:
                                  betM === "—"
                                    ? "var(--muted)"
                                    : strong
                                      ? "var(--up)"
                                      : "var(--down)",
                              }}
                            >
                              {betM}
                            </td>
                          </>
                        );
                      })()}
                      <td style={{ padding: "6px 10px", color: "var(--muted)" }}>
                        {t.gate ? String(t.gate) : "—"}
                        {t.min_left_sec != null ? (
                          <span style={{ color: "var(--muted)" }}>
                            {" "}
                            (
                            {Number(t.min_left_sec) > 0
                              ? `${(Number(t.min_left_sec) / 60).toFixed(2)} דק׳`
                              : `${Number(t.min_left_sec).toFixed(1)}s`}
                            )
                          </span>
                        ) : null}
                      </td>
                      <td style={{ padding: "6px 10px", color: "var(--muted)" }}>
                        {t.reason ? String(t.reason) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </div>
        );
      })}
      </div>
    </>
  );
}

/** מינ׳ חוזים אפקטיבי: max(הגדרת משתמש, מינ׳ השוק) — בלי תלות בשמות ישנים של state */
function computeEffectiveMinContracts(minContracts: number, orderMinSize: number | undefined): number {
  const oms = orderMinSize != null ? Math.ceil(orderMinSize) : 5;
  return Math.max(minContracts, oms);
}

const PRESETS = {
  simple: {
    name: "מתחיל פשוט",
    investment_usd: 5,
    entry_price_cents: 20,
    take_profit_pct: 20,
    dca_enabled: false,
    hedge_enabled: false,
    desc: "השקעה של 5 דולר לכניסה עד מחיר 20 סנט לחוזה (כ־25 חוזים, מינימום 5). יעד רווח 20%.",
  },
  dca: {
    name: "עם DCA",
    investment_usd: 20,
    entry_price_cents: 25,
    take_profit_pct: 15,
    dca_enabled: true,
    dca_slices: 4,
    dca_interval_sec: 30,
    hedge_enabled: false,
    desc: "השקעה של 20 דולר המחולקת לארבעה מקטעים במרווח של 30 שניות בין מקטע למקטע.",
  },
  hedge: {
    name: "גידור",
    investment_usd: 10,
    entry_price_cents: 30,
    hedge_enabled: true,
    hedge_combined_ask_max: 0.98,
    side_preference: "Up" as const,
    desc: "כניסה לכיוון Up; כאשר סכום שאלות הקנייה ל-Up ול-Down אינו עולה על 0.98 — מוצעת פתיחת רגל נוספת.",
  },
};

export default function App() {
  const [showOnboard, setShowOnboard] = useState(
    () => typeof localStorage !== "undefined" && !localStorage.getItem("pm_onboard_done")
  );
  const [liveMode, setLiveMode] = useState(false);
  /** מצב "כסף אמיתי" בפועל מצד המנוע (לאחר kill-switch/מפתח) — לצגת חסימה אם יש */
  const [liveModeEffective, setLiveModeEffective] = useState(false);
  const [liveModeBlockedReason, setLiveModeBlockedReason] = useState<string | null>(null);
  /** האם המפתח הנוכחי נשמר ל-Keychain (כלומר נטען אוטומטית בהרצות הבאות) */
  const [pkPersistedInKeychain, setPkPersistedInKeychain] = useState(false);
  /** Checkbox ב-UI: האם לשמור את המפתח לצמיתות כשלוחצים "שמור" */
  const [pkPersistChecked, setPkPersistChecked] = useState(true);
  const [tab, setTab] = useState<Tab>("dash");
  const [market, setMarket] = useState<Market | null>(null);
  const [btc, setBtc] = useState<{ price: number; history: { t: number; p: number }[] }>({
    price: 0,
    history: [],
  });
  const [demoState, setDemoState] = useState<Record<string, unknown>>({});
  /** יתרת USDC ב-CLOB (Polymarket) לפי המפתח הנוכחי — לא יתרת הסימולציה */
  const [pmClobAccount, setPmClobAccount] = useState<{
    ok: boolean;
    balance_usd: number | null;
    allowance_usd: number | null;
    address: string | null;
    funder_address?: string | null;
    is_proxy?: boolean;
    hint?: string;
    error?: string;
  } | null>(null);
  /** snapshot חי מלא: יתרה + פוזיציות אמיתיות + שווי נטו — נקרא רק כשליב פעיל */
  const [livePortfolio, setLivePortfolio] = useState<{
    ok: boolean;
    balance_usd: number | null;
    allowance_usd: number | null;
    equity_usd: number | null;
    address: string | null;
    funder_address?: string | null;
    is_proxy?: boolean;
    positions: {
      token_id: string;
      side: string;
      size: number;
      avg_price: number | null;
      mark_price: number | null;
      value_usd: number | null;
    }[];
    ts: number | null;
    error?: string;
    hint?: string;
  } | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  /** משוב קצר אחרי לחיצה על «העתק את כל היומן» */
  const [logJournalCopied, setLogJournalCopied] = useState(false);
  const [logEntries, setLogEntries] = useState<{ ts: number; msg: string; type: string; session_id?: string }[]>([]);
  const [pending, setPending] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState("");
  const [ob, setOb] = useState<OrderbookSummary | null>(null);
  const priceStream = usePriceStream();

  /** טיק שנייה — מונה «נותרו…» בלוח הבקרה מתעדכן כל שנייה, לא רק כשמגיע refresh מהשרת */
  const [clock, setClock] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setClock((c) => c + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  /** עוגן ל־seconds_left מהשרת; בין רענונים מפחיתים לפי שעון קיר (כמו LiveStreamTrade). */
  const windowSecondsLeftAnchorRef = useRef<{ left: number; atMs: number } | null>(null);
  useEffect(() => {
    if (!market) {
      windowSecondsLeftAnchorRef.current = null;
      return;
    }
    windowSecondsLeftAnchorRef.current = {
      left: market.seconds_left,
      atMs: Date.now(),
    };
  }, [market?.seconds_left, market?.epoch, market?.slug]);

  const effectiveWindowSecondsLeft = useMemo(() => {
    void clock;
    if (!market) return null;
    const a = windowSecondsLeftAnchorRef.current;
    if (!a) return market.seconds_left;
    const driftSec = Math.floor((Date.now() - a.atMs) / 1000);
    return Math.max(0, a.left - driftSec);
  }, [market, clock]);

  useEffect(() => {
    if (!priceStream.lastUpdateTs) return;
    setOb((prev) => {
      const base = prev ?? { slug: "", up: { bid: null, ask: null, mid: null }, down: { bid: null, ask: null, mid: null } };
      return {
        ...base,
        up: priceStream.up
          ? { bid: priceStream.up.bid, ask: priceStream.up.ask, mid: priceStream.up.mid }
          : base.up,
        down: priceStream.down
          ? { bid: priceStream.down.bid, ask: priceStream.down.ask, mid: priceStream.down.mid }
          : base.down,
      };
    });
  }, [priceStream.up?.bid, priceStream.up?.ask, priceStream.down?.bid, priceStream.down?.ask, priceStream.lastUpdateTs]);

  const [engineStatus, setEngineStatus] = useState("");
  const [engineLastTickTs, setEngineLastTickTs] = useState<number | null>(null);
  /** unix seconds — נקודת התחלה לטיימר זמן ריצה (מהשרת, מתוך config) */
  const [runtimeStartedTs, setRuntimeStartedTs] = useState<number | null>(null);
  const [runtimeDisplaySec, setRuntimeDisplaySec] = useState<number | null>(null);
  const [cfgDirty, setCfgDirty] = useState(false);
  const [saveFeedback, setSaveFeedback] = useState<"saved" | null>(null);
  const cfgDirtyRef = useRef(false);

  const [inv, setInv] = useState(5);
  const [entryCents, setEntryCents] = useState(20);
  const [tp, setTp] = useState(20);
  const [minMin, setMinMin] = useState(3);
  const [freezeMin, setFreezeMin] = useState(1);
  const [interBlock, setInterBlock] = useState(true);
  const [dca, setDca] = useState(false);
  const [dcaSlices, setDcaSlices] = useState(4);
  const [dcaInt, setDcaInt] = useState(30);
  const [dcaDiscountEnabled, setDcaDiscountEnabled] = useState(false);
  const [dcaDiscountPct, setDcaDiscountPct] = useState(2);
  const [hedge, setHedge] = useState(false);
  const [hedgeMax, setHedgeMax] = useState(0.98);
  const [side, setSide] = useState<"Up" | "Down" | "signal">("Up");
  const [botMode, setBotMode] = useState<"off" | "semi" | "auto">("off");
  const [requireApproval, setRequireApproval] = useState(true);
  const [autoReenter, setAutoReenter] = useState(true);
  const [reenterCooldown, setReenterCooldown] = useState(8);
  const [maxEntriesPerWindow, setMaxEntriesPerWindow] = useState(3);
  const [maxNotionalPerWindow, setMaxNotionalPerWindow] = useState(1_000_000);
  const [maxTradesPerHour, setMaxTradesPerHour] = useState(1_000);
  const [nearEntryPct, setNearEntryPct] = useState(3);
  const [nearTpPct, setNearTpPct] = useState(2);
  const [dcaTpOverridePct, setDcaTpOverridePct] = useState(50);
  /** 0 = כבוי; כל X שניות — שורת יומן עם Ask/Bid מ-Polymarket */
  const [bookLogIntervalSec, setBookLogIntervalSec] = useState(0);
  const [lossRecoveryEnabled, setLossRecoveryEnabled] = useState(false);
  const [lossRecoveryStepPct, setLossRecoveryStepPct] = useState(20);
  const [lossRecoveryEveryN, setLossRecoveryEveryN] = useState(1);
  const [lossRecoveryMaxMult, setLossRecoveryMaxMult] = useState(10);
  /** ביצוע: "limit" = GTC קלאסי (תאימות לאחור); "market" = FOK לכניסה, FAK+retry ליציאה */
  const [orderMode, setOrderMode] = useState<"limit" | "market">("limit");
  const [entrySlippagePct, setEntrySlippagePct] = useState(2);
  const [exitSlippagePct, setExitSlippagePct] = useState(5);
  const [peakWatchdogEnabled, setPeakWatchdogEnabled] = useState(true);
  const [peakRetreatExitPct, setPeakRetreatExitPct] = useState(2);
  const [retryMaxAttempts, setRetryMaxAttempts] = useState(3);
  const [holdToResolutionEnabled, setHoldToResolutionEnabled] = useState(false);
  const [holdToResolutionMinDcaSlices, setHoldToResolutionMinDcaSlices] = useState(2);
  const [holdToResolutionMinPrice, setHoldToResolutionMinPrice] = useState(0.85);
  const [holdToResolutionStopLoss, setHoldToResolutionStopLoss] = useState(true);
  const [investmentMode, setInvestmentMode] = useState<"fixed" | "percent">("fixed");
  const [investmentPctOfPortfolio, setInvestmentPctOfPortfolio] = useState(5);
  // Follow Last Winner (FLW)
  const [flwEnabled, setFlwEnabled] = useState(false);
  const [flwLookback, setFlwLookback] = useState(1);
  const [flwMode, setFlwMode] = useState<"forward" | "reverse">("forward");
  const [flwMinDrift, setFlwMinDrift] = useState(0);
  // תצוגת תוצאות חלון אחרון + תצוגה מקדימה של בחירת FLW
  const [lastWindowOutcome, setLastWindowOutcome] = useState<{
    last: { side_won?: string | null; btc_open?: number | null; btc_close?: number | null; drift_pct?: number | null; epoch?: number | null } | null;
    flw_preview: { side?: string | null; lookback?: number; mode?: string; min_drift_pct?: number; fallback_side_preference?: string; samples?: Array<{ epoch: number; side_won: string; btc_open?: number; btc_close?: number }> } | null;
  } | null>(null);
  /** מצב ריצה מהשרת (תמיד מסונכרן) */
  const [lossRecoveryStreak, setLossRecoveryStreak] = useState(0);
  const [lossRecoveryMultLive, setLossRecoveryMultLive] = useState(1);
  /** שוק Polymarket: חלון 5 או 15 דק׳ (לא מספר חוזים) */
  const [btcWindow, setBtcWindow] = useState<"5m" | "15m">("5m");
  /** מינ׳ חוזים — לפחות max(זה, מינ׳ השוק) */
  const [minContracts, setMinContracts] = useState(5);
  const [pk, setPk] = useState("");
  /** loading state לפעולות חד-פעמיות: מונע לחיצה כפולה */
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  useEffect(() => {
    cfgDirtyRef.current = cfgDirty;
  }, [cfgDirty]);

  useEffect(() => {
    if (runtimeStartedTs == null) {
      setRuntimeDisplaySec(null);
      return;
    }
    const tick = () => setRuntimeDisplaySec(Math.max(0, Date.now() / 1000 - runtimeStartedTs));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [runtimeStartedTs]);

  const markCfgDirty = useCallback(() => {
    cfgDirtyRef.current = true;
    setCfgDirty(true);
  }, []);

  const exchangeMinContractsCeil = useMemo(() => {
    if (market?.order_min_size == null) return undefined;
    return Math.ceil(market.order_min_size);
  }, [market?.order_min_size]);

  const effectiveMinContracts = useMemo(
    () => computeEffectiveMinContracts(minContracts, market?.order_min_size),
    [minContracts, market?.order_min_size]
  );

  /** מינימום Polymarket לשוק הנוכחי — לא נסחר מתחת (מסונכרן ל־min_contracts כשהשוק נטען) */
  useEffect(() => {
    if (exchangeMinContractsCeil == null) return;
    setMinContracts((prev) => Math.max(prev, exchangeMinContractsCeil));
  }, [market?.slug, exchangeMinContractsCeil]);

  const contracts = useMemo(() => {
    const price = entryCents / 100;
    if (price <= 0) return 0;
    const n = Math.floor(inv / price);
    return n >= effectiveMinContracts ? n : 0;
  }, [inv, entryCents, effectiveMinContracts]);

  const refreshInFlight = useRef(false);
  const refreshFailCount = useRef(0);
  const REFRESH_FAIL_THRESHOLD = 3; // show error only after N consecutive failures
  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    try {
      const [m, b, st, lg, pe, cfg, obSummary, logEnt, lm, pmClobRaw, lwo] = await Promise.all([
        api<Market>("/api/market/current", { timeoutMs: TIMEOUT_MS_MARKET_CURRENT }),
        api<{ price: number; history: { t: number; p: number }[] }>("/api/btc/live"),
        api<Record<string, unknown>>("/api/demo/state", { timeoutMs: TIMEOUT_MS_DEMO_STATE }),
        api<{ lines: string[] }>("/api/strategy/logs"),
        api<{ pending: unknown }>("/api/strategy/pending"),
        api<Record<string, unknown>>("/api/strategy/config"),
        api<OrderbookSummary>("/api/market/orderbook-summary", { timeoutMs: TIMEOUT_MS_ORDERBOOK_SUMMARY }),
        api<{ entries: { ts: number; msg: string; type: string; session_id?: string }[] }>("/api/strategy/log-entries").catch(() => ({ entries: [] })),
        api<{ enabled: boolean; effective: boolean; reason_blocked: string | null; persisted_in_keychain?: boolean }>("/api/live/mode").catch(() => null),
        api<{
          ok?: boolean;
          error?: string;
          balance_usd?: number | null;
          allowance_usd?: number | null;
          address?: string | null;
        }>("/api/live/polymarket-clob-account").catch(() => ({ ok: false, error: "לא ניתן לטעון" })),
        api<{
          last: { side_won?: string | null; btc_open?: number | null; btc_close?: number | null; drift_pct?: number | null; epoch?: number | null } | null;
          flw_preview: { side?: string | null; lookback?: number; mode?: string; min_drift_pct?: number; fallback_side_preference?: string; samples?: Array<{ epoch: number; side_won: string; btc_open?: number; btc_close?: number }> } | null;
        }>("/api/history/last-window-outcome").catch(() => null),
      ]);
      if (lm) {
        setLiveMode(Boolean(lm.enabled));
        setLiveModeEffective(Boolean(lm.effective));
        setLiveModeBlockedReason(lm.reason_blocked ?? null);
        setPkPersistedInKeychain(Boolean(lm.persisted_in_keychain));
      }
      {
        const p = pmClobRaw as Record<string, unknown>;
        setPmClobAccount({
          ok: Boolean(p.ok),
          balance_usd: typeof p.balance_usd === "number" && Number.isFinite(p.balance_usd) ? p.balance_usd : null,
          allowance_usd:
            typeof p.allowance_usd === "number" && Number.isFinite(p.allowance_usd) ? p.allowance_usd : null,
          address: typeof p.address === "string" ? p.address : null,
          funder_address: typeof p.funder_address === "string" ? p.funder_address : null,
          is_proxy: Boolean(p.is_proxy),
          hint: typeof p.hint === "string" ? p.hint : undefined,
          error: typeof p.error === "string" ? p.error : undefined,
        });
      }
      setMarket(m);
      setBtc(b);
      setDemoState(st);
      setLogs(lg.lines || []);
      setLogEntries((logEnt?.entries as { ts: number; msg: string; type: string; session_id?: string }[]) || []);
      setPending((pe.pending as Record<string, unknown>) || null);
      {
        const ur = cfg as Record<string, unknown>;
        if (typeof ur.ui_runtime_started_ts === "number") {
          setRuntimeStartedTs(ur.ui_runtime_started_ts);
        } else {
          setRuntimeStartedTs(null);
        }
      }
      {
        const m = cfg.mode as string | undefined;
        if (m === "off" || m === "semi" || m === "auto") {
          setBotMode(m);
          setRequireApproval(m === "semi");
        }
      }
      if (typeof (cfg as any).last_status === "string") setEngineStatus((cfg as any).last_status);
      if (typeof (cfg as any).last_tick_ts === "number") setEngineLastTickTs((cfg as any).last_tick_ts);
      {
        const lr = cfg as Record<string, unknown>;
        if (typeof lr.loss_recovery_streak === "number") setLossRecoveryStreak(lr.loss_recovery_streak);
        if (typeof lr.loss_recovery_multiplier === "number") setLossRecoveryMultLive(lr.loss_recovery_multiplier);
      }
      // חשוב: יש רענון כל שנייה. לא נדרוס ערכים שהמשתמש עורך לפני "שמור".
      // כשאין עריכה פתוחה — מסנכרנים את כל ההגדרות מהשרת (אחרת אחרי F5 חוזרים לברירות מחדל מקומיות).
      if (!cfgDirtyRef.current) {
        const c = cfg as Record<string, unknown>;
        if (typeof c.investment_usd === "number") setInv(c.investment_usd);
        if (typeof c.entry_price_cents === "number") setEntryCents(c.entry_price_cents);
        if (typeof c.take_profit_pct === "number") setTp(c.take_profit_pct);
        if (typeof c.min_minutes_for_entry === "number") setMinMin(c.min_minutes_for_entry);
        if (typeof c.freeze_last_minutes === "number") setFreezeMin(c.freeze_last_minutes);
        if (typeof c.intermediate_block_new_entries === "boolean") setInterBlock(c.intermediate_block_new_entries);
        if (typeof c.dca_enabled === "boolean") setDca(c.dca_enabled);
        if (typeof c.dca_slices === "number") setDcaSlices(c.dca_slices);
        if (typeof c.dca_interval_sec === "number") setDcaInt(c.dca_interval_sec);
        if (typeof c.dca_discount_enabled === "boolean") setDcaDiscountEnabled(c.dca_discount_enabled);
        if (typeof c.dca_discount_pct === "number") setDcaDiscountPct(c.dca_discount_pct);
        if (typeof c.hedge_enabled === "boolean") setHedge(c.hedge_enabled);
        if (typeof c.hedge_combined_ask_max === "number") setHedgeMax(c.hedge_combined_ask_max);
        const sp = c.side_preference;
        if (sp === "Up" || sp === "Down" || sp === "signal") setSide(sp);
        if (typeof c.auto_reenter_after_tp === "boolean") setAutoReenter(c.auto_reenter_after_tp);
        if (typeof c.reenter_cooldown_sec === "number") setReenterCooldown(c.reenter_cooldown_sec);
        if (typeof c.max_entries_per_window === "number") setMaxEntriesPerWindow(c.max_entries_per_window);
        if (typeof c.max_notional_per_window_usd === "number") setMaxNotionalPerWindow(c.max_notional_per_window_usd);
        if (typeof c.max_trades_per_hour === "number") setMaxTradesPerHour(c.max_trades_per_hour);
        if (typeof c.near_entry_pct === "number") setNearEntryPct(c.near_entry_pct);
        if (typeof c.near_tp_pct === "number") setNearTpPct(c.near_tp_pct);
        if (typeof c.dca_tp_override_pct === "number") setDcaTpOverridePct(c.dca_tp_override_pct);
        if (typeof c.book_log_interval_sec === "number") setBookLogIntervalSec(c.book_log_interval_sec);
        const bw = c.btc_window;
        if (bw === "5m" || bw === "15m") setBtcWindow(bw);
        if (typeof c.min_contracts === "number") {
          const floor =
            typeof m.order_min_size === "number" ? Math.ceil(m.order_min_size) : c.min_contracts;
          setMinContracts(Math.max(c.min_contracts, floor));
        }
        if (typeof c.loss_recovery_enabled === "boolean") setLossRecoveryEnabled(c.loss_recovery_enabled);
        if (typeof c.loss_recovery_step_pct === "number") setLossRecoveryStepPct(c.loss_recovery_step_pct);
        if (typeof c.loss_recovery_every_n_losses === "number") setLossRecoveryEveryN(c.loss_recovery_every_n_losses);
        if (typeof c.loss_recovery_max_multiplier === "number") setLossRecoveryMaxMult(c.loss_recovery_max_multiplier);
        if (c.order_mode === "limit" || c.order_mode === "market") setOrderMode(c.order_mode);
        if (typeof c.entry_slippage_pct === "number") setEntrySlippagePct(c.entry_slippage_pct);
        if (typeof c.exit_slippage_pct === "number") setExitSlippagePct(c.exit_slippage_pct);
        if (typeof c.peak_watchdog_enabled === "boolean") setPeakWatchdogEnabled(c.peak_watchdog_enabled);
        if (typeof c.peak_retreat_exit_pct === "number") setPeakRetreatExitPct(c.peak_retreat_exit_pct);
        if (typeof c.retry_max_attempts === "number") setRetryMaxAttempts(c.retry_max_attempts);
        if (typeof c.hold_to_resolution_enabled === "boolean") setHoldToResolutionEnabled(c.hold_to_resolution_enabled);
        if (typeof c.hold_to_resolution_min_dca_slices === "number") setHoldToResolutionMinDcaSlices(c.hold_to_resolution_min_dca_slices);
        if (typeof c.hold_to_resolution_min_price === "number") setHoldToResolutionMinPrice(c.hold_to_resolution_min_price);
        if (typeof c.hold_to_resolution_stop_loss_enabled === "boolean") setHoldToResolutionStopLoss(c.hold_to_resolution_stop_loss_enabled);
        if (c.investment_mode === "fixed" || c.investment_mode === "percent") setInvestmentMode(c.investment_mode);
        if (typeof c.investment_pct_of_portfolio === "number") setInvestmentPctOfPortfolio(c.investment_pct_of_portfolio);
        if (typeof c.follow_last_winner_enabled === "boolean") setFlwEnabled(c.follow_last_winner_enabled);
        if (typeof c.follow_last_winner_lookback === "number") setFlwLookback(c.follow_last_winner_lookback);
        if (c.follow_last_winner_mode === "forward" || c.follow_last_winner_mode === "reverse") setFlwMode(c.follow_last_winner_mode);
        if (typeof c.follow_last_winner_min_btc_drift_pct === "number") setFlwMinDrift(c.follow_last_winner_min_btc_drift_pct);
      }
      setOb(obSummary);
      if (lwo) setLastWindowOutcome(lwo as typeof lastWindowOutcome);
      refreshFailCount.current = 0;
      setErr("");
    } catch (e: unknown) {
      refreshFailCount.current += 1;
      if (refreshFailCount.current >= REFRESH_FAIL_THRESHOLD) {
        setErr(
          e instanceof Error
            ? e.message
            : "שגיאת רשת. יש לוודא שהמנוע פעיל (למשל: npm run engine מתוך תיקיית הפרויקט).",
        );
      }
    } finally {
      refreshInFlight.current = false;
    }
  }, []);

  const hasOpenDemoPositions = useMemo(() => {
    const p = (demoState as { positions?: unknown[] }).positions;
    return Array.isArray(p) && p.length > 0;
  }, [demoState]);

  /** עדכון דינמי בלי לאפס interval (הימנעות מבזק כפול של refresh + בקשות כפולות בלוג) */
  const hasOpenDemoPositionsRef = useRef(hasOpenDemoPositions);
  useEffect(() => {
    hasOpenDemoPositionsRef.current = hasOpenDemoPositions;
  }, [hasOpenDemoPositions]);

  useEffect(() => {
    let cancelled = false;
    const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
    (async () => {
      while (!cancelled) {
        if (!isPageHidden()) await refresh();
        if (cancelled) break;
        const ms = isPageHidden() ? 10_000 : hasOpenDemoPositionsRef.current ? 800 : 1500;
        await sleep(ms);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  /** Fast 500ms snapshot poll — רק balance + last_mark + positions לעדכון P&L מהיר */
  useEffect(() => {
    let cancelled = false;
    const pollSnapshot = async () => {
      if (cancelled || isPageHidden()) return;
      try {
        const snap = await api<Record<string, unknown>>("/api/demo/snapshot");
        if (!cancelled) {
          setDemoState((prev) => ({
            ...prev,
            balance_usd: snap.balance_usd,
            positions: snap.positions ?? (prev as any).positions,
            last_mark: snap.last_mark ?? (prev as any).last_mark,
            bot_run_started_ts: snap.bot_run_started_ts,
            bot_run_equity_baseline_usd: snap.bot_run_equity_baseline_usd,
            ui_runtime_equity_baseline_usd: snap.ui_runtime_equity_baseline_usd,
          }));
        }
      } catch {
        // silent — full refresh will recover
      }
    };
    const id = window.setInterval(pollSnapshot, 250);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  /** Polling נפרד לתיק חי מ-Polymarket: רק כשמצב לייב "effective" (מפתח, דגל ו-kill-switch). */
  useEffect(() => {
    if (!liveModeEffective) {
      setLivePortfolio(null);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      if (isPageHidden()) return;
      try {
        const p = await api<typeof livePortfolio>("/api/live/portfolio");
        if (!cancelled) setLivePortfolio(p);
      } catch {
        // rate-limited / offline — נשאיר את הקודם
      }
    };
    void poll();
    const id = window.setInterval(poll, 3500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [liveModeEffective]);

  const pushConfig = async () => {
    try {
      await api("/api/strategy/config", {
        method: "POST",
        body: JSON.stringify({
          investment_usd: inv,
          entry_price_cents: entryCents,
          min_contracts: minContracts,
          btc_window: btcWindow,
          take_profit_pct: tp,
          min_minutes_for_entry: minMin,
          freeze_last_minutes: freezeMin,
          intermediate_block_new_entries: interBlock,
          dca_enabled: dca,
          dca_slices: dcaSlices,
          dca_interval_sec: dcaInt,
          dca_discount_enabled: dcaDiscountEnabled,
          dca_discount_pct: dcaDiscountPct,
          hedge_enabled: hedge,
          hedge_combined_ask_max: hedgeMax,
          side_preference: side,
          auto_reenter_after_tp: autoReenter,
          reenter_cooldown_sec: reenterCooldown,
          max_entries_per_window: maxEntriesPerWindow,
          max_notional_per_window_usd: maxNotionalPerWindow,
          max_trades_per_hour: maxTradesPerHour,
          near_entry_pct: nearEntryPct,
          near_tp_pct: nearTpPct,
          dca_tp_override_pct: dcaTpOverridePct,
          book_log_interval_sec: bookLogIntervalSec,
          loss_recovery_enabled: lossRecoveryEnabled,
          loss_recovery_step_pct: lossRecoveryStepPct,
          loss_recovery_every_n_losses: Math.max(1, Math.floor(lossRecoveryEveryN)),
          loss_recovery_max_multiplier: Math.max(1, lossRecoveryMaxMult),
          order_mode: orderMode,
          entry_slippage_pct: Math.max(0, entrySlippagePct),
          exit_slippage_pct: Math.max(0, exitSlippagePct),
          peak_watchdog_enabled: peakWatchdogEnabled,
          peak_retreat_exit_pct: Math.max(0, peakRetreatExitPct),
          retry_max_attempts: Math.max(0, Math.floor(retryMaxAttempts)),
          hold_to_resolution_enabled: holdToResolutionEnabled,
          hold_to_resolution_min_dca_slices: Math.max(0, Math.floor(holdToResolutionMinDcaSlices)),
          hold_to_resolution_min_price: Math.max(0, Math.min(1, holdToResolutionMinPrice)),
          hold_to_resolution_stop_loss_enabled: holdToResolutionStopLoss,
          investment_mode: investmentMode,
          investment_pct_of_portfolio: Math.max(0, investmentPctOfPortfolio),
          follow_last_winner_enabled: flwEnabled,
          follow_last_winner_lookback: Math.max(1, Math.min(5, Math.floor(flwLookback))),
          follow_last_winner_mode: flwMode,
          follow_last_winner_min_btc_drift_pct: Math.max(0, Math.min(10, flwMinDrift)),
        }),
      });
      cfgDirtyRef.current = false;
      setCfgDirty(false);
      setSaveFeedback("saved");
      setTimeout(() => setSaveFeedback(null), 3000);
      void refresh();
    } catch {
      // silent — auto-save will retry on next config change
    }
  };

  /** Auto-save: אם המשתמש ערך ערך בלשונית אסטרטגיה — שמור אוטומטית 1.5s אחרי הקלדה אחרונה
   *  (debounce). כך אין תלות בלחיצה ידנית על "שמור הגדרות" — אם סוגרים את הבוט/דפדפן
   *  תוך כדי עריכה, הערך האחרון כבר נשמר ל-config_persisted.json.
   */
  const pushConfigRef = useRef(pushConfig);
  useEffect(() => { pushConfigRef.current = pushConfig; });
  useEffect(() => {
    if (!cfgDirty) return;
    const id = window.setTimeout(() => {
      void pushConfigRef.current().catch(() => { /* שקט — נסיון חוזר בשינוי הבא */ });
    }, 1500);
    return () => window.clearTimeout(id);
  }, [cfgDirty, inv, entryCents, minContracts, btcWindow, tp, minMin, freezeMin, interBlock,
      dca, dcaSlices, dcaInt, dcaDiscountEnabled, dcaDiscountPct, hedge, hedgeMax, side,
      autoReenter, reenterCooldown, maxEntriesPerWindow, maxNotionalPerWindow, maxTradesPerHour,
      nearEntryPct, nearTpPct, dcaTpOverridePct, bookLogIntervalSec,
      lossRecoveryEnabled, lossRecoveryStepPct, lossRecoveryEveryN, lossRecoveryMaxMult,
      orderMode, entrySlippagePct, exitSlippagePct, peakWatchdogEnabled, peakRetreatExitPct,
      retryMaxAttempts, holdToResolutionEnabled, holdToResolutionMinDcaSlices,
      holdToResolutionMinPrice, holdToResolutionStopLoss,
      investmentMode, investmentPctOfPortfolio,
      flwEnabled, flwLookback, flwMode, flwMinDrift]);

  const setMode = (m: "off" | "semi" | "auto") => {
    const prevMode = botMode;
    const prevRequireApproval = requireApproval;
    setBotMode(m);
    setRequireApproval(m === "semi");
    void (async () => {
      try {
        await api("/api/strategy/mode", { method: "POST", body: JSON.stringify({ mode: m }) });
        void refresh();
      } catch {
        setBotMode(prevMode);
        setRequireApproval(prevRequireApproval);
      }
    })();
  };

  const chartData = useMemo(() => {
    return (btc.history || []).map((x, i) => {
      const ts = Number(x.t);
      return {
        i,
        p: x.p,
        ts,
        t: new Date(ts * 1000).toLocaleTimeString("he-IL"),
      };
    });
  }, [btc.history]);

  const btcChartYDomain = useMemo(
    () => computeBtcPriceChartYDomain(
      chartData.map((d) => Number(d.p)),
      market?.price_to_beat ?? null,
    ),
    [chartData, market?.price_to_beat],
  );

  const cumPnlChartData = useMemo(() => {
    const rawTrades = ((demoState as any).trades as Trade[]) || [];
    const trades = tradesForSessionStats(rawTrades, demoState as Record<string, unknown> | null);
    const realizedTrades = trades.filter(
      (t) =>
        !isReconcileLedgerEntry(t) &&
        t.realized_pnl != null &&
        !Number.isNaN(Number(t.realized_pnl)),
    );
    // חשוב: חישוב PnL מצטבר חייב להיות כרונולוגי לפי ts.
    // אם לא ממיינים, Recharts תחבר נקודות לפי ts אבל cum יחושב בסדר אחר => גרף "קופץ".
    // בנוסף: אם ts לא קיים/0, נזרוק כדי לא “להפיל” נקודות מוקדם מדי על הציר.
    const withValidTs = realizedTrades
      .map((t) => ({ t, tsSec: Number(t.ts) }))
      .filter((x) => Number.isFinite(x.tsSec) && x.tsSec > 0);

    withValidTs.sort((a, b) => a.tsSec - b.tsSec);

    let cum = 0;
    let lastTs: number | null = null;
    const out: { i: number; pnl: number; ts: number; t: string }[] = [];

    for (let i = 0; i < withValidTs.length; i++) {
      const t = withValidTs[i].t;
      const tsSec = withValidTs[i].tsSec;
      cum += Number(t.realized_pnl || 0);

      if (lastTs != null && Math.abs(lastTs - tsSec) < 1e-9) {
        // כמה אירועים באותו ts — עדכן את אותה נקודה כדי למנוע "קפיצות" כפולות.
        out[out.length - 1].pnl = cum;
        continue;
      }

      lastTs = tsSec;
      out.push({
        i,
        pnl: cum,
        ts: tsSec,
        t: new Date(tsSec * 1000).toLocaleTimeString("he-IL"),
      });
    }

    return out;
  }, [demoState]);

  const cumPnlChartDataLive = useMemo(() => {
    const rawTrades = ((demoState as any).trades as Trade[]) || [];
    const sessionTrades = tradesForSessionStats(rawTrades, demoState as Record<string, unknown> | null);
    const trades = tradesLiveOnly(sessionTrades);
    const realizedTrades = trades.filter(
      (t) =>
        !isReconcileLedgerEntry(t) &&
        !isShadowWindowSettlementTrade(t) &&
        t.realized_pnl != null &&
        !Number.isNaN(Number(t.realized_pnl)),
    );
    const withValidTs = realizedTrades
      .map((t) => ({ t, tsSec: Number(t.ts) }))
      .filter((x) => Number.isFinite(x.tsSec) && x.tsSec > 0);
    withValidTs.sort((a, b) => a.tsSec - b.tsSec);
    let cum = 0;
    let lastTs: number | null = null;
    const out: { i: number; pnl: number; ts: number; t: string }[] = [];
    for (let i = 0; i < withValidTs.length; i++) {
      const t = withValidTs[i].t;
      const tsSec = withValidTs[i].tsSec;
      cum += Number(t.realized_pnl || 0);
      if (lastTs != null && Math.abs(lastTs - tsSec) < 1e-9) {
        out[out.length - 1].pnl = cum;
        continue;
      }
      lastTs = tsSec;
      out.push({
        i,
        pnl: cum,
        ts: tsSec,
        t: new Date(tsSec * 1000).toLocaleTimeString("he-IL"),
      });
    }
    return out;
  }, [demoState]);

  const cumPnlLast = cumPnlChartData.length ? cumPnlChartData[cumPnlChartData.length - 1].pnl : undefined;
  const cumPnlAnim = useChartAnimationGate(cumPnlChartData.length, cumPnlLast, { epsilon: 0.005 });

  const cumPnlLastLive = cumPnlChartDataLive.length
    ? cumPnlChartDataLive[cumPnlChartDataLive.length - 1].pnl
    : undefined;
  const cumPnlAnimLive = useChartAnimationGate(cumPnlChartDataLive.length, cumPnlLastLive, { epsilon: 0.005 });

  const diff =
    market?.price_to_beat != null && btc.price
      ? btc.price - market.price_to_beat
      : null;

  return (
    <div className="app-shell">
      {showOnboard && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.85)",
            zIndex: 1000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 24,
          }}
        >
          <div style={{ background: "var(--card)", maxWidth: 480, padding: 24, borderRadius: 16 }}>
            <h2 style={{ marginTop: 0 }}>ברוכים הבאים</h2>
            <ol style={{ paddingRight: 20, lineHeight: 1.8 }}>
              <li>
                <strong>מצב סימולציה</strong>: מסחר מדומה מול נתוני שוק בפועל, ללא חשיפה כספית.
              </li>
              <li>
                <strong>חלון זמן 5 או 15 דקות</strong>: ניתן לבחור בהגדרות האסטרטגיה; המערכת תעבור בין השווקים בהתאם.
              </li>
              <li>
                מומלץ לפתוח את הלשונית <strong>אסטרטגיה</strong>, לטעון את הפריסט &quot;מתחיל פשוט&quot;, לשמור את ההגדרות ולבחור מצב הפעלה (חצי־אוטומטי או אוטומטי מלא).
              </li>
            </ol>
            <Button
              variant="primary"
              style={{ width: "100%", padding: "12px 24px" }}
              onClick={() => {
                localStorage.setItem("pm_onboard_done", "1");
                setShowOnboard(false);
              }}
            >
              הבנתי — המשך
            </Button>
          </div>
        </div>
      )}
      <header className="app-header">
        <h1 className="app-title">Polymarket BTC — מסחר Up/Down · גרסה 3</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className={`badge-mode ${liveMode ? "badge-mode--live" : "badge-mode--demo"}`}>
            {liveMode ? (liveModeEffective ? "מסחר חי" : "מסחר חי (חסום)") : "סימולציה"}
          </span>
          <Button
            variant="primary"
            className="header-mode-btn"
            data-live={liveMode ? "true" : "false"}
            onClick={async () => {
              const next = !liveMode;
              if (
                next &&
                !confirm("האם לעבור למסחר חי? פעולה זו כרוכה בסיכון כספי ודורשת אחריות.")
              )
                return;
              // עדכון אופטימיסטי כדי שהכפתור יגיב מיד; הערך הסופי מגיע מ-refresh הבא.
              setLiveMode(next);
              try {
                const r = await api<{
                  ok?: boolean;
                  enabled?: boolean;
                  effective?: boolean;
                  reason_blocked?: string | null;
                }>("/api/live/mode", {
                  method: "POST",
                  body: JSON.stringify({ enabled: next }),
                });
                if (r) {
                  if (typeof r.enabled === "boolean") setLiveMode(r.enabled);
                  if (typeof r.effective === "boolean") setLiveModeEffective(r.effective);
                  setLiveModeBlockedReason(r.reason_blocked ?? null);
                }
              } catch (e) {
                // במקרה של כשל, חוזרים למצב קודם
                setLiveMode(!next);
                alert(e instanceof Error ? e.message : "כשל בעדכון מצב כסף אמיתי");
              }
            }}
          >
            {liveMode ? "חזרה לסימולציה" : "מעבר למסחר חי"}
          </Button>
        </div>
      </header>

      {liveMode && (
        <div className="live-banner">
          <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 8 }}>
            מצב "כסף אמיתי" נשלט מהכפתור בראש המסך — אין צורך לערוך <code>.env</code>.{" "}
            <code>POLYMARKET_LIVE=0</code> משמש רק כ-kill-switch ברמת שרת/פריסה ולא מפעיל לייב.
            {liveModeBlockedReason && (
              <div style={{ color: "var(--down, #e24)", marginTop: 4 }}>
                ⚠ לייב חסום כרגע: {liveModeBlockedReason}
              </div>
            )}
          </div>
          <strong>מפתח פרטי:</strong>
          {pkPersistedInKeychain && (
            <span
              style={{
                marginInlineStart: 8,
                padding: "2px 8px",
                borderRadius: 10,
                fontSize: 11,
                background: "rgba(34,197,94,0.18)",
                color: "#4ade80",
                border: "1px solid rgba(34,197,94,0.35)",
              }}
            >
              נטען אוטומטית מ-Keychain
            </span>
          )}
          <input
            type="password"
            style={{
              width: "100%",
              maxWidth: 480,
              marginTop: 8,
              padding: 8,
              borderRadius: 6,
              border: "1px solid #666",
              background: "#1a1a1a",
              color: "#fff",
            }}
            placeholder={pkPersistedInKeychain ? "(מפתח כבר שמור — השאר ריק אם אין שינוי)" : "0x..."}
            value={pk}
            onChange={(e) => setPk(e.target.value)}
          />
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginTop: 8, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={pkPersistChecked}
              onChange={(e) => setPkPersistChecked(e.target.checked)}
            />
            שמור לצמיתות ב-Keychain של המחשב הזה
          </label>
          <div style={{ marginTop: 8 }}>
            <button
              type="button"
              style={{ marginInlineEnd: 8, padding: "6px 12px" }}
              onClick={async () => {
                try {
                  const r = await api<{
                    py_clob_client_installed?: boolean;
                    persisted?: boolean;
                    persist_requested?: boolean;
                  }>("/api/live/private-key", {
                    method: "POST",
                    body: JSON.stringify({ key: pk, persist: pkPersistChecked }),
                  });
                  if (r?.py_clob_client_installed === false) {
                    alert(
                      "המפתח נשמר.\n\nחבילת המסחר (py-clob-client) לא נמצאה בפייתון שמריץ את המנוע.\nבטרמינל: cd engine && python3 -m pip install -r requirements.txt\nואז הפעל מחדש את המנוע (npm run engine).",
                    );
                  } else if (pkPersistChecked && r?.persisted) {
                    alert(
                      "המפתח נשמר ל-Keychain של המחשב הזה. אין צורך להקליד אותו שוב בהרצות הבאות.\nשים לב: אבדן המחשב = סיכון לכסף.",
                    );
                  } else if (pkPersistChecked && !r?.persisted) {
                    alert(
                      "המפתח נשמר לסשן בלבד — שמירה קבועה ל-Keychain נכשלה.\nהתקן את ספריית keyring בפייתון של המנוע (pip install -r engine/requirements.txt) והפעל מחדש.",
                    );
                  } else {
                    alert(
                      "המפתח נשמר לסשן הנוכחי. אפשר להפעיל מסחר חי מהכפתור למעלה (אם אינו חסום).",
                    );
                  }
                  setPk("");
                  await refresh();
                } catch (e) {
                  alert(`שמירת המפתח נכשלה: ${e instanceof Error ? e.message : String(e)}`);
                }
              }}
            >
              שמור מפתח
            </button>
            {pkPersistedInKeychain && (
              <button
                type="button"
                style={{ padding: "6px 12px" }}
                onClick={async () => {
                  if (
                    !confirm(
                      "למחוק את המפתח השמור ב-Keychain? לאחר מכן תצטרך להקליד מפתח שוב כדי לסחור חי.",
                    )
                  ) {
                    return;
                  }
                  try {
                    await api("/api/live/private-key", { method: "DELETE" });
                    setPk("");
                    await refresh();
                  } catch (e) {
                    alert(`מחיקת המפתח נכשלה: ${e instanceof Error ? e.message : String(e)}`);
                  }
                }}
              >
                מחק מפתח שמור
              </button>
            )}
          </div>
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8, lineHeight: 1.45 }}>
            המפתח נשמר רק במחשב המקומי דרך Keychain/Secret Service/Credential Manager של מערכת ההפעלה —{" "}
            לא נשלח לשום שרת ולא נכתב ללוגים. אבדן המחשב = סיכון לכסף.
          </div>
        </div>
      )}

      <nav className="app-nav" role="tablist" aria-label="ניווט ראשי">
        {(
          [
            ["dash", "לוח בקרה"],
            ["strategy", "אסטרטגיה"],
            ["signals", "📡 סיגנלים"],
            ["trigger", "⚡ מסחר מהיר"],
            ["stats", "סטטיסטיקה (דמו)"],
            ["stats_live", "סטטיסטיקה לייב"],
            ["tips_v2", "ניתוח v3"],
            ["analytics_v3", "📊 אנליטיקס V3"],
            ["help", "עזרה ותיעוד"],
          ] as const
        ).map(([k, l]) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={tab === k}
            data-active={tab === k ? "true" : "false"}
            className="tab-btn"
            onClick={() => setTab(k)}
          >
            {l}
          </button>
        ))}
      </nav>

      {err && (
        <div className="alert-error" role="alert">
          {err}
        </div>
      )}

      <main id="main-content">
      {tab === "dash" && (
        <>
          {market && (
            <Card padding="md" style={{ marginBottom: "var(--s-4)" }}>
              <div style={{ color: "var(--muted)", fontSize: 14 }}>{market.title}</div>
              <div style={{ fontSize: 13, marginTop: 4 }}>
                נותרו {Math.floor((effectiveWindowSecondsLeft ?? market.seconds_left) / 60)}:
                {String(Math.floor((effectiveWindowSecondsLeft ?? market.seconds_left) % 60)).padStart(2, "0")} עד סיום החלון · אורך החלון{" "}
                {market.window_sec != null
                  ? `${Math.round(market.window_sec / 60)} דקות`
                  : "—"}{" "}
                · מינ׳ Polymarket (CLOB) {market.order_min_size} חוזים
                {market.order_min_size_source === "gamma" ? " — מטא־דאטה Gamma (CLOB לא זמין)" : ""}
                {" · "}
                <span style={{ color: priceStream.connected ? "#10b981" : "#ef4444", fontWeight: 600, fontSize: 11 }}>
                  {priceStream.connected ? "⚡ WS Live" : "⏳ WS מתחבר…"}
                </span>
              </div>
              <p style={{ fontSize: 14, marginTop: 12 }}>
                <strong>מחיר יעד לפתיחת החלון:</strong>{" "}
                {market.price_to_beat != null
                  ? `$${market.price_to_beat.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
                  : "טוען…"}{" "}
                <span style={{ color: "var(--muted)", fontSize: 12 }}>({market.price_to_beat_note})</span>
              </p>
              {market.polymarket_resolution_source ? (
                <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
                  מקור הרזולוציה ב-Polymarket (Gamma API):{" "}
                  <a
                    href={market.polymarket_resolution_source}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: "var(--accent-bright)" }}
                  >
                    {market.polymarket_resolution_source}
                  </a>{" "}
                  — ב-API אין שדה למחיר הדולר; המספר מחושב אצלנו מ-Chainlink/Binance.
                </p>
              ) : null}
              <p>
                <strong>מחיר BTC נוכחי:</strong> $
                {btc.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}{" "}
                {diff != null && (
                  <span style={{ color: diff >= 0 ? "var(--up)" : "var(--down)" }}>
                    ({diff >= 0 ? "↑" : "↓"} {Math.abs(diff).toFixed(2)}$ ממחיר הפתיחה)
                  </span>
                )}
              </p>
              <p style={{ fontSize: 13, color: "var(--muted)" }}>
                כיוון Up מנצח אם במועד סיום החלון מחיר הייחוס Chainlink BTC/USD אינו נמוך ממחיר פתיחת החלון.
              </p>
              {ob && (
                <div style={{ display: "flex", gap: 24, marginTop: 12, fontSize: 14 }}>
                  <span>
                    Up חוזה (mid):{" "}
                    <strong>
                      {ob.up.mid != null ? `${(ob.up.mid * 100).toFixed(1)}¢` : "—"}
                    </strong>
                    {ob.up.bid != null && ob.up.ask != null && (
                      <span style={{ color: "var(--muted)", fontSize: 12 }}>
                        {" "}
                        (bid {(ob.up.bid * 100).toFixed(1)}¢ / ask{" "}
                        {(ob.up.ask * 100).toFixed(1)}¢)
                      </span>
                    )}
                  </span>
                  <span>
                    Down חוזה (mid):{" "}
                    <strong>
                      {ob.down.mid != null ? `${(ob.down.mid * 100).toFixed(1)}¢` : "—"}
                    </strong>
                    {ob.down.bid != null && ob.down.ask != null && (
                      <span style={{ color: "var(--muted)", fontSize: 12 }}>
                        {" "}
                        (bid {(ob.down.bid * 100).toFixed(1)}¢ / ask{" "}
                        {(ob.down.ask * 100).toFixed(1)}¢)
                      </span>
                    )}
                  </span>
                </div>
              )}
              {/* תוצאת חלון קודם — לתצוגה ולפיצ'ר Follow Last Winner */}
              {lastWindowOutcome?.last && lastWindowOutcome.last.side_won && (
                <div style={{ marginTop: 10, fontSize: 12, color: "var(--muted)", lineHeight: 1.5 }}>
                  <strong>חלון קודם:</strong>{" "}
                  <span
                    style={{
                      color: lastWindowOutcome.last.side_won === "Up" ? "var(--up)" : "var(--down)",
                      fontWeight: 600,
                    }}
                  >
                    {lastWindowOutcome.last.side_won} ניצח
                  </span>
                  {lastWindowOutcome.last.btc_open != null && lastWindowOutcome.last.btc_close != null && (
                    <>
                      {" · "}BTC ${Number(lastWindowOutcome.last.btc_open).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      {" → "}${Number(lastWindowOutcome.last.btc_close).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      {lastWindowOutcome.last.drift_pct != null && (
                        <span
                          style={{
                            color:
                              (lastWindowOutcome.last.drift_pct ?? 0) >= 0 ? "var(--up)" : "var(--down)",
                            marginRight: 4,
                          }}
                        >
                          {" "}({(lastWindowOutcome.last.drift_pct as number) >= 0 ? "+" : ""}
                          {(lastWindowOutcome.last.drift_pct as number).toFixed(3)}%)
                        </span>
                      )}
                    </>
                  )}
                  {flwEnabled && lastWindowOutcome?.flw_preview?.side && (
                    <span style={{ marginRight: 8 }}>
                      {" · "}FLW יבחר:{" "}
                      <strong
                        style={{
                          color:
                            lastWindowOutcome.flw_preview.side === "Up" ? "var(--up)" : "var(--down)",
                        }}
                      >
                        {lastWindowOutcome.flw_preview.side}
                      </strong>
                    </span>
                  )}
                </div>
              )}
            </Card>
          )}

          <ChartCard
            title="מחיר BTC"
            subtitle="דגימות אחרונות במחשב המקומי; יעד הרזולוציה: Chainlink BTC/USD"
            height={280}
          >
            <div style={{ width: "100%", height: 220 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                  <XAxis
                    dataKey="ts"
                    type="number"
                    domain={["dataMin", "dataMax"]}
                    tick={{ ...chartAxisTick, fontSize: 10 }}
                    tickFormatter={(v) => formatPnlAxisTime(Number(v))}
                    allowDecimals
                  />
                  <YAxis
                    domain={btcChartYDomain ?? (["auto", "auto"] as const)}
                    tick={{ ...chartAxisTick, fontSize: 10 }}
                    width={72}
                    tickFormatter={(v) =>
                      typeof v === "number" && Number.isFinite(v)
                        ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
                        : ""
                    }
                  />
                  <Tooltip
                    contentStyle={chartTooltipStyle}
                    labelStyle={{ color: "var(--text-secondary)" }}
                    itemStyle={{ color: "var(--text)" }}
                    labelFormatter={(label) =>
                      Number.isFinite(Number(label)) ? formatPnlAxisTime(Number(label)) : String(label)
                    }
                    formatter={(value) => [
                      typeof value === "number" && Number.isFinite(value)
                        ? `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                        : String(value),
                      "מחיר",
                    ]}
                  />
                  {market?.price_to_beat != null && (
                    <ReferenceLine
                      y={market.price_to_beat}
                      stroke="var(--accent-bright)"
                      strokeOpacity={0.65}
                      strokeDasharray="4 4"
                      label={{ value: "מחיר יעד לפתיחת החלון", fill: "var(--text-secondary)", fontSize: 11 }}
                    />
                  )}
                  <Line
                    type="linear"
                    dataKey="p"
                    stroke="var(--chart-line-primary)"
                    dot={false}
                    strokeWidth={chartStroke.width}
                    strokeLinecap={chartStroke.linecap}
                    strokeLinejoin={chartStroke.linejoin}
                    isAnimationActive
                    animationDuration={380}
                    animationEasing="ease-out"
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </ChartCard>

          <div style={{ marginTop: 16, display: "flex", gap: 12, flexWrap: "wrap", alignItems: "stretch" }}>
            <Card padding="md" style={{ flex: 1, minWidth: 200 }}>
              {liveModeEffective && livePortfolio && livePortfolio.ok === false ? (
                <>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>
                    Polymarket — תיק חי (שגיאת טעינה){" "}
                    <span
                      style={{
                        marginInlineStart: 6,
                        padding: "1px 6px",
                        borderRadius: 8,
                        fontSize: 10,
                        background: "rgba(248, 113, 113, 0.2)",
                        color: "#f87171",
                        border: "1px solid rgba(248, 113, 113, 0.45)",
                      }}
                    >
                      LIVE
                    </span>
                  </div>
                  <div style={{ fontSize: 13, color: "#f87171", lineHeight: 1.45, marginBottom: 10 }}>
                    {livePortfolio.error ?? "לא ניתן למשוך את תיק ה-CLOB. בדקו מפתח, py-clob-client וחיבור."}
                  </div>
                  {pmClobAccount?.ok && pmClobAccount.balance_usd != null && (
                    <div style={{ fontSize: 13, marginBottom: 10 }}>
                      יתרת USDC ב-CLOB (אומתה ישירות):{" "}
                      <strong className="tabular-nums" style={pmClobAccount.balance_usd === 0 ? { color: "#f87171" } : undefined}>
                        ${pmClobAccount.balance_usd.toFixed(2)}
                      </strong>
                      {pmClobAccount.allowance_usd != null && (
                        <span style={{ marginInlineStart: 10, fontSize: 11, color: "var(--muted)" }}>
                          Allowance: ${pmClobAccount.allowance_usd.toFixed(2)}
                        </span>
                      )}
                    </div>
                  )}
                  {pmClobAccount?.is_proxy && pmClobAccount.funder_address ? (
                    <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4, wordBreak: "break-all", lineHeight: 1.35 }}>
                      Funder (proxy): {pmClobAccount.funder_address}
                      <br />
                      Signer: {pmClobAccount.address}
                    </div>
                  ) : pmClobAccount?.address ? (
                    <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4, wordBreak: "break-all", lineHeight: 1.35 }}>
                      כתובת חותם: {pmClobAccount.address}
                    </div>
                  ) : null}
                  {pmClobAccount?.hint && (
                    <div style={{
                      marginTop: 10,
                      padding: "10px 12px",
                      borderRadius: 6,
                      background: "rgba(250, 204, 21, 0.15)",
                      border: "1px solid rgba(250, 204, 21, 0.4)",
                      color: "#facc15",
                      fontSize: 12,
                      fontWeight: 500,
                      lineHeight: 1.45,
                    }}>
                      ⚠ {pmClobAccount.hint}
                    </div>
                  )}
                  <div style={{ marginTop: 10, fontSize: 11, color: "var(--muted)", lineHeight: 1.45 }}>
                    רענון תיק מלא (פוזיציות) מתבצע כל כמה שניות; נתוני יתרה גולמיים מתעדכנים גם מהרענון הכללי של הדשבורד.
                  </div>
                </>
              ) : liveModeEffective && livePortfolio?.ok ? (
                <>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>
                    Polymarket — נתונים חיים{" "}
                    <span
                      style={{
                        marginInlineStart: 6,
                        padding: "1px 6px",
                        borderRadius: 8,
                        fontSize: 10,
                        background: "rgba(34,197,94,0.18)",
                        color: "#4ade80",
                        border: "1px solid rgba(34,197,94,0.35)",
                      }}
                    >
                      LIVE
                    </span>
                  </div>
                  יתרת USDC ב-CLOB:{" "}
                  <strong className="tabular-nums" style={Number(livePortfolio.balance_usd ?? 0) === 0 ? { color: "#f87171" } : undefined}>
                    ${Number(livePortfolio.balance_usd ?? 0).toFixed(2)}
                  </strong>
                  {livePortfolio.allowance_usd != null && (
                    <span style={{
                      marginInlineStart: 10,
                      fontSize: 11,
                      color: Number(livePortfolio.allowance_usd) < Number(livePortfolio.balance_usd ?? 0) ? "#facc15" : "var(--muted)",
                    }}>
                      Allowance: ${Number(livePortfolio.allowance_usd).toFixed(2)}
                    </span>
                  )}
                  {Number(livePortfolio.balance_usd ?? 0) === 0 && !livePortfolio.hint && (
                    <div style={{
                      marginTop: 8,
                      padding: "8px 10px",
                      borderRadius: 6,
                      background: "rgba(248, 113, 113, 0.12)",
                      border: "1px solid rgba(248, 113, 113, 0.35)",
                      color: "#f87171",
                      fontSize: 11,
                      lineHeight: 1.4,
                    }}>
                      יתרה 0 — לא ניתן לסחור. הפקידו USDC לחשבון CLOB ב-polymarket.com (Deposit).
                    </div>
                  )}
                  <div style={{ marginTop: 6, color: "var(--muted)", fontSize: 12 }}>
                    שווי נטו (כולל פוזיציות פתוחות ב-Polymarket):{" "}
                    <strong className="tabular-nums" style={{ color: "var(--text)" }}>
                      ${Number(livePortfolio.equity_usd ?? livePortfolio.balance_usd ?? 0).toFixed(2)}
                    </strong>
                  </div>
                  <div style={{ marginTop: 6, color: "var(--muted)", fontSize: 12 }}>
                    פוזיציות פתוחות:{" "}
                    <strong className="tabular-nums" style={{ color: "var(--text)" }}>
                      {livePortfolio.positions?.length ?? 0}
                    </strong>
                  </div>
                  {livePortfolio.is_proxy && livePortfolio.funder_address ? (
                    <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8, wordBreak: "break-all", lineHeight: 1.35 }}>
                      Funder (proxy): {livePortfolio.funder_address}
                      <br />
                      Signer: {livePortfolio.address}
                    </div>
                  ) : livePortfolio.address ? (
                    <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8, wordBreak: "break-all", lineHeight: 1.35 }}>
                      כתובת חותם: {livePortfolio.address}
                    </div>
                  ) : null}
                  {livePortfolio.hint && (
                    <div style={{
                      marginTop: 8,
                      padding: "10px 12px",
                      borderRadius: 6,
                      background: "rgba(250, 204, 21, 0.15)",
                      border: "1px solid rgba(250, 204, 21, 0.4)",
                      color: "#facc15",
                      fontSize: 12,
                      fontWeight: 500,
                      lineHeight: 1.45,
                    }}>
                      ⚠ {livePortfolio.hint}
                    </div>
                  )}
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px dashed var(--border)", fontSize: 11, color: "var(--muted)", lineHeight: 1.45 }}>
                    נתוני היתרה והפוזיציות נמשכים ישירות מ-Polymarket (CLOB + Data API). ספר הסימולציה הפנימי מתסנכרן אוטומטית (reconcile) מדי רוטציה של חלון.
                  </div>
                  <details style={{ marginTop: 10 }}>
                    <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--muted)" }}>
                      ספר סימולציה (מקומי, לעיון)
                    </summary>
                    <div style={{ marginTop: 6, fontSize: 12 }}>
                      יתרה מקומית:{" "}
                      <strong className="tabular-nums">
                        ${Number((demoState as any).balance_usd || 0).toFixed(2)}
                      </strong>
                      <span style={{ color: "var(--muted)", margin: "0 6px" }}>·</span>
                      שווי נטו מקומי:{" "}
                      <strong className="tabular-nums">
                        $
                        {Number(
                          ((demoState as any).last_mark || {}).equity ||
                            (demoState as any).balance_usd ||
                            0,
                        ).toFixed(2)}
                      </strong>
                      <span style={{ color: "var(--muted)", fontSize: 10, marginInlineStart: 6 }}>
                        (חישוב מקומי)
                      </span>
                    </div>
                  </details>
                </>
              ) : (
                <>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>סימולציה (מנוע מקומי)</div>
                  יתרה במזומן:{" "}
                  <strong className="tabular-nums">${Number((demoState as any).balance_usd || 0).toFixed(2)}</strong>
                  <div style={{ marginTop: 6, color: "var(--muted)", fontSize: 12 }}>
                    שווי נטו (כולל פוזיציות):{" "}
                    <strong className="tabular-nums" style={{ color: "var(--text)" }}>
                      $
                      {Number(
                        ((demoState as any).last_mark || {}).equity || (demoState as any).balance_usd || 0,
                      ).toFixed(2)}
                    </strong>
                  </div>
                  {pmClobAccount?.ok ? (
                    <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
                      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>
                        Polymarket — יתרת USDC ב-CLOB (לפי המפתח ששמרת)
                      </div>
                      <div style={{ fontSize: 13 }}>
                        זמין למסחר:{" "}
                        <strong className="tabular-nums">
                          {pmClobAccount.balance_usd != null
                            ? `$${pmClobAccount.balance_usd.toFixed(2)}`
                            : "—"}
                        </strong>
                        <span style={{ color: "var(--muted)", margin: "0 6px" }}>·</span>
                        אישור הוצאה:{" "}
                        <strong className="tabular-nums">
                          {pmClobAccount.allowance_usd != null
                            ? `$${pmClobAccount.allowance_usd.toFixed(2)}`
                            : "—"}
                        </strong>
                      </div>
                      {pmClobAccount.is_proxy && pmClobAccount.funder_address ? (
                        <div
                          style={{
                            fontSize: 11,
                            color: "var(--muted)",
                            marginTop: 8,
                            wordBreak: "break-all",
                            lineHeight: 1.35,
                          }}
                        >
                          Funder (proxy): {pmClobAccount.funder_address}
                          <br />
                          Signer: {pmClobAccount.address}
                        </div>
                      ) : pmClobAccount.address ? (
                        <div
                          style={{
                            fontSize: 11,
                            color: "var(--muted)",
                            marginTop: 8,
                            wordBreak: "break-all",
                            lineHeight: 1.35,
                          }}
                        >
                          כתובת חותם: {pmClobAccount.address}
                        </div>
                      ) : null}
                      {pmClobAccount.hint && (
                        <div style={{
                          marginTop: 10,
                          padding: "10px 12px",
                          borderRadius: 6,
                          background: "rgba(250, 204, 21, 0.15)",
                          border: "1px solid rgba(250, 204, 21, 0.4)",
                          color: "#facc15",
                          fontSize: 12,
                          fontWeight: 500,
                          lineHeight: 1.45,
                        }}>
                          ⚠ {pmClobAccount.hint}
                        </div>
                      )}
                      <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8, lineHeight: 1.4 }}>
                        זה מה שמערכת ה-CLOB מדווחת (get_balance_allowance, collateral); זה לא בהכרח זהה ל«Portfolio» המלא באתר Polymarket.
                      </div>
                    </div>
                  ) : pmClobAccount && !pmClobAccount.ok ? (
                    <div style={{ marginTop: 10, fontSize: 12, color: "var(--muted)", lineHeight: 1.45 }}>
                      Polymarket CLOB: {pmClobAccount.error ?? "לא זמין"} — שמור מפתח (למעלה) והתקן py-clob-client במנוע.
                    </div>
                  ) : null}
                </>
              )}
            </Card>
            <Button
              variant="primary"
              type="button"
              disabled={actionLoading === "reset"}
              onClick={async () => {
                if (
                  !confirm(
                    "לאפס את חשבון הסימולציה ל־10,000$ — בלי פוזיציות פתוחות וללא סימוני מחיר?\n" +
                      "היסטוריית העסקאות נשמרת בקובץ (לא נמחקת) — ניתוח v3 ממשיך להשתמש בנתונים מהדיסק; במסך יוצג סשן חדש בלבד.",
                  )
                ) {
                  return;
                }
                setActionLoading("reset");
                try {
                  await api("/api/demo/reset", { method: "POST", body: "{}" });
                  await refresh();
                } finally {
                  setActionLoading(null);
                }
              }}
            >
              {actionLoading === "reset" ? "מאפס…" : "איפוס חשבון סימולציה (10,000 דולר)"}
            </Button>
          </div>
        </>
      )}

      {tab === "strategy" && (
        <Card padding="lg">
          <SectionTitle as="h2">הגדרות אסטרטגיה</SectionTitle>

          <div style={{ marginBottom: 16 }}>
            <strong>פריסטים:</strong>
            {Object.entries(PRESETS).map(([k, v]) => (
              <button
                key={k}
                type="button"
                style={{ marginRight: 8, marginTop: 8, padding: "6px 10px" }}
                onClick={() => {
                  const p = v as Record<string, unknown>;
                  setInv(v.investment_usd);
                  setEntryCents(v.entry_price_cents);
                  if ("take_profit_pct" in v) setTp(v.take_profit_pct);
                  setDca(!!p.dca_enabled);
                  if (p.dca_slices) setDcaSlices(Number(p.dca_slices));
                  if (p.dca_interval_sec) setDcaInt(Number(p.dca_interval_sec));
                  setHedge(!!v.hedge_enabled);
                  if (p.hedge_combined_ask_max) setHedgeMax(Number(p.hedge_combined_ask_max));
                  if (p.side_preference) setSide(p.side_preference as "Up" | "Down" | "signal");
                  markCfgDirty();
                  alert(v.desc);
                }}
              >
                {v.name}
              </button>
            ))}
          </div>

          <label style={{ display: "block", marginBottom: 8 }}>
            מצב סכום השקעה{" "}
            <span title="קבוע: סכום ב-$ לכל עסקה. אחוז מתיק: % מה-equity הנוכחי (demo balance + שווי פוזיציות)">?</span>
            <select
              value={investmentMode}
              onChange={(e) => {
                setInvestmentMode(e.target.value as "fixed" | "percent");
                markCfgDirty();
              }}
              style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 4, padding: 8 }}
            >
              <option value="fixed">סכום קבוע ($)</option>
              <option value="percent">אחוז מגודל התיק (%)</option>
            </select>
          </label>
          {investmentMode === "fixed" ? (
            <label>
              סכום השקעה ($) <span title="תקציב לעסקה">?</span>
              <input
                type="number"
                step="0.5"
                value={inv}
                onChange={(e) => {
                  setInv(Number(e.target.value));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
              />
              {lossRecoveryEnabled && (
                <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 12, lineHeight: 1.5 }}>
                  יעד השקעה אצל המנוע כרגע:{" "}
                  <strong className="tabular-nums">${(inv * lossRecoveryMultLive).toFixed(2)}</strong> (בסיס × מכפיל{" "}
                  {lossRecoveryMultLive.toFixed(2)}) · הפסדים רצופים: {lossRecoveryStreak}
                </div>
              )}
            </label>
          ) : (
            <label>
              אחוז מגודל התיק (%){" "}
              <span title="סכום לעסקה = equity × אחוז / 100. loss recovery חל על זה כמו על סכום קבוע.">?</span>
              <input
                type="number"
                min={0}
                max={100}
                step="0.5"
                value={investmentPctOfPortfolio}
                onChange={(e) => {
                  setInvestmentPctOfPortfolio(Number(e.target.value));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
              />
              <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 12, lineHeight: 1.5 }}>
                מחושב דינמית כל כניסה מול שווי התיק (equity snapshot){lossRecoveryEnabled
                  ? ` · מכפיל loss recovery ×${lossRecoveryMultLive.toFixed(2)}`
                  : ""}
              </div>
            </label>
          )}
          <label>
            כניסה במחיר (¢){" "}
            <span title="Polymarket: בדרך כלל 1¢–99¢ לחוזה (0.01$–0.99$)">?</span>
            <input
              type="number"
              min={1}
              max={99}
              value={entryCents}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n)) {
                  setEntryCents(Math.min(99, Math.max(1, Math.round(n))));
                  markCfgDirty();
                }
              }}
              style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
            />
            <span style={{ fontSize: 12, color: "var(--muted)", display: "block", marginTop: 4 }}>
              טווח מומלץ: 1–99 (סנטים) = 0.01$–0.99$ לחוזה
            </span>
          </label>
          <label style={{ display: "block", marginBottom: 12 }}>
            שוק BTC Up/Down (אורך חלון)
            <select
              value={btcWindow}
              onChange={(e) => {
                const v = e.target.value;
                if (v === "5m" || v === "15m") {
                  setBtcWindow(v);
                  markCfgDirty();
                }
              }}
              style={{ display: "block", width: "100%", maxWidth: 320, marginTop: 6, padding: 8 }}
            >
              <option value="5m">5 דק׳ — btc-updown-5m (ברירת מחדל)</option>
              <option value="15m">15 דק׳ — btc-updown-15m</option>
            </select>
            <span style={{ fontSize: 12, color: "var(--muted)", display: "block", marginTop: 6 }}>
              אחרי שינוי — לחץ &quot;שמור&quot;. הלוח והבוט יטענו את השוק המתאים (slug נפרד ב-Polymarket).
            </span>
          </label>
          <label>
            מינ׳ חוזים (רצפה — לא פחות ממינ׳ Polymarket)
            <input
              type="number"
              min={exchangeMinContractsCeil ?? 1}
              step={1}
              value={minContracts}
              onChange={(e) => {
                const v = Number(e.target.value);
                const floor = exchangeMinContractsCeil ?? 1;
                setMinContracts(Number.isFinite(v) ? Math.max(v, floor) : floor);
                markCfgDirty();
              }}
              style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, marginTop: 6, padding: 8 }}
            />
            <span style={{ fontSize: 12, color: "var(--muted)" }}>
              מינ׳ בורסה לשוק הנוכחי:{" "}
              {market != null ? (
                <>
                  <strong>{Math.ceil(market.order_min_size)}</strong> חוזים
                  {market.order_min_size_source === "clob"
                    ? " (מספר CLOB — סמכותי)"
                    : " (Gamma — עד עדכון מ־CLOB)"}
                </>
              ) : (
                "טוען…"
              )}
              . בפועל הבוט סוחר בלפחות <strong>{effectiveMinContracts}</strong> חוזים.
            </span>
          </label>
          <div style={{ marginBottom: 12, color: contracts ? "var(--up)" : "#f87171" }}>
            ≈ {contracts || `לא מספיק למינ׳ ${effectiveMinContracts}`} חוזים
            {contracts
              ? ` (מינ׳ ${effectiveMinContracts} — עומד)`
              : ` — צריך לפחות ${((effectiveMinContracts * entryCents) / 100).toFixed(2)}$ ב-${entryCents}¢`}
          </div>

          <label>
            יעד רווח % (נטו משוער)
            <input
              type="number"
              value={tp}
              onChange={(e) => {
                setTp(Number(e.target.value));
                markCfgDirty();
              }}
              style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
            />
          </label>
          <label>
            דק׳ מינימום לכניסה (נשאר בחלון)
            <input
              type="number"
              step="0.5"
              value={minMin}
              onChange={(e) => {
                setMinMin(Number(e.target.value));
                markCfgDirty();
              }}
              style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
            />
          </label>
          <label>
            קפיאה בדקה(ות) אחרונה(ות)
            <input
              type="number"
              step="0.5"
              value={freezeMin}
              onChange={(e) => {
                setFreezeMin(Number(e.target.value));
                markCfgDirty();
              }}
              style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
            />
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={interBlock}
              onChange={(e) => {
                setInterBlock(e.target.checked);
                markCfgDirty();
              }}
            />
            אזור ביניים: בלי כניסות חדשות בין {freezeMin} ל-{minMin} דק׳ לסיום
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={dca}
              onChange={(e) => {
                setDca(e.target.checked);
                markCfgDirty();
              }}
            />
            DCA
          </label>
          {dca && (
            <>
              <label>
                מספר פריסות
                <input
                  type="number"
                  value={dcaSlices}
                  onChange={(e) => {
                    setDcaSlices(Number(e.target.value));
                    markCfgDirty();
                  }}
                  style={{ display: "block", marginBottom: 8, padding: 8 }}
                />
              </label>
              <label>
                מרווח שניות
                <input
                  type="number"
                  value={dcaInt}
                  onChange={(e) => {
                    setDcaInt(Number(e.target.value));
                    markCfgDirty();
                  }}
                  style={{ display: "block", marginBottom: 12, padding: 8 }}
                />
              </label>
              <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px dashed #263244" }}>
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                  <input
                    type="checkbox"
                    checked={dcaDiscountEnabled}
                    onChange={(e) => {
                      setDcaDiscountEnabled(e.target.checked);
                      markCfgDirty();
                    }}
                  />
                  DCA בהנחת מחיר באחוזים (לכל סל)
                </label>
                <label>
                  אחוז הנחה מה-Ask (%)
                  <input
                    type="number"
                    step="0.5"
                    value={dcaDiscountPct}
                    onChange={(e) => {
                      setDcaDiscountPct(Number(e.target.value));
                      markCfgDirty();
                    }}
                    disabled={!dcaDiscountEnabled}
                    style={{
                      display: "block",
                      width: "100%",
                      maxWidth: 240,
                      marginBottom: 6,
                      padding: 8,
                      opacity: dcaDiscountEnabled ? 1 : 0.6,
                    }}
                  />
                </label>
                <div style={{ color: "var(--muted)", fontSize: 12 }}>
                  אם לא מסמנים—הבוט נשאר בהתנהגות הנוכחית: DCA לפי זמן. אם כן—הסל הבא מוגבל X% מתחת ל-Ask.
                </div>
              </div>
            </>
          )}
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={hedge}
              onChange={(e) => {
                setHedge(e.target.checked);
                markCfgDirty();
              }}
            />
            מצב גידור (רגל 2 כש-Ask משולב ≤)
            <input
              type="number"
              step="0.01"
              value={hedgeMax}
              onChange={(e) => {
                setHedgeMax(Number(e.target.value));
                markCfgDirty();
              }}
              style={{ width: 72, marginRight: 8 }}
            />
          </label>
          <div style={{ marginTop: 18, paddingTop: 14, borderTop: "1px solid #263244" }}>
            <h3 style={{ marginTop: 0 }}>רווח אוטומטי (TP) + מגבלות בטיחות</h3>
            <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <input
                type="checkbox"
                checked={autoReenter}
                onChange={(e) => {
                  setAutoReenter(e.target.checked);
                  markCfgDirty();
                }}
              />
              כניסה מחדש אוטומטית אחרי TP (אם נשאר זמן בחלון והתנאים מתקיימים)
            </label>
            <label>
              Cooldown אחרי TP (שניות)
              <input
                type="number"
                step="1"
                value={reenterCooldown}
                onChange={(e) => {
                  setReenterCooldown(Number(e.target.value));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
              />
            </label>
            <label>
              מקס׳ כניסות בחלון (5 דק׳)
              <input
                type="number"
                step="1"
                value={maxEntriesPerWindow}
                onChange={(e) => {
                  setMaxEntriesPerWindow(Number(e.target.value));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
              />
            </label>
            <label>
              תקרת חשיפה בחלון ($) (רך)
              <input
                type="number"
                step="10"
                value={maxNotionalPerWindow}
                onChange={(e) => {
                  setMaxNotionalPerWindow(Number(e.target.value));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 240, marginBottom: 12, padding: 8 }}
              />
            </label>
            <label>
              מקס׳ עסקאות לשעה (רך)
              <input
                type="number"
                step="1"
                value={maxTradesPerHour}
                onChange={(e) => {
                  setMaxTradesPerHour(Number(e.target.value));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
              />
            </label>
            <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px dashed #263244" }}>
              <div style={{ fontWeight: 700, marginBottom: 8 }}>סטטוס “קרוב ל…” (באחוזים)</div>
              <label>
                קרוב לכניסה: עד כמה % מעל היעד עדיין נחשב “קרוב”
                <input
                  type="number"
                  step="0.5"
                  value={nearEntryPct}
                  onChange={(e) => {
                    setNearEntryPct(Number(e.target.value));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
                />
              </label>
              <label>
                קרוב ל-TP: אם חסר עד כמה % ליעד — יוצג “קרוב”
                <input
                  type="number"
                  step="0.5"
                  value={nearTpPct}
                  onChange={(e) => {
                    setNearTpPct(Number(e.target.value));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 12, padding: 8 }}
                />
              </label>
              <label>
                DCA override (%)
                <input
                  type="number"
                  step="5"
                  value={dcaTpOverridePct}
                  onChange={(e) => {
                    setDcaTpOverridePct(Number(e.target.value));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginBottom: 8, padding: 8 }}
                />
              </label>
              <div style={{ color: "var(--muted)", fontSize: 12, lineHeight: 1.55, marginBottom: 8 }}>
                <strong>מתי זה נכנס לפעולה:</strong> רק כש־<strong>DCA מופעל</strong> ועדיין{" "}
                <strong>לא סיימת את כל הסלייסים</strong>. אז הבוט בדרך כלל <strong>לא</strong> מוכר ב־TP עד
                שתסיים את הפריסה — <strong>חוץ</strong> ממצבים מיוחדים: דקה(ות) אחרונות לפי &quot;קפיאה&quot;,
                או כאן: כשהרווח הלא ממומש (מול עלות, לפי bid ברגע הבדיקה) <strong>≥ האחוז הזה</strong> — אז
                מותרת מכירת TP גם באמצע DCA.
                <br />
                <strong>אחרי שכל הסלייסים בוצעו:</strong> השדה הזה <strong>לא רלוונטי</strong> — יציאה לפי{" "}
                <strong>יעד TP</strong> (take_profit_pct) הרגיל (+ מעט עמלה בחישוב המנוע).
                <br />
                <strong>מספר נמוך יותר</strong> = קל יותר &quot;לפתוח&quot; TP באמצע DCA. <strong>מספר גבוה</strong> = צריך
                רווח לא ממומש גדול יותר לפני שמותר למכור — עלול לעכב יציאה אם השוק קופץ וחוזר מהר (השיא בגרף ≠ מה
                שהבוט ראה בטיק).
              </div>
              <div style={{ color: "var(--muted)", fontSize: 12 }}>
                ברירות מחדל ל&quot;קרוב ל…&quot;: כניסה 3% / קרוב ל־TP 2%. DCA override לדוגמה 50% — רק כש־DCA חלקי.
                {!dca && (
                  <span style={{ display: "block", marginTop: 6, color: "#94a3b8" }}>
                    כרגע DCA כבוי — override נשמר בהגדרות אבל לא משפיע על המנוע.
                  </span>
                )}
              </div>
            </div>
            <label style={{ display: "block", marginTop: 12 }}>
              יומן מחירי שוק (שניות, 0 = כבוי)
              <span
                title="כל כמה שניות תיכתב שורה ביומן: Ask/Bid ל־Up ו‑Down מ־Polymarket CLOB — כדי לראות שהבוט מעדכן מול השוק"
                style={{ marginRight: 4 }}
              >
                ?
              </span>
              <input
                type="number"
                min={0}
                step={1}
                value={bookLogIntervalSec}
                onChange={(e) => {
                  setBookLogIntervalSec(Math.max(0, Number(e.target.value) || 0));
                  markCfgDirty();
                }}
                style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, padding: 8 }}
              />
              <span style={{ fontSize: 12, color: "var(--muted)", display: "block", marginTop: 4 }}>
                למשל 3–5 — לעקוב אחרי תנועת מחירים בלי להציף; 0 ללא שורות &quot;שוק CLOB&quot;.
              </span>
            </label>
            <div
              style={{
                marginTop: 14,
                paddingTop: 14,
                borderTop: "1px dashed #263244",
                marginBottom: 12,
              }}
            >
              <div style={{ fontWeight: 700, marginBottom: 8 }}>ביצוע מובטח (Market / FOK+FAK)</div>
              <p style={{ fontSize: 12, color: "var(--muted)", margin: "0 0 10px", lineHeight: 1.55 }}>
                מצב &quot;ביצוע מובטח&quot;: כניסה ב-FOK (או הכל או כלום — אין חצי פוזיציה), יציאה ב-FAK עם slippage רחב +
                retry ladder. פותר את הבעיה של &quot;עסקה שהייתה ברווח והפכה להפסד כי ה-LIMIT SELL לא התמלא&quot;.
              </p>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <input
                  type="checkbox"
                  checked={orderMode === "market"}
                  onChange={(e) => {
                    setOrderMode(e.target.checked ? "market" : "limit");
                    markCfgDirty();
                  }}
                />
                הפעל ביצוע מובטח (Market). כבוי = LIMIT GTC קלאסי (ברירת מחדל, תאימות לאחור).
              </label>
              <label>
                Slippage כניסה (% — תקרת מחיר מעל Ask ב-BUY FOK)
                <input
                  type="number"
                  step="0.1"
                  min={0}
                  max={50}
                  disabled={orderMode !== "market"}
                  value={entrySlippagePct}
                  onChange={(e) => {
                    setEntrySlippagePct(Math.max(0, Number(e.target.value) || 0));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label>
                Slippage יציאה (% — מינ׳ מחיר מתחת ל-Bid ב-SELL FAK; רחב יותר = ביצוע ודאי יותר)
                <input
                  type="number"
                  step="0.1"
                  min={0}
                  max={50}
                  disabled={orderMode !== "market"}
                  value={exitSlippagePct}
                  onChange={(e) => {
                    setExitSlippagePct(Math.max(0, Number(e.target.value) || 0));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label>
                נסיונות חוזרים ליציאה חלקית (retry ladder — slippage מתרחב)
                <input
                  type="number"
                  step="1"
                  min={0}
                  max={10}
                  disabled={orderMode !== "market"}
                  value={retryMaxAttempts}
                  onChange={(e) => {
                    setRetryMaxAttempts(Math.max(0, Math.floor(Number(e.target.value) || 0)));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6, marginBottom: 10 }}>
                <input
                  type="checkbox"
                  checked={peakWatchdogEnabled}
                  onChange={(e) => {
                    setPeakWatchdogEnabled(e.target.checked);
                    markCfgDirty();
                  }}
                />
                Peak Watchdog — אחרי שה-bid נגע ב-TP, מכירה אוטומטית אם נופל מהשיא
              </label>
              <label>
                נסיגה מהשיא שמפעילה יציאה (% — ברגע שה-bid נפל X% משיאו אחרי TP, מוכרים)
                <input
                  type="number"
                  step="0.1"
                  min={0}
                  max={50}
                  disabled={!peakWatchdogEnabled}
                  value={peakRetreatExitPct}
                  onChange={(e) => {
                    setPeakRetreatExitPct(Math.max(0, Number(e.target.value) || 0));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, marginBottom: 8 }}>
                <input
                  type="checkbox"
                  checked={holdToResolutionEnabled}
                  onChange={(e) => {
                    setHoldToResolutionEnabled(e.target.checked);
                    markCfgDirty();
                  }}
                />
                החזק עד סוף החלון (Hold-to-Resolution) — אחרי DCA, אל תצא ב-TP אם הכיוון ברור
              </label>
              <p style={{ fontSize: 12, color: "var(--muted)", margin: "0 0 10px", lineHeight: 1.55 }}>
                מטרה: אחרי כמה סלייסי DCA עם הפסדים, רווח קטן ב-TP לא מכסה. אם הבוט בכיוון הנכון (bid ≥ סף),
                מחזיקים עד סגירת החלון ומקבלים ~$1.00 לחוזה. stop-loss דינמי (אופציונלי) סוגר אם bid נופל
                מתחת לממוצע הכניסה המשוקלל. בדקה האחרונה (freeze) חוזרים להתנהגות רגילה.
              </p>
              <label>
                הפעלה רק אם בוצעו לפחות N סלייסי DCA (0 = להחזיק כבר מכניסה ראשונה)
                <input
                  type="number"
                  step="1"
                  min={0}
                  disabled={!holdToResolutionEnabled}
                  value={holdToResolutionMinDcaSlices}
                  onChange={(e) => {
                    setHoldToResolutionMinDcaSlices(Math.max(0, Math.floor(Number(e.target.value) || 0)));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label>
                סף ביטחון: הפעלה רק כש-bid ≥ (0.00–1.00)
                <input
                  type="number"
                  step="0.01"
                  min={0}
                  max={1}
                  disabled={!holdToResolutionEnabled}
                  value={holdToResolutionMinPrice}
                  onChange={(e) => {
                    setHoldToResolutionMinPrice(Math.max(0, Math.min(1, Number(e.target.value) || 0)));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <input
                  type="checkbox"
                  disabled={!holdToResolutionEnabled}
                  checked={holdToResolutionStopLoss}
                  onChange={(e) => {
                    setHoldToResolutionStopLoss(e.target.checked);
                    markCfgDirty();
                  }}
                />
                stop-loss דינמי — מכירה מידית אם bid יורד מתחת לממוצע הכניסה המשוקלל
              </label>
            </div>
            <div
              style={{
                marginTop: 14,
                paddingTop: 14,
                borderTop: "1px dashed #263244",
                marginBottom: 12,
              }}
            >
              <div style={{ fontWeight: 700, marginBottom: 8 }}>שחזור אחרי הפסד (מכפיל כניסה)</div>
              <p style={{ fontSize: 12, color: "var(--muted)", margin: "0 0 10px", lineHeight: 1.55 }}>
                רלוונטי כשהחלון נסגר והפוזיציה נפרקת בהפסד (הזמן נגמר / Up או Down הפסיד לפי מחיר הסגירה). לא מפעילים
                הכפלה על TP — אחרי TP מנצח המכפיל מתאפס.
              </p>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <input
                  type="checkbox"
                  checked={lossRecoveryEnabled}
                  onChange={(e) => {
                    setLossRecoveryEnabled(e.target.checked);
                    markCfgDirty();
                  }}
                />
                הפעל — סכום היעד לכניסה × מכפיל; אחרי פירוק מופסד המכפיל עולה; איפוס אחרי TP או פירוק מנצח
              </label>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 12 }}>
                <button
                  type="button"
                  disabled={!lossRecoveryEnabled}
                  onClick={() => {
                    setLossRecoveryStepPct(100);
                    setLossRecoveryEveryN(1);
                    markCfgDirty();
                  }}
                  style={{
                    padding: "8px 12px",
                    fontSize: 12,
                    borderRadius: 8,
                    border: "1px solid #475569",
                    background: lossRecoveryEnabled ? "#1e293b" : "#0f172a",
                    color: "#e2e8f0",
                    cursor: lossRecoveryEnabled ? "pointer" : "not-allowed",
                  }}
                >
                  הגדר הכפלה ×2 (צעד 100%, כל הפסד בפירוק)
                </button>
                <span style={{ fontSize: 11, color: "var(--muted)" }}>
                  מכפיל ×2 אחרי כל פירוק מופסד, עד התקרה למטה
                </span>
              </div>
              <label>
                צעד הגדלה (% — מכפיל חדש = ישן × (1 + %/100); 100% = הכפלה)
                <input
                  type="number"
                  step="1"
                  min={0}
                  value={lossRecoveryStepPct}
                  onChange={(e) => {
                    setLossRecoveryStepPct(Math.max(0, Number(e.target.value) || 0));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label>
                כל כמה הפסדים רצופים (פירוק) להחיל צעד (1 = אחרי כל הפסד)
                <input
                  type="number"
                  step="1"
                  min={1}
                  value={lossRecoveryEveryN}
                  onChange={(e) => {
                    setLossRecoveryEveryN(Math.max(1, Math.floor(Number(e.target.value) || 1)));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 10, padding: 8 }}
                />
              </label>
              <label>
                תקרת מכפיל מול בסיס ההשקעה
                <input
                  type="number"
                  step="0.5"
                  min={1}
                  value={lossRecoveryMaxMult}
                  onChange={(e) => {
                    setLossRecoveryMaxMult(Math.max(1, Number(e.target.value) || 1));
                    markCfgDirty();
                  }}
                  style={{ display: "block", width: "100%", maxWidth: 200, marginTop: 6, marginBottom: 8, padding: 8 }}
                />
              </label>
              <div style={{ color: "var(--muted)", fontSize: 12, lineHeight: 1.55 }}>
                חל על לולאת האסטרטגיה הראשית בלבד (לא Trigger Trader). סיכון מוגבר ליתרה — השתמש בתקרה וביתרה מתאימה.
              </div>
            </div>
            <div style={{ color: "var(--muted)", fontSize: 12 }}>
              ההגבלות כאן לא מחליפות Stop Loss — הן רק מונעות מהבוט לרוץ בצורה לא מבוקרת.
            </div>
          </div>
          <label>
            צד
            <select
              value={side}
              onChange={(e) => {
                setSide(e.target.value as "Up" | "Down" | "signal");
                markCfgDirty();
              }}
              style={{ display: "block", marginBottom: 16, padding: 8 }}
            >
              <option value="Up">Up</option>
              <option value="Down">Down</option>
              <option value="signal">אוטו (הצד הזול מ-Ask)</option>
            </select>
          </label>

          {/* Follow Last Winner — כיוון לפי תוצאת חלון/ות קודמים */}
          <div
            style={{
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-sm)",
              padding: 12,
              marginBottom: 16,
              background: "var(--bg-elevated)",
            }}
          >
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
              <input
                type="checkbox"
                checked={flwEnabled}
                onChange={(e) => {
                  setFlwEnabled(e.target.checked);
                  markCfgDirty();
                }}
              />
              כניסה לפי החלון המנצח (Follow Last Winner)
            </label>
            <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6, lineHeight: 1.55 }}>
              כשמסומן: כיוון הכניסה נגזר מתוצאת החלון/ות הקודמים, ועוקף את "צד" למעלה.
              כל שאר ההגדרות (DCA, TP, slippage, גידור) נשארות זהות. אם אין מספיק היסטוריה או יש תיקו — חוזרים לבחירה ב"צד".
            </div>

            {flwEnabled && (
              <div style={{ marginTop: 10, display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
                <label style={{ fontSize: 13 }}>
                  מספר חלונות לבדוק{" "}
                  <span title="1 = רק האחרון; 3 = רוב של 3 אחרונים; וכו'">?</span>
                  <input
                    type="number"
                    min={1}
                    max={5}
                    step={1}
                    value={flwLookback}
                    onChange={(e) => {
                      setFlwLookback(Math.max(1, Math.min(5, Math.floor(Number(e.target.value) || 1))));
                      markCfgDirty();
                    }}
                    style={{ display: "block", width: "100%", padding: 6, marginTop: 4 }}
                  />
                </label>

                <label style={{ fontSize: 13 }}>
                  כיוון{" "}
                  <span title="forward = הצד שניצח חוזר; reverse = הימור הפוך (mean reversion)">?</span>
                  <select
                    value={flwMode}
                    onChange={(e) => {
                      setFlwMode(e.target.value as "forward" | "reverse");
                      markCfgDirty();
                    }}
                    style={{ display: "block", width: "100%", padding: 6, marginTop: 4 }}
                  >
                    <option value="forward">בכיוון המנצח (forward)</option>
                    <option value="reverse">בכיוון הפוך (reverse)</option>
                  </select>
                </label>

                <label style={{ fontSize: 13 }}>
                  מינ׳ תזוזת BTC (%) {" "}
                  <span title="חלון שתזוזת BTC בו קטנה מהסף נחשב 'רעש' ולא נחשב בבחירה. 0 = ללא סינון">?</span>
                  <input
                    type="number"
                    min={0}
                    max={10}
                    step={0.01}
                    value={flwMinDrift}
                    onChange={(e) => {
                      setFlwMinDrift(Math.max(0, Math.min(10, Number(e.target.value) || 0)));
                      markCfgDirty();
                    }}
                    style={{ display: "block", width: "100%", padding: 6, marginTop: 4 }}
                  />
                </label>
              </div>
            )}

            {flwEnabled && lastWindowOutcome?.flw_preview && (
              <div
                style={{
                  marginTop: 12,
                  padding: 10,
                  borderRadius: "var(--radius-sm)",
                  background: "var(--bg)",
                  fontSize: 12,
                  lineHeight: 1.6,
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  תצוגה מקדימה (לפי המצב הנוכחי):
                </div>
                {lastWindowOutcome.flw_preview.side ? (
                  <div>
                    הכניסה הבאה תהיה{" "}
                    <strong
                      style={{
                        color: lastWindowOutcome.flw_preview.side === "Up" ? "var(--up)" : "var(--down)",
                      }}
                    >
                      {lastWindowOutcome.flw_preview.side}
                    </strong>{" "}
                    (lookback={lastWindowOutcome.flw_preview.lookback}, mode={lastWindowOutcome.flw_preview.mode})
                  </div>
                ) : (
                  <div style={{ color: "var(--muted)" }}>
                    אין מספיק היסטוריה / תיקו → fallback ל"צד"=<strong>{lastWindowOutcome.flw_preview.fallback_side_preference || side}</strong>
                  </div>
                )}
                {lastWindowOutcome.flw_preview.samples && lastWindowOutcome.flw_preview.samples.length > 0 && (
                  <div style={{ marginTop: 6, color: "var(--muted)" }}>
                    דגימות:{" "}
                    {lastWindowOutcome.flw_preview.samples
                      .map((s) => `${s.side_won === "Up" ? "↑" : "↓"} ${s.side_won}`)
                      .join(" · ")}
                  </div>
                )}
              </div>
            )}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
            <button
              type="button"
              style={{
                padding: "10px 20px",
                background: cfgDirty ? "var(--accent)" : "#334155",
                border: cfgDirty ? "2px solid #f59e0b" : "none",
                color: "#fff",
                borderRadius: 8,
              }}
              onClick={pushConfig}
            >
              {saveFeedback === "saved" ? "נשמר בהצלחה" : "שמור הגדרות"}
            </button>
            {cfgDirty && (
              <span style={{ fontSize: 13, color: "var(--muted)" }}>
                יש שינויים לא שמורים — ישמרו אוטומטית תוך 1.5 שניות (או לחץ שמור עכשיו)
              </span>
            )}
          </div>

          <h3>מצב בוט</h3>
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <input
              type="checkbox"
              checked={requireApproval}
              onChange={async (e) => {
                const v = e.target.checked;
                setRequireApproval(v);
                if (botMode !== "off") {
                  await setMode(v ? "semi" : "auto");
                }
              }}
            />
            דורש אישור לפני כניסה לעסקה (מסומן = חצי־אוטומטי, לא מסומן = נכנס בלי לשאול)
          </label>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {(["off", "semi", "auto"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                style={{
                  padding: "10px 18px",
                  background: botMode === m ? "var(--accent)" : "#333",
                  color: "#fff",
                  border: "none",
                  borderRadius: 8,
                }}
              >
                {m === "off" ? "כבוי" : m === "semi" ? "חצי-אוטומטי" : "אוטומטי מלא"}
              </button>
            ))}
          </div>

          <div style={{ marginTop: 10, color: "var(--muted)", fontSize: 13 }}>
            סטטוס מנוע: <strong style={{ color: "#fff" }}>{engineStatus || "—"}</strong>
            {engineLastTickTs ? (
              <div style={{ marginTop: 4 }}>
                עדכון אחרון:{" "}
                <strong style={{ color: "#fff" }}>
                  {Math.max(0, Math.floor((Date.now() / 1000 - engineLastTickTs) as number))} שנ׳
                </strong>
              </div>
            ) : null}
          </div>

          {pending && (
            <div
              style={{
                marginTop: 20,
                padding: 16,
                background: "#2d3748",
                borderRadius: 8,
              }}
            >
              <strong>ממתין לאישור:</strong>
              <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(pending, null, 2)}</pre>
              <button
                type="button"
                disabled={actionLoading === "approve"}
                style={{ marginLeft: 8, padding: "8px 16px", background: actionLoading === "approve" ? "#374151" : "var(--up)", border: "none", color: "#fff", borderRadius: 6, opacity: actionLoading === "approve" ? 0.7 : 1, cursor: actionLoading === "approve" ? "not-allowed" : "pointer" }}
                onClick={async () => {
                  setActionLoading("approve");
                  try {
                    // מצב "כסף אמיתי" נשלט מראש ע"י כפתור הראש — השרת קורא את הדגל מהמנוע.
                    const r = await api<{ ok?: boolean; error?: string }>("/api/strategy/approve", {
                      method: "POST",
                      body: JSON.stringify({}),
                    });
                    if (r && (r as { ok?: boolean }).ok === false) {
                      alert((r as { error?: string }).error || "כשל באישור");
                    }
                  } catch (e) {
                    alert(e instanceof Error ? e.message : "שגיאה");
                  } finally {
                    setActionLoading(null);
                  }
                  await refresh();
                }}
              >
                {actionLoading === "approve" ? "מאשר…" : "אשר פקודה"}
              </button>
              <button
                type="button"
                disabled={actionLoading === "reject"}
                style={{ opacity: actionLoading === "reject" ? 0.7 : 1, cursor: actionLoading === "reject" ? "not-allowed" : "pointer" }}
                onClick={async () => {
                  setActionLoading("reject");
                  try {
                    await api("/api/strategy/reject", { method: "POST", body: "{}" });
                  } finally {
                    setActionLoading(null);
                  }
                  await refresh();
                }}
              >
                {actionLoading === "reject" ? "מבטל…" : "בטל"}
              </button>
            </div>
          )}

          <div
            style={{
              marginTop: 24,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              flexWrap: "wrap",
            }}
          >
            <h3 style={{ margin: 0 }}>יומן</h3>
            <button
              type="button"
              disabled={logs.length === 0}
              title="מעתיק את כל שורות היומן (כפי שמוצג למעלה) ללוח"
              onClick={async () => {
                const text = logs.join("\n");
                try {
                  if (navigator.clipboard?.writeText) {
                    await navigator.clipboard.writeText(text);
                  } else {
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
                  setLogJournalCopied(true);
                  window.setTimeout(() => setLogJournalCopied(false), 1600);
                } catch {
                  alert("לא ניתן להעתיק ללוח — נסה שוב או העתק ידנית מהתיבה למטה.");
                }
              }}
              style={{
                fontSize: 12,
                padding: "6px 12px",
                borderRadius: 8,
                border: "1px solid rgba(255,255,255,0.15)",
                background: logs.length === 0 ? "rgba(255,255,255,0.04)" : "rgba(255,255,255,0.08)",
                color: "var(--text, #e2e8f0)",
                cursor: logs.length === 0 ? "not-allowed" : "pointer",
                opacity: logs.length === 0 ? 0.55 : 1,
              }}
            >
              {logJournalCopied ? "הועתק ללוח" : "העתק את כל היומן"}
            </button>
          </div>
          <pre
            style={{
              maxHeight: 200,
              overflow: "auto",
              background: "#111",
              padding: 12,
              fontSize: 12,
              borderRadius: 8,
            }}
          >
            {logs.join("\n")}
          </pre>
        </Card>
      )}

      {(tab === "stats" || tab === "stats_live") && (
        <Card padding="lg">
          <SectionTitle as="h2">
            {tab === "stats_live" ? "סטטיסטיקות מסחר חי" : "סטטיסטיקות סימולציה (דמו)"}
          </SectionTitle>
          {tab === "stats_live" && (
            <p style={{ fontSize: 13, color: "var(--muted)", marginTop: 8, marginBottom: 12, lineHeight: 1.5 }}>
              יתרה ושווי נטו מוצגים מ־<strong>Polymarket (CLOB)</strong> כשמצב לייב פעיל; הגרף והטבלה — עסקאות מסחר חי בלבד (
              <code style={{ fontSize: 12 }}>execution=live</code>
              ), בלי דמו.               רשומות <code style={{ fontSize: 12 }}>RECONCILE</code> (סנכרון יומן פנימי מול היתרה האמיתית) לא
              נספרות ב־PnL; גם <code style={{ fontSize: 12 }}>SETTLE_*</code> / <code style={{ fontSize: 12 }}>EXPIRE_0</code>{" "}
              (פירוק מודל בסוף חלון) לא נכנסים לגרף/אחוז ניצחונות בלייב — כדי שלא יופיעו קפיצות מזויפות כשהיה drift
              מול ה־CLOB. רשומות אלה עדיין ב־CSV ובהיסטוריה לביקורת.
            </p>
          )}
          {(() => {
            const statsLive = tab === "stats_live";
            const allTradesRaw = ((demoState as any).trades as any[]) || [];
            const sessionTrades = tradesForSessionStats(allTradesRaw as Trade[], demoState as Record<string, unknown> | null);
            let trades = statsLive ? tradesLiveOnly(sessionTrades) : sessionTrades;
            if (statsLive) {
              trades = trades.filter((t) => !isReconcileLedgerEntry(t));
            }
            const sessions = groupTradesBySession(trades);
            const tradesCount = sessions.length;
            const lastMark = (demoState as any).last_mark as
              | { equity?: number; unrealized_usd?: number; ts?: number }
              | undefined;
            const hasOpenDemoPositions = ((demoState as any).positions as unknown[])?.length > 0;

            let balance: number;
            let unreal: number;
            let equity: number;

            if (statsLive && liveModeEffective && livePortfolio?.ok) {
              balance = Number(livePortfolio.balance_usd ?? 0);
              equity = Number(livePortfolio.equity_usd ?? livePortfolio.balance_usd ?? 0);
              // לא ממומש: עדיין מחושב במנוע על פוזיציות ה-shadow (אותן פוזיציות כמו בלייב)
              unreal = hasOpenDemoPositions ? Number(lastMark?.unrealized_usd || 0) : 0;
            } else if (statsLive) {
              balance = 0;
              unreal = 0;
              equity = 0;
            } else {
              balance = Number((demoState as any).balance_usd || 0);
              unreal = hasOpenDemoPositions ? Number(lastMark?.unrealized_usd || 0) : 0;
              equity = hasOpenDemoPositions ? Number(lastMark?.equity || balance) : balance;
            }

            // גרף PnL מבוסס על רווח/הפסד ממומש בלבד (realized_pnl),
            // כך שהקו זז רק כשנסגרת עסקה ולא כשמחיר השוק זז.
            const data = statsLive ? cumPnlChartDataLive : cumPnlChartData;
            const pnlAnim = statsLive ? cumPnlAnimLive : cumPnlAnim;

            const allPnls = data.map((d) => d.pnl);
            const maxPnl = allPnls.length ? Math.max(...allPnls) : 0;
            const minPnl = allPnls.length ? Math.min(...allPnls) : 0;
            const pnl = allPnls.length ? allPnls[allPnls.length - 1] : 0;

            // יציאות עם PnL ממומש: בסימולציה — גם SETTLE/EXPIRE; בלייב — רק יציאות CLOB (SELL_*) כדי שלא יספרו פירוקי צל אחרי drift.
            const exitTrades = trades.filter(
              (t) =>
                t.realized_pnl != null &&
                !Number.isNaN(Number(t.realized_pnl)) &&
                (t.type === "EXPIRE_0" ||
                  t.type === "SETTLE_WIN" ||
                  t.type === "SETTLE_LOSS" ||
                  t.type === "SETTLE_UNKNOWN" ||
                  (t.type && String(t.type).startsWith("SELL"))) &&
                (!statsLive || !isShadowWindowSettlementTrade(t)),
            );
            const wins = exitTrades.filter((t) => Number(t.realized_pnl || 0) > 0);
            const losses = exitTrades.filter((t) => Number(t.realized_pnl || 0) < 0);
            const winRate =
              exitTrades.length > 0 ? (wins.length / exitTrades.length) * 100 : 0;
            const avgWin =
              wins.length > 0
                ? wins.reduce((s, t) => s + Number(t.realized_pnl || 0), 0) / wins.length
                : 0;
            const avgLossAbs =
              losses.length > 0
                ? Math.abs(
                    losses.reduce((s, t) => s + Number(t.realized_pnl || 0), 0) / losses.length,
                  )
                : 0;
            const rr = avgLossAbs > 0 ? avgWin / avgLossAbs : 0;

            return (
              <>
                <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
                  {statsLive && !liveModeEffective && (
                    <div className="alert-error" role="status" style={{ width: "100%", marginBottom: 4 }}>
                      מצב לייב לא פעיל — הפעל «מסחר חי» מהלוח וודא מפתח. להלן רק עסקאות חי מהיומן (אם יש).
                    </div>
                  )}
                  {statsLive && liveModeEffective && !livePortfolio?.ok && (
                    <div
                      style={{
                        width: "100%",
                        padding: "10px 12px",
                        borderRadius: 8,
                        background: "rgba(250, 204, 21, 0.12)",
                        border: "1px solid rgba(250, 204, 21, 0.35)",
                        color: "#facc15",
                        fontSize: 13,
                        marginBottom: 4,
                      }}
                    >
                      לא נטען תיק Polymarket — יתרה/שווי מוצגים כ־0. בדוק חיבור ל־CLOB. הגרף משקף עדיין עסקאות חי
                      ממומשות מהיומן.
                    </div>
                  )}
                  <div className="stat-pill" title="זמן מאז שהבוט/המנוע הופעל או מאז האיפוס האחרון (איפוס סטטיסטיקה / איפוס סימולציה / כיבוי מצב הבוט)">
                    זמן ריצה מאז הפעלה/איפוס:{" "}
                    <strong className="tabular-nums">
                      {runtimeDisplaySec != null ? formatHms(runtimeDisplaySec) : "—"}
                    </strong>
                  </div>
                  <div className="stat-pill">
                    יתרה במזומן:{" "}
                    <strong>
                      {statsLive && !livePortfolio?.ok ? "—" : `$${balance.toFixed(2)}`}
                    </strong>
                    {statsLive && livePortfolio?.ok && (
                      <span style={{ fontSize: 10, color: "var(--muted)", marginInlineStart: 6 }}>(CLOB)</span>
                    )}
                  </div>
                  <div className="stat-pill">
                    שווי נטו (כולל פוזיציות פתוחות):{" "}
                    <strong>
                      {statsLive && !livePortfolio?.ok ? "—" : `$${equity.toFixed(2)}`}
                    </strong>
                    {statsLive && livePortfolio?.ok && (
                      <span style={{ fontSize: 10, color: "var(--muted)", marginInlineStart: 6 }}>(Polymarket)</span>
                    )}
                  </div>
                  <div className="stat-pill">
                    רווח והפסד מצטבר:{" "}
                    <strong style={{ color: pnl >= 0 ? "var(--up)" : "var(--down)" }}>
                      {pnl >= 0 ? "+" : "-"}${Math.abs(pnl).toFixed(2)}
                    </strong>
                  </div>
                  <div className="stat-pill">
                    רווח והפסד לא ממומש:{" "}
                    <strong style={{ color: unreal >= 0 ? "var(--up)" : "var(--down)" }}>
                      {unreal >= 0 ? "+" : "-"}${Math.abs(unreal).toFixed(2)}
                    </strong>
                  </div>
                  <div className="stat-pill">
                    שיא רווח מצטבר:{" "}
                    <strong style={{ color: maxPnl >= 0 ? "var(--up)" : "var(--down)" }}>
                      {maxPnl >= 0 ? "+" : "-"}${Math.abs(maxPnl).toFixed(2)}
                    </strong>
                  </div>
                  <div className="stat-pill">
                    שיא הפסד מצטבר:{" "}
                    <strong style={{ color: minPnl <= 0 ? "var(--down)" : "var(--up)" }}>
                      {minPnl >= 0 ? "+" : "-"}${Math.abs(minPnl).toFixed(2)}
                    </strong>
                  </div>
                  <div className="stat-pill" title="מספר מחזורי עסקה מלאים (מכניסה ועד יציאה)">
                    מחזורי עסקה: <strong>{tradesCount}</strong>
                  </div>
                  <div className="stat-pill">
                    אחוז עסקאות רווחיות לעומת הפסדיות:{" "}
                    <strong style={{ color: winRate >= 50 ? "var(--up)" : "var(--down)" }}>
                      {winRate.toFixed(1)}%
                    </strong>
                  </div>
                  <div className="stat-pill">
                    יחס רווח מול סיכון ממוצע:{" "}
                    <strong style={{ color: rr >= 1 ? "var(--up)" : "var(--down)" }}>
                      {rr ? rr.toFixed(2) : "—"}
                    </strong>
                  </div>
                  <button
                    type="button"
                    style={{
                      padding: "10px 14px",
                      borderRadius: 10,
                      border: "none",
                      background: "#334155",
                      color: "#fff",
                    }}
                    onClick={async () => {
                      if (
                        !confirm(
                          "לסגור פוזיציות פתוחות (ליתרה לפי bid) ולהתחיל סשן סטטיסטיקה חדש?\n" +
                            "לא נמחקות עסקאות מהקובץ — ניתוח v3 לא נפגע; במסך יוצגו מהסשן החדש בלבד.",
                        )
                      )
                        return;
                      await api("/api/demo/clear-stats", { method: "POST", body: "{}" });
                      await refresh();
                    }}
                  >
                    איפוס נתוני סטטיסטיקה
                  </button>
                  <a
                    href={engineUrl(statsLive ? "/api/demo/export.csv?live_only=true" : "/api/demo/export.csv")}
                    download={statsLive ? "live-trades.csv" : "demo-trades.csv"}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      padding: "10px 14px",
                      borderRadius: 10,
                      background: "var(--accent)",
                      color: "#fff",
                      textDecoration: "none",
                      fontWeight: 700,
                    }}
                  >
                    ייצוא CSV
                  </a>
                </div>

                <ChartCard
                  title="רווח והפסד מצטברים"
                  subtitle={
                    statsLive
                      ? "רק עסקאות מסחר חי (ממומש). אחרי איפוס סטטיסטיקה — רק מהסשן הנוכחי."
                      : "רווח או הפסד ממומשים בלבד; הקו מתקדם עם סגירת עסקאות. אחרי איפוס — רק מהסשן הנוכחי (היסטוריה מלאה נשמרת לניתוח v3)."
                  }
                >
                  <div style={{ width: "100%", height: 300 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={data} margin={{ top: 8, right: 12, bottom: 8, left: 8 }}>
                        <XAxis
                          dataKey="ts"
                          type="number"
                          domain={data.length ? ["dataMin", "dataMax"] : [0, 1]}
                          tick={{ ...chartAxisTick, fontSize: 10 }}
                          tickFormatter={(v) => (Number(v) > 0 ? formatPnlAxisTime(Number(v)) : "")}
                          allowDecimals
                        />
                        <YAxis
                          tick={{ ...chartAxisTick, fontSize: 10 }}
                          domain={["auto", "auto"]}
                          width={56}
                          tickFormatter={(v) => `$${Number(v).toFixed(0)}`}
                        />
                        <Tooltip
                          contentStyle={chartTooltipStyle}
                          labelStyle={{ color: "var(--text-secondary)" }}
                          itemStyle={{ color: "var(--text)" }}
                          labelFormatter={(label) =>
                            Number(label) > 0 ? formatPnlAxisTime(Number(label)) : String(label)
                          }
                          formatter={(v: number) => [`$${Number(v).toFixed(2)}`, "PnL מצטבר"]}
                        />
                        <ReferenceLine y={0} stroke="var(--chart-axis)" strokeDasharray="3 3" />
                        <Line
                          // ב-PnL מצטבר עדיף להימנע מ-overshoot של "natural" כשיש מעט נקודות.
                          type="monotone"
                          dataKey="pnl"
                          stroke="var(--up)"
                          dot={false}
                          strokeWidth={chartStroke.width}
                          strokeLinecap={chartStroke.linecap}
                          strokeLinejoin={chartStroke.linejoin}
                          isAnimationActive={pnlAnim}
                          animationDuration={420}
                          animationEasing="ease-out"
                          connectNulls
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </ChartCard>

                <h3 style={{ marginTop: 18 }}>
                  היסטוריית עסקאות (בסשן הנוכחי{statsLive ? " — לייב בלבד" : ""})
                </h3>
                <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 8 }}>
                  עמודת «תחילת חלון» מבוססת על שדה אורך החלון בכל עסקה (5 דק׳ / 15 דק׳). «עלות הכניסה» = מחיר ליחידה × חוזים בכניסה (ברוטו; העמלה בעמודה נפרדת). אחרי איפוס לוח או איפוס סטטיסטיקה מוצגות כאן רק עסקאות מההתחלה החדשה; הרשומות הישנות נשארות בקובץ לניתוח v3.
                </p>
                <TradesBySession
                  trades={trades}
                  logEntries={logEntries}
                  lastMark={(demoState as any).last_mark}
                  fallbackWindowSec={(() => {
                    // נגזור מה-window_sec של העסקות האחרונות (trigger trades תמיד שומרות אותו)
                    const lastWithWindow = [...trades].reverse().find(t => (t as any).window_sec != null);
                    return (lastWithWindow as any)?.window_sec ?? market?.window_sec ?? (btcWindow === "15m" ? 900 : 300);
                  })()}
                  liveBtcUsd={btc.price > 0 ? btc.price : null}
                  priceToBeatUsd={market?.price_to_beat ?? null}
                  marketEpoch={market?.epoch ?? null}
                  priceToBeatNote={market?.price_to_beat_note ?? ""}
                />
              </>
            );
          })()}
        </Card>
      )}

      {tab === "help" && (
        <Card padding="lg" style={{ lineHeight: 1.7 }}>
          <SectionTitle as="h2">מילון מונחים ועזרה</SectionTitle>
          <ul>
            <li>
              <strong>חוזה:</strong> יחידת המסחר ב־Polymarket; גודל מינימלי לפי כללי השוק (לרוב חמישה חוזים).
            </li>
            <li>
              <strong>מחיר יעד לפתיחת החלון:</strong> ערך ה־BTC בתחילת החלון (כאן באמצעות שער עקיף); הרזולוציה הרשמית לפי Chainlink.
            </li>
            <li>
              <strong>שוק BTC:</strong> ניתן לבחור חלון של <strong>חמש דקות</strong> —{" "}
              <code>btc-updown-5m-{"{epoch}"}</code> — או <strong>חמש עשרה דקות</strong> —{" "}
              <code>btc-updown-15m-{"{epoch}"}</code>.
            </li>
            <li>
              <strong>סימולציה:</strong> ביצוע הזמנות וירטואליות מול ספר הזמנות בפועל, ללא רישום על גבי הבלוקצ׳יין.
            </li>
            <li>
              <strong>מסחר חי:</strong> דורש התקנת py-clob-client ומפתח תקף; יש לעמוד בתנאי השימוש של Polymarket ובדיני המדינה החלים.
            </li>
            <li>
              <strong>גידור:</strong> החזקה בו־זמנית בכיווני Up ו־Down; אינה שקולה לשורט במובן הקלאסי.
            </li>
          </ul>
          <h3>תקלות נפוצות</h3>
          <p>
            אם לא נשלחה פקודה: ייתכן שנותר פחות מדקה לסיום החלון, או שהמחיר בבקשה לקנייה עולה על הגבול שהוגדר, או שאין
            יתרה מספקת בחשבון הסימולציה.
          </p>
          <h3>הפעלה</h3>
          <pre style={{ background: "var(--bg-elevated)", padding: 12, borderRadius: "var(--radius-sm)", border: "1px solid var(--border)" }}>
            cd engine && pip install -r requirements.txt
            cd .. && npm install && npm run dev
          </pre>
        </Card>
      )}
      {tab === "tips_v2" && <TipsV2 />}
      {tab === "analytics_v3" && <AnalyticsV3 />}
      {tab === "signals" && <SignalsPanel />}
      {tab === "trigger" && <TriggerTrader />}
      </main>
    </div>
  );
}
