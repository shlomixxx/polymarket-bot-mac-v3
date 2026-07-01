/**
 * WindowCircles — the shared 🟢/🔴 window-outcome circle UI.
 * One look + one click-to-reveal interaction, reused across the Strategy strip,
 * the Stats-tab windows card, and each trade's expanded panel. All on-token, RTL-aware.
 */
import type { ReactNode } from "react";
import {
  clockHms,
  deriveTradeWindowView,
  deriveWindowStats,
  driftOf,
  outcomeOf,
  upRateTint,
  windowSecForSlug,
  type CircleDatum,
  type HourlyBucket,
  type RecentWindow,
  type WindowOutcome,
} from "../windowStats";
import { Collapsible } from "./Collapsible";

const OUTCOME_BG: Record<WindowOutcome, string> = {
  up: "var(--up)",
  down: "var(--down)",
  unknown: "var(--muted)",
};

function circleLabel(d: CircleDatum): string {
  const base = `${clockHms(d.epoch)} — ${
    d.outcome === "up" ? "עלה 🟢" : d.outcome === "down" ? "ירד 🔴" : "לא ידוע"
  }`;
  if (!d.betSide) return base;
  const mark = d.won === true ? " ✓" : d.won === false ? " ✗" : "";
  return `${base} · הימור ${d.betSide}${mark}`;
}

/** A single window dot: rounded-square, focus/selected ring, optional ✓/✗ for a graded trade. */
export function WindowCircle({
  datum,
  size = 20,
  active = false,
  onClick,
}: {
  datum: CircleDatum;
  size?: number;
  active?: boolean;
  onClick?: (d: CircleDatum) => void;
}) {
  const known = datum.outcome !== "unknown";
  const ringed = active || datum.isFocus;
  const mark = datum.won === true ? "✓" : datum.won === false ? "✗" : "";
  const label = circleLabel(datum);
  return (
    <button
      type="button"
      role="listitem"
      title={label}
      aria-label={label}
      aria-pressed={active}
      onClick={onClick ? () => onClick(datum) : undefined}
      style={{
        width: size,
        height: size,
        padding: 0,
        borderRadius: Math.max(5, Math.round(size * 0.3)),
        background: OUTCOME_BG[datum.outcome],
        opacity: known ? 1 : 0.4,
        border: ringed ? "2px solid var(--accent-bright)" : "1px solid rgba(255,255,255,0.08)",
        boxShadow: active
          ? "0 0 0 3px var(--accent-muted), 0 2px 6px rgba(0,0,0,0.4)"
          : datum.isFocus
          ? "0 0 0 3px rgba(147,169,201,0.16)"
          : "0 1px 3px rgba(0,0,0,0.35)",
        cursor: onClick ? "pointer" : "default",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#fff",
        fontSize: Math.round(size * 0.55),
        fontWeight: 800,
        lineHeight: 1,
        transition: "transform .12s ease, box-shadow .12s ease",
        transform: active ? "translateY(-1px)" : undefined,
      }}
    >
      {mark}
    </button>
  );
}

/** Low-level strip over already-built CircleDatum[]. Renders LTR (time old→new). */
export function CircleStrip({
  data,
  size = 20,
  selectedEpoch = null,
  onSelect,
}: {
  data: CircleDatum[];
  size?: number;
  selectedEpoch?: number | null;
  onSelect?: (epoch: number | null) => void;
}) {
  return (
    <div dir="ltr" role="list" style={{ display: "flex", gap: 5, alignItems: "center", flexWrap: "wrap" }}>
      {data.map((d) => (
        <WindowCircle
          key={d.epoch}
          datum={d}
          size={size}
          active={selectedEpoch === d.epoch}
          onClick={onSelect ? (dd) => onSelect(selectedEpoch === dd.epoch ? null : dd.epoch) : undefined}
        />
      ))}
    </div>
  );
}

