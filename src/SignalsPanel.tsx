import { useCallback, useEffect, useRef, useState } from "react";
import { api, isPageHidden } from "./api";
import { Card } from "./ui/Card";
import { SectionTitle } from "./ui/SectionTitle";
import { israelHM, israelTimeMs } from "./timeFormat";

// ─── Types ────────────────────────────────────────────────────────────────────

type SignalItem = {
  name: string;
  signal: "up" | "down" | "neutral";
  note: string;
  value?: number;
};

type SubTA = {
  available: boolean;
  rsi?: number | null;
  ema9?: number | null;
  ema21?: number | null;
  atr?: number | null;
  momentum_3m_pct?: number | null;
  momentum_5m_pct?: number | null;
  score?: number;
  max_score?: number;
  current_price?: number;
  signals?: SignalItem[];
};

type SubCLOB = {
  available: boolean;
  up?: { bid_depth: number; ask_depth: number; imbalance: number; spread?: number | null };
  down?: { bid_depth: number; ask_depth: number; imbalance: number; spread?: number | null };
  net_score?: number;
  signal?: string;
  signals?: SignalItem[];
};

type SubHistory = {
  available: boolean;
  total_windows?: number;
  overall?: { up_wins: number; down_wins: number; up_rate?: number | null; down_rate?: number | null };
  current_hour_utc?: number;
  hour?: { total: number; up_wins: number; up_rate?: number | null };
  current_weekday?: number;
  recent_5?: string[];
  error?: string;
};

type SubSentiment = {
  available: boolean;
  funding?: { available: boolean; rate_pct?: number; signal?: string; note?: string };
  fear_greed?: { available: boolean; value?: number; classification?: string; signal?: string; note?: string };
  score?: number;
  signals?: SignalItem[];
};

type SignalsData = {
  up_confidence: number;
  down_confidence: number;
  recommendation: "Up" | "Down" | "neutral";
  confidence_pct: number;
  weighted_score: number;
  signals: SignalItem[];
  sub: {
    ta: SubTA;
    clob: SubCLOB;
    history: SubHistory;
    sentiment: SubSentiment;
  };
  weights: Record<string, number>;
  threshold: number;
  ts: number;
  contract_asks?: { up: number | null; down: number | null };
  market_slug?: string | null;
  btc_window?: string;
};

type ContractPrices = {
  up: number | null;
  down: number | null;
  slug: string | null;
  btc_window?: string;
  ts: number;
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

const SIGNAL_COLORS: Record<string, string> = {
  up: "var(--up)",
  down: "var(--down)",
  neutral: "var(--muted)",
};

const SIGNAL_LABELS: Record<string, string> = {
  up: "⬆ Up",
  down: "⬇ Down",
  neutral: "— ניטרלי",
};

function pct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function slugToLabel(slug: string | null | undefined): string {
  if (!slug) return "";
  // "btc-updown-5m-1774466700" → "BTC 5 דק׳ · 21:55–22:00"
  const m = slug.match(/btc-updown-(5m|15m)-(\d+)/);
  if (!m) return slug;
  const winLabel = m[1] === "5m" ? "5 דק׳" : "15 דק׳";
  const epoch = parseInt(m[2]);
  const winSec = m[1] === "5m" ? 300 : 900;
  const start = israelHM(epoch);
  const end = israelHM(epoch + winSec);
  return `BTC ${winLabel} · ${start}–${end}`;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function SignalBadge({ signal }: { signal: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 6,
        fontSize: 11,
        fontWeight: 700,
        background:
          signal === "up"
            ? "var(--up-muted)"
            : signal === "down"
              ? "var(--down-muted)"
              : "rgba(120,130,150,0.15)",
        color: SIGNAL_COLORS[signal] ?? "var(--muted)",
        letterSpacing: "0.02em",
      }}
    >
      {SIGNAL_LABELS[signal] ?? signal}
    </span>
  );
}

