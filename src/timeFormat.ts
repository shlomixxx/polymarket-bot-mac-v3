// Central time/date display helpers — ALL wall-clock times in the UI are shown in
// Israel time (Asia/Jerusalem), regardless of the viewer's device or the server's
// timezone (Railway runs in UTC). Timestamps are STORED in UTC/epoch; only DISPLAY
// is pinned here. Do not use these for analytics hour-bucketing — those stay UTC.
//
// Input unit is epoch SECONDS unless the function name ends in `Ms`.

const TZ = "Asia/Jerusalem";

/** Label to put next to a clock so it's unambiguous, e.g. `שעה 20:30 (שעון ישראל)`. */
export const ISRAEL_TZ_LABEL = "שעון ישראל";

function invalid(ts: number): boolean {
  return !Number.isFinite(ts) || ts <= 0;
}

/** HH:MM:SS in Israel time. Input: epoch seconds. */
export function israelTime(tsSec: number): string {
  if (invalid(tsSec)) return "—";
  return new Date(tsSec * 1000).toLocaleTimeString("he-IL", {
    timeZone: TZ,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** HH:MM in Israel time. Input: epoch seconds. */
export function israelHM(tsSec: number): string {
  if (invalid(tsSec)) return "—";
  return new Date(tsSec * 1000).toLocaleTimeString("he-IL", {
    timeZone: TZ,
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** DD/MM HH:MM:SS in Israel time. Input: epoch seconds. */
export function israelDateTime(tsSec: number): string {
  if (invalid(tsSec)) return "—";
  return new Date(tsSec * 1000).toLocaleString("he-IL", {
    timeZone: TZ,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** DD/MM HH:MM in Israel time (no seconds). Input: epoch seconds. */
export function israelDateHM(tsSec: number): string {
  if (invalid(tsSec)) return "—";
  return new Date(tsSec * 1000).toLocaleString("he-IL", {
    timeZone: TZ,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** DD/MM/YYYY in Israel time. Input: epoch seconds. */
export function israelDate(tsSec: number): string {
  if (invalid(tsSec)) return "—";
  return new Date(tsSec * 1000).toLocaleDateString("he-IL", {
    timeZone: TZ,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

/** DD/MM HH:MM:SS in Israel time. Input: epoch MILLISECONDS (e.g. audit rows). */
export function israelDateTimeMs(tsMs: number): string {
  if (invalid(tsMs)) return "—";
  return israelDateTime(tsMs / 1000);
}

/** HH:MM:SS in Israel time. Input: epoch MILLISECONDS. */
export function israelTimeMs(tsMs: number): string {
  if (invalid(tsMs)) return "—";
  return israelTime(tsMs / 1000);
}