/** Clickable strip over raw windows (oldest→newest); the newest window is ringed. */
export function WindowCircles({
  windows,
  selectedEpoch = null,
  onSelect,
  size = 20,
}: {
  windows: RecentWindow[];
  selectedEpoch?: number | null;
  onSelect?: (epoch: number | null) => void;
  size?: number;
}) {
  const sorted = windows
    .filter((w) => Number.isFinite(w.epoch))
    .slice()
    .sort((a, b) => a.epoch - b.epoch);
  const lastEpoch = sorted.length ? sorted[sorted.length - 1].epoch : null;
  const data: CircleDatum[] = sorted.map((w) => ({
    epoch: w.epoch,
    outcome: outcomeOf(w),
    isFocus: w.epoch === lastEpoch, // subtle ring on the latest window
  }));
  return <CircleStrip data={data} size={size} selectedEpoch={selectedEpoch} onSelect={onSelect} />;
}

const fmtUsd0 = (n: number | null | undefined) =>
  n == null ? "—" : `$${Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

/** Detail card shown below a strip for the selected window (time / BTC / drift). */
export function WindowDetailPanel({ window: w, onClose }: { window: RecentWindow; onClose: () => void }) {
  const o = outcomeOf(w);
  const outcomeLabel = o === "up" ? "עלה 🟢" : o === "down" ? "ירד 🔴" : "לא ידוע";
  const outcomeColor = o === "up" ? "var(--up)" : o === "down" ? "var(--down)" : "var(--muted)";
  const durSec = windowSecForSlug(w.slug);
  const start = new Date(w.epoch * 1000);
  const end = new Date((w.epoch + durSec) * 1000);
  const sameDay = start.toDateString() === new Date().toDateString();
  const dateStr = sameDay ? "" : ` · ${start.toLocaleDateString("he-IL")}`;
  const d = driftOf(w);
  const signedUsd = (n: number | null) =>
    n == null ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  const signedPct = (n: number | null) => (n == null ? "" : ` (${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(3)}%)`);
  return (
    <div
      style={{
        marginTop: "var(--s-2)",
        padding: "var(--s-3)",
        paddingInlineStart: 40,
        borderRadius: "var(--radius-sm)",
        background: "var(--card-hover)",
        border: "1px solid var(--border-strong)",
        position: "relative",
      }}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="סגור פרטי חלון"
        style={{
          position: "absolute",
          insetInlineStart: 8,
          top: 8,
          width: 24,
          height: 24,
          borderRadius: 6,
          border: "1px solid var(--border)",
          background: "var(--card)",
          color: "var(--muted)",
          cursor: "pointer",
          lineHeight: 1,
          fontSize: 14,
        }}
      >
        ×
      </button>
      <div style={{ display: "grid", gap: 3, fontSize: 13 }}>
        <div>
          <strong style={{ color: outcomeColor }}>{outcomeLabel}</strong>
        </div>
        <div style={{ color: "var(--text-secondary)" }}>
          מתי:{" "}
          <span dir="ltr" style={{ unicodeBidi: "isolate", display: "inline-block" }}>
            {start.toLocaleTimeString("he-IL")} – {end.toLocaleTimeString("he-IL")}
          </span>
          {dateStr}
        </div>
        <div style={{ color: "var(--text-secondary)" }}>
          BTC:{" "}
          <span dir="ltr" style={{ unicodeBidi: "isolate", display: "inline-block" }}>
            {fmtUsd0(w.btc_open)} → {fmtUsd0(w.btc_close)}
          </span>
        </div>
        {d.abs != null && (
          <div style={{ color: "var(--text-secondary)" }}>
            תזוזה:{" "}
            <span style={{ color: d.abs >= 0 ? "var(--up)" : "var(--down)", fontWeight: 700 }}>
              {signedUsd(d.abs)}
              {signedPct(d.pct)}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function StatTile({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  color?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        padding: "8px 12px",
        borderRadius: "var(--radius-sm)",
        background: "var(--bg)",
        border: "1px solid var(--border)",
        minWidth: 104,
      }}
    >
      <div style={{ fontSize: 11, color: "var(--muted)" }}>{label}</div>
      <div style={{ fontSize: 19, fontWeight: 700, color: color ?? "var(--text)" }}>{value}</div>
      {sub ? <div style={{ fontSize: 11, color: "var(--muted)" }}>{sub}</div> : null}
    </div>
  );
}

/** 24-cell heatmap (hour 0-23 UTC), tinted green↔red by up_rate. */
export function HourlyHeatmap({ hourly }: { hourly: HourlyBucket[] }) {
  const byHour = new Map(hourly.map((h) => [h.hour, h]));
  const cells = Array.from({ length: 24 }, (_, hr) => byHour.get(hr) ?? { hour: hr, total: 0, up_wins: 0, up_rate: 0 });
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: "var(--s-2)" }}>
        הטיה לפי שעה (UTC) — ירוק = נטייה לעלייה 🟢 · אדום = לירידה 🔴
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(12, 1fr)", gap: 3, direction: "ltr" }}>
        {cells.map((h) => {
          const tint = upRateTint(h.up_rate, h.total);
          const title =
            h.total > 0
              ? `שעה ${h.hour}:00 UTC — ${Math.round(h.up_rate * 100)}% עליות (${h.total} חלונות)`
              : `שעה ${h.hour}:00 UTC — אין נתונים`;
          return (
            <div
              key={h.hour}
              title={title}
              style={{
                aspectRatio: "1 / 1",
                borderRadius: 5,
                background: tint.bg,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 10,
                color: h.total > 0 ? tint.fg : "var(--muted)",
                border: "1px solid var(--border)",
              }}
            >
              {h.hour}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Part C — the demo-trades stats windows card body. */
export function WindowsStatsPanel({
  windows,
  hourly,
  selectedEpoch,
  onSelect,
  botWins,
}: {
  windows: RecentWindow[];
  hourly: HourlyBucket[];
  selectedEpoch: number | null;
  onSelect: (e: number | null) => void;
  botWins: { wins: number; n: number; pct: number | null };
}) {
  const s = deriveWindowStats(windows);
  const selected = selectedEpoch != null ? windows.find((w) => w.epoch === selectedEpoch) : undefined;
  const upPct = s.knownN ? Math.round((s.upCount / s.knownN) * 100) : 0;

  return (
    <div dir="rtl" style={{ display: "grid", gap: "var(--s-4)" }}>
      <div style={{ background: "var(--bg)", padding: "var(--s-3)", borderRadius: "var(--radius-sm)" }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: "var(--s-2)" }}>
          {s.total} חלונות אחרונים (ישן → חדש) · לחיצה על עיגול מציגה את הזמן
        </div>
        <WindowCircles windows={windows} selectedEpoch={selectedEpoch} onSelect={onSelect} size={22} />
        {selected && <WindowDetailPanel window={selected} onClose={() => onSelect(null)} />}
      </div>

      {/* up vs down ratio bar */}
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
          <span style={{ color: "var(--up)" }}>עלה {s.upCount} 🟢</span>
          <span style={{ color: "var(--down)" }}>🔴 {s.downCount} ירד</span>
        </div>
        <div style={{ display: "flex", height: 10, borderRadius: 999, overflow: "hidden", background: "var(--card-hover)" }}>
          <div style={{ width: `${upPct}%`, background: "var(--up)", transition: "width .3s ease" }} />
          <div style={{ width: `${100 - upPct}%`, background: "var(--down)", transition: "width .3s ease" }} />
        </div>
        {s.unknownN > 0 && (
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
            ({s.unknownN} חלונות ללא תוצאה ידועה — לא נספרים)
          </div>
        )}
      </div>

      {/* aggregate tiles */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s-2)" }}>
        <StatTile
          label="אחוז עליות"
          value={s.upRate != null ? `${Math.round(s.upRate * 100)}%` : "—"}
          sub={`מתוך ${s.knownN} חלונות ידועים`}
          color={s.upRate != null && s.upRate >= 0.5 ? "var(--up)" : "var(--down)"}
        />
        <StatTile
          label="רצף נוכחי"
          value={s.currentStreak > 0 ? `${s.currentStreak}× ${s.currentStreakDir === "up" ? "🟢" : "🔴"}` : "—"}
          color={s.currentStreakDir === "up" ? "var(--up)" : s.currentStreakDir === "down" ? "var(--down)" : undefined}
        />
        <StatTile label="רצף הכי ארוך" value={`${s.longestStreak}`} />
        <StatTile label="ציון דשדוש" value={`${Math.round(s.chopScore * 100)}%`} sub="100% = 🔴🟢🔴🟢 מלא" color={s.chopScore >= 0.6 ? "var(--accent-bright)" : "var(--muted)"} />
        <StatTile label="ניצחונות הבוט" value={`${botWins.wins}/${botWins.n}`} sub={botWins.pct != null ? `${botWins.pct}%` : "—"} color="var(--up)" />
      </div>

      <Collapsible title="מה זה «ציון דשדוש»?" subtitle="למה זה חשוב לאסטרטגיית Chop-Armed FLW">
        <p style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6, margin: 0 }}>
          הבוט מחכה ל־N חלונות מתחלפים (דשדוש) ואז נכנס לפי המנצח האחרון. ציון גבוה = השוק מדשדש
          (🔴🟢🔴🟢) והבוט קרוב ל“דריכה”. ציון נמוך = מגמה אחת ארוכה, הבוט ימתין.
        </p>
      </Collapsible>

      {hourly.length > 0 && <HourlyHeatmap hourly={hourly} />}
    </div>
  );
}

/** Part D — compact window chip for a single trade's expanded panel. */
export function TradeWindowChip({
  epoch,
  windowSec,
  btcStart,
  btcEnd,
  side,
  resolvedOutcome,
  settleWon,
  recentWindows,
}: {
  epoch: number | null;
  windowSec: number;
  btcStart?: number | null;
  btcEnd?: number | null;
  side?: string;
  resolvedOutcome?: string;
  settleWon?: boolean;
  recentWindows: RecentWindow[];
}) {
  const wv = deriveTradeWindowView({
    epoch,
    windowSec,
    btcStart,
    btcEnd,
    side,
    resolvedOutcome,
    settleWon,
    recentWindows,
  });
  const f = wv.focus;
  if (!f) return null;
  const tint = f.outcome === "up" ? "var(--up-muted)" : f.outcome === "down" ? "var(--down-muted)" : "rgba(122,132,148,0.10)";
  const edge = f.outcome === "up" ? "rgba(74,155,126,0.35)" : f.outcome === "down" ? "rgba(184,92,92,0.35)" : "var(--border)";
  const driftPos = (wv.driftUsd ?? 0) >= 0;
  return (
    <div
      dir="rtl"
      style={{
        marginTop: 10,
        padding: "10px 12px",
        background: tint,
        border: `1px solid ${edge}`,
        borderRadius: "var(--radius-md)",
        boxShadow: "0 1px 3px rgba(0,0,0,0.35)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <WindowCircle datum={f} size={30} />
        <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text)" }}>
            תוצאת החלון: {f.outcome === "up" ? "עלה 🟢" : f.outcome === "down" ? "ירד 🔴" : "לא ידוע"}
            {f.betSide != null && (
              <span style={{ marginInlineStart: 6, color: f.won ? "var(--up)" : "var(--down)", fontWeight: 700 }}>
                · הימור {f.betSide} {f.won === true ? "✓ תואם" : f.won === false ? "✗ לא תואם" : ""}
              </span>
            )}
          </span>
          <span
            dir="ltr"
            style={{ fontSize: 11, color: "var(--text-secondary)", unicodeBidi: "isolate" }}
          >
            {wv.timeStart} – {wv.timeEnd}
          </span>
        </div>
        {wv.driftUsd != null && (
          <div style={{ marginInlineStart: "auto", textAlign: "left", fontSize: 11, color: "var(--muted)", lineHeight: 1.5 }}>
            <div dir="ltr" style={{ unicodeBidi: "isolate" }}>
              BTC ${Number(btcStart).toLocaleString(undefined, { maximumFractionDigits: 2 })} → $
              {Number(btcEnd).toLocaleString(undefined, { maximumFractionDigits: 2 })}
            </div>
            <div style={{ color: driftPos ? "var(--up)" : "var(--down)", fontWeight: 700 }}>
              {driftPos ? "▲" : "▼"} ${Math.abs(wv.driftUsd).toLocaleString(undefined, { maximumFractionDigits: 2 })}
              {wv.driftPct != null ? ` (${driftPos ? "+" : ""}${wv.driftPct.toFixed(3)}%)` : ""}
            </div>
          </div>
        )}
      </div>
      {wv.strip.length > 1 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 10, color: "var(--muted)", marginBottom: 4 }}>
            חלונות סביב העסקה (ישן → חדש) — המסומן = החלון של העסקה
          </div>
          <CircleStrip data={wv.strip} size={18} />
        </div>
      )}
    </div>
  );
}