function SignalRow({ item }: { item: SignalItem }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        padding: "7px 0",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <span style={{ minWidth: 64, fontWeight: 600, fontSize: 13, color: "var(--accent-bright)" }}>
        {item.name}
      </span>
      <SignalBadge signal={item.signal} />
      <span style={{ color: "var(--text-secondary)", fontSize: 12, flex: 1, lineHeight: 1.45 }}>
        {item.note}
      </span>
    </div>
  );
}

function ConfidenceBar({ upConf, downConf }: { upConf: number; downConf: number }) {
  const upPct = upConf * 100;
  const downPct = downConf * 100;
  return (
    <div style={{ marginTop: 12 }}>
      <div
        style={{
          display: "flex",
          height: 10,
          borderRadius: 5,
          overflow: "hidden",
          background: "var(--border)",
        }}
      >
        <div style={{ width: `${upPct}%`, background: "var(--up)", transition: "width 0.4s ease" }} />
        <div style={{ width: `${downPct}%`, background: "var(--down)", transition: "width 0.4s ease" }} />
      </div>
      <div
        style={{ display: "flex", justifyContent: "space-between", marginTop: 4, fontSize: 11, color: "var(--muted)" }}
      >
        <span style={{ color: "var(--up)" }}>Up {upPct.toFixed(1)}%</span>
        <span style={{ color: "var(--down)" }}>Down {downPct.toFixed(1)}%</span>
      </div>
    </div>
  );
}

function TASection({ data }: { data: SubTA }) {
  if (!data.available) return <div style={{ color: "var(--muted)", fontSize: 13 }}>לא זמין</div>;
  return (
    <div style={{ fontSize: 13 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px", marginBottom: 8 }}>
        {data.rsi != null && (
          <div>
            <span style={{ color: "var(--muted)" }}>RSI(14): </span>
            <strong style={{ color: data.rsi > 55 ? "var(--up)" : data.rsi < 45 ? "var(--down)" : "var(--text)" }}>
              {data.rsi.toFixed(1)}
            </strong>
          </div>
        )}
        {data.ema9 != null && data.ema21 != null && (
          <div>
            <span style={{ color: "var(--muted)" }}>EMA9/21: </span>
            <strong style={{ color: data.ema9 > data.ema21 ? "var(--up)" : "var(--down)" }}>
              {data.ema9 > data.ema21 ? "⬆" : "⬇"} {Math.abs(data.ema9 - data.ema21).toFixed(0)}$
            </strong>
          </div>
        )}
        {data.atr != null && (
          <div>
            <span style={{ color: "var(--muted)" }}>ATR(14): </span>
            <strong>${data.atr.toFixed(0)}</strong>
          </div>
        )}
        {data.momentum_3m_pct != null && (
          <div>
            <span style={{ color: "var(--muted)" }}>מומנטום 3m: </span>
            <strong style={{ color: data.momentum_3m_pct > 0 ? "var(--up)" : "var(--down)" }}>
              {data.momentum_3m_pct > 0 ? "+" : ""}{data.momentum_3m_pct.toFixed(3)}%
            </strong>
          </div>
        )}
      </div>
    </div>
  );
}

function CLOBSection({ data }: { data: SubCLOB }) {
  if (!data.available) return <div style={{ color: "var(--muted)", fontSize: 13 }}>לא זמין</div>;
  return (
    <div style={{ fontSize: 13 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px" }}>
        {data.up && (
          <div>
            <span style={{ color: "var(--muted)" }}>Up imbalance: </span>
            <strong style={{ color: (data.up.imbalance ?? 0) > 0 ? "var(--up)" : "var(--down)" }}>
              {((data.up.imbalance ?? 0) * 100).toFixed(1)}%
            </strong>
          </div>
        )}
        {data.down && (
          <div>
            <span style={{ color: "var(--muted)" }}>Down imbalance: </span>
            <strong style={{ color: (data.down.imbalance ?? 0) > 0 ? "var(--up)" : "var(--down)" }}>
              {((data.down.imbalance ?? 0) * 100).toFixed(1)}%
            </strong>
          </div>
        )}
        {data.up && (
          <div>
            <span style={{ color: "var(--muted)" }}>Up bid/ask: </span>
            <strong>{data.up.bid_depth.toFixed(0)}$ / {data.up.ask_depth.toFixed(0)}$</strong>
          </div>
        )}
        {data.down && (
          <div>
            <span style={{ color: "var(--muted)" }}>Down bid/ask: </span>
            <strong>{data.down.bid_depth.toFixed(0)}$ / {data.down.ask_depth.toFixed(0)}$</strong>
          </div>
        )}
      </div>
    </div>
  );
}

function HistorySection({ data }: { data: SubHistory }) {
  if (!data.available && !data.total_windows) {
    return (
      <div style={{ color: "var(--muted)", fontSize: 13 }}>
        אין מספיק נתונים היסטוריים עדיין. הנתונים יצטברו כאשר הבוט יעקוב אחרי חלונות שנפתרו.
      </div>
    );
  }
  const overall = data.overall;
  const hour = data.hour;
  const recent = data.recent_5 ?? [];
  return (
    <div style={{ fontSize: 13 }}>
      {overall && (data.total_windows ?? 0) > 0 && (
        <div style={{ marginBottom: 6 }}>
          <span style={{ color: "var(--muted)" }}>כולל ({data.total_windows} חלונות): </span>
          <strong style={{ color: "var(--up)" }}>Up {pct(overall.up_rate)}</strong>
          {" · "}
          <strong style={{ color: "var(--down)" }}>Down {pct(overall.down_rate)}</strong>
        </div>
      )}
      {hour && hour.total > 0 && data.current_hour_utc != null && (
        <div style={{ marginBottom: 6 }}>
          <span style={{ color: "var(--muted)" }}>שעה {data.current_hour_utc}:00 UTC ({hour.total} חלונות): </span>
          <strong style={{ color: (hour.up_rate ?? 0.5) > 0.5 ? "var(--up)" : "var(--down)" }}>
            Up {pct(hour.up_rate)}
          </strong>
        </div>
      )}
      {recent.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <span style={{ color: "var(--muted)", fontSize: 12 }}>5 אחרונים: </span>
          {recent.map((side, i) => (
            <span
              key={i}
              style={{
                display: "inline-block",
                margin: "0 2px",
                padding: "1px 7px",
                borderRadius: 4,
                fontSize: 11,
                fontWeight: 700,
                background: side === "Up" ? "var(--up-muted)" : "var(--down-muted)",
                color: side === "Up" ? "var(--up)" : "var(--down)",
              }}
            >
              {side}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function SentimentSection({ data }: { data: SubSentiment }) {
  if (!data.available) return <div style={{ color: "var(--muted)", fontSize: 13 }}>לא זמין</div>;
  return (
    <div style={{ fontSize: 13 }}>
      {data.funding?.available && (
        <div style={{ marginBottom: 6 }}>
          <span style={{ color: "var(--muted)" }}>Funding Rate: </span>
          <strong
            style={{
              color:
                data.funding.signal === "up"
                  ? "var(--up)"
                  : data.funding.signal === "down"
                    ? "var(--down)"
                    : "var(--text)",
            }}
          >
            {data.funding.rate_pct?.toFixed(4)}%
          </strong>
          <span style={{ color: "var(--muted)", fontSize: 11, marginRight: 6 }}>
            ({data.funding.signal === "up" ? "שלילי — נח׳ shorts" : data.funding.signal === "down" ? "חיובי — נח׳ longs" : "ניטרלי"})
          </span>
        </div>
      )}
      {data.fear_greed?.available && (
        <div>
          <span style={{ color: "var(--muted)" }}>Fear & Greed: </span>
          <strong
            style={{
              color:
                data.fear_greed.signal === "up"
                  ? "var(--up)"
                  : data.fear_greed.signal === "down"
                    ? "var(--down)"
                    : "var(--text)",
            }}
          >
            {data.fear_greed.value}
          </strong>
          <span style={{ color: "var(--text-secondary)", fontSize: 12, marginRight: 6 }}>
            ({data.fear_greed.classification})
          </span>
        </div>
      )}
    </div>
  );
}

// ─── Window Selector ──────────────────────────────────────────────────────────

function WindowSelector({
  selected,
  onChange,
}: {
  selected: "5m" | "15m";
  onChange: (w: "5m" | "15m") => void;
}) {
  const btnStyle = (active: boolean, win: "5m" | "15m") => ({
    padding: "6px 18px",
    borderRadius: 8,
    border: active ? "1.5px solid var(--accent-bright)" : "1px solid var(--border)",
    background: active ? "var(--accent-muted)" : "transparent",
    color: active ? "var(--accent-bright)" : "var(--muted)",
    fontWeight: active ? 700 : 400,
    fontSize: 13,
    cursor: "pointer",
    transition: "all 0.15s",
  });
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--muted)", marginLeft: 4 }}>חלון:</span>
      {(["5m", "15m"] as const).map(w => (
        <button key={w} type="button" style={btnStyle(selected === w, w)} onClick={() => onChange(w)}>
          {w === "5m" ? "⏱ 5 דק׳" : "⏳ 15 דק׳"}
        </button>
      ))}
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

export default function SignalsPanel() {
  const [selectedWindow, setSelectedWindow] = useState<"5m" | "15m">("5m");
  const [data, setData] = useState<SignalsData | null>(null);
  const [liveAsk, setLiveAsk] = useState<ContractPrices | null>(null);
  const [loading, setLoading] = useState(false);
  const [priceAge, setPriceAge] = useState<number>(0); // שניות מאז עדכון אחרון
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<number | null>(null);
  const priceTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async (force = false, win?: "5m" | "15m") => {
    const w = win ?? selectedWindow;
    setLoading(true);
    setError(null);
    try {
      const result = await api<SignalsData>(
        `/api/signals?window=${w}${force ? "&refresh=true" : ""}`
      );
      setData(result);
      setLastRefresh(Date.now());
    } catch (e) {
      setError(e instanceof Error ? e.message : "שגיאה בטעינת סיגנלים");
    } finally {
      setLoading(false);
    }
  }, [selectedWindow]);

  // כשמשנים חלון — רענן מיד + נקה נתונים ישנים
  const handleWindowChange = useCallback((w: "5m" | "15m") => {
    setSelectedWindow(w);
    setData(null);
    setLiveAsk(null);
    refresh(false, w);
  }, [refresh]);

  // Auto-refresh signals every 5 seconds (skip when tab hidden)
  useEffect(() => {
    refresh();
    const id = setInterval(() => {
      if (!isPageHidden()) refresh();
    }, 5_000);
    return () => clearInterval(id);
  }, [refresh]);

  // Contract price poll — every 750ms (server uses WS cache; skip when tab hidden)
  useEffect(() => {
    let active = true;
    const poll = async () => {
      if (!active || isPageHidden()) return;
      try {
        const p = await api<ContractPrices>(`/api/contract-prices?window=${selectedWindow}`);
        if (active) {
          setLiveAsk(p);
          setPriceAge(0);
        }
      } catch {
        // silent
      }
    };
    poll();
    const id = setInterval(poll, 750);
    if (priceTimerRef.current) clearInterval(priceTimerRef.current);
    priceTimerRef.current = setInterval(() => setPriceAge(a => a + 1), 1000);
    return () => {
      active = false;
      clearInterval(id);
      if (priceTimerRef.current) clearInterval(priceTimerRef.current);
    };
  }, [selectedWindow]);

  const rec = data?.recommendation;
  const recColor = rec === "Up" ? "var(--up)" : rec === "Down" ? "var(--down)" : "var(--muted)";

  // מציג את ה-slug מהסיגנלים או מהמחירים
  const activeSlug = data?.market_slug ?? liveAsk?.slug;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--s-4)" }}>
      {/* ── כותרת + בורר חלון + כפתור רענון ── */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <SectionTitle>סיגנלים וכיוון השקעה</SectionTitle>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <WindowSelector selected={selectedWindow} onChange={handleWindowChange} />
          <button
            type="button"
            onClick={() => refresh(true)}
            disabled={loading}
            style={{
              background: "var(--accent-muted)",
              border: "1px solid var(--border-strong)",
              borderRadius: 8,
              color: "var(--accent-bright)",
              cursor: loading ? "default" : "pointer",
              fontSize: 12,
              fontWeight: 600,
              padding: "5px 14px",
              opacity: loading ? 0.6 : 1,
            }}
          >
            {loading ? "טוען…" : "רענן"}
          </button>
        </div>
      </div>

      {error && (
        <div
          style={{
            background: "var(--down-muted)",
            border: "1px solid var(--down)",
            borderRadius: 8,
            padding: "10px 14px",
            color: "var(--down)",
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}

      {/* ── כרטיס המלצה ראשי ── */}
      {data && (
        <Card padding="md">
          {/* שם החוזה הפעיל */}
          <div style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
            marginBottom: 8,
          }}>
            <div style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              background: "rgba(100,130,200,0.1)",
              border: "1px solid var(--border-strong)",
              borderRadius: 8,
              padding: "4px 12px",
              fontSize: 12,
              color: "var(--text-secondary)",
              fontVariantNumeric: "tabular-nums",
            }}>
              <span style={{ fontWeight: 700, color: "var(--accent-bright)" }}>
                {selectedWindow === "5m" ? "⏱ 5 דק׳" : "⏳ 15 דק׳"}
              </span>
              {activeSlug && (
                <span style={{ color: "var(--muted)" }}>
                  {slugToLabel(activeSlug)}
                </span>
              )}
            </div>
          </div>

          <div style={{ textAlign: "center", padding: "4px 0 12px" }}>
            <div style={{ color: "var(--muted)", fontSize: 13, marginBottom: 6 }}>המלצת כיוון</div>
            <div
              style={{
                fontSize: 48,
                fontWeight: 800,
                color: recColor,
                letterSpacing: "-0.03em",
                lineHeight: 1,
                fontFamily: "var(--font-display)",
              }}
            >
              {rec === "Up" ? "⬆ UP" : rec === "Down" ? "⬇ DOWN" : "≡ ניטרלי"}
            </div>
            <div style={{ fontSize: 14, color: "var(--text-secondary)", marginTop: 6 }}>
              ביטחון: <strong style={{ color: recColor }}>{data.confidence_pct}%</strong>
              {rec === "neutral" && (
                <span style={{ color: "var(--muted)", fontSize: 12 }}> — אין כיוון ברור, אל תיכנס</span>
              )}
            </div>

            {/* מחירי חוזים חיים */}
            {(liveAsk || data.contract_asks) && (
              <div style={{ display: "flex", gap: 10, justifyContent: "center", marginTop: 14 }}>
                {(["up", "down"] as const).map(side => {
                  const cents = liveAsk ? liveAsk[side] : data.contract_asks?.[side];
                  const isUp = side === "up";
                  const color = isUp ? "var(--up)" : "var(--down)";
                  const bg = isUp ? "var(--up-muted)" : "var(--down-muted)";
                  return (
                    <div key={side} style={{
                      background: bg,
                      border: `1px solid ${color}`,
                      borderRadius: 10,
                      padding: "8px 18px",
                      textAlign: "center",
                      minWidth: 90,
                    }}>
                      <div style={{ fontSize: 11, color, fontWeight: 700, marginBottom: 3 }}>
                        {isUp ? "⬆ Up" : "⬇ Down"}
                      </div>
                      <div style={{
                        fontSize: 26, fontWeight: 800, color,
                        fontVariantNumeric: "tabular-nums", lineHeight: 1,
                      }}>
                        {cents != null ? `${cents}¢` : "—"}
                      </div>
                      <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 3 }}>
                        מחיר קנייה
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            <ConfidenceBar upConf={data.up_confidence} downConf={data.down_confidence} />
          </div>

          {/* תחתית הכרטיס: זמני עדכון */}
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
            <span>
              מחירים: עכשיו
              {priceAge > 2 && (
                <span style={{ color: "var(--down)" }}> ({priceAge}ש׳ לא עודכן)</span>
              )}
            </span>
            {lastRefresh && (
              <span>סיגנלים: {israelTimeMs(lastRefresh)}</span>
            )}
          </div>
        </Card>
      )}

      {/* ── רשימת סיגנלים מפורטים ── */}
      {data && data.signals.length > 0 && (
        <Card padding="md">
          <h3 className="section-title" style={{ fontSize: "1rem", marginBottom: "var(--s-2)" }}>
            פירוט סיגנלים
          </h3>
          <div>
            {data.signals.map((s, i) => (
              <SignalRow key={i} item={s} />
            ))}
          </div>
        </Card>
      )}

      {/* ── פירוט לפי קטגוריה ── */}
      {data && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
            gap: "var(--s-3)",
          }}
        >
          <Card padding="md">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ fontWeight: 700, fontSize: 13, color: "var(--accent-bright)" }}>📊 ניתוח טכני (TA)</div>
              <div style={{ fontSize: 11, color: "var(--muted)" }}>משקל {(data.weights.ta * 100).toFixed(0)}%</div>
            </div>
            <TASection data={data.sub.ta} />
          </Card>

          <Card padding="md">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ fontWeight: 700, fontSize: 13, color: "var(--accent-bright)" }}>📖 CLOB Imbalance</div>
              <div style={{ fontSize: 11, color: "var(--muted)" }}>משקל {(data.weights.clob * 100).toFixed(0)}%</div>
            </div>
            <CLOBSection data={data.sub.clob} />
          </Card>

          <Card padding="md">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ fontWeight: 700, fontSize: 13, color: "var(--accent-bright)" }}>🕓 היסטוריה</div>
              <div style={{ fontSize: 11, color: "var(--muted)" }}>משקל {(data.weights.history * 100).toFixed(0)}%</div>
            </div>
            <HistorySection data={data.sub.history} />
          </Card>

          <Card padding="md">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ fontWeight: 700, fontSize: 13, color: "var(--accent-bright)" }}>🌡 סנטימנט</div>
              <div style={{ fontSize: 11, color: "var(--muted)" }}>משקל {(data.weights.sentiment * 100).toFixed(0)}%</div>
            </div>
            <SentimentSection data={data.sub.sentiment} />
          </Card>
        </div>
      )}

      {/* ── הסבר מתודולוגיה ── */}
      <Card padding="md">
        <h3 className="section-title" style={{ fontSize: "0.95rem", marginBottom: "var(--s-2)" }}>
          איך מחושב הציון?
        </h3>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <div>• <strong>TA (40%)</strong> — RSI, EMA9/21, מומנטום 3m ו-5m על נרות Binance 1m</div>
          <div>• <strong>CLOB (30%)</strong> — השוואת bid depth בין Up ו-Down (smart money flow)</div>
          <div>• <strong>היסטוריה (15%)</strong> — win rate לפי שעה ב-24 שעות אחרונות (SQLite מקומי)</div>
          <div>• <strong>סנטימנט (15%)</strong> — Binance Funding Rate + Fear & Greed Index</div>
          <div style={{ marginTop: 6, color: "var(--muted)" }}>
            המלצה מופיעה רק כאשר הביטחון {'>'} {((data?.threshold ?? 0.6) * 100).toFixed(0)}%.
            מתחת לסף — "ניטרלי" = עדיף לא להיכנס. מחירי חוזה מתעדכנים כל 500ms; סיגנלים כל 15 שנ׳.
          </div>
        </div>
      </Card>
    </div>
  );
}
