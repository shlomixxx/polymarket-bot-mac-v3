"""
btc_strategy.py — `trend_pullback_v1` signal engine + candlestick detectors.

This is the HARMLESS half of the BTC TA system: a pure, deterministic signal
engine. There is **zero real-money code here** — no exchange, no live orders,
no sizing. It only looks at OHLCV candles and returns a structured opinion.

Design rules (from the spec):
  * Engine = HIGHER-TIMEFRAME trend (daily). Candles/indicators = confirmation.
  * LONG entry requires ALL FOUR gates (mirror for SHORT), each toggleable via
    config so its contribution is independently measurable:
      1. trend     — price > daily EMA200 AND daily EMA50 > EMA200
      2. pullback  — 4h price pulled back to EMA21 / prior swing-low AND
                     RSI(14) dipped into the 40–50 band (healthy dip)
      3. trigger   — a best-evidenced candle pattern in trend direction at the
                     level, with body > k·ATR
      4. liquidity — (advisory) liquid session only
  * Stop = trigger-candle wick OR 1.5–2×ATR, whichever is TIGHTER.
    If the structural (wick) stop is wider than 2×ATR -> SKIP (flat).
  * Target = entry + RR·risk_distance, with RR >= 2 (2:1 reward:risk).
  * NO MARTINGALE, no adding to losers, no widening stops — there is simply no
    code path here that could do that; sizing/averaging lives nowhere in this
    file by construction.
  * NEVER raises on the hot path: any bad/short/malformed input -> a well-formed
    'flat' result with an explanatory reason.

Candle dict shape: {"open","high","low","close"[, "volume", "open_time"]}.
"""
from __future__ import annotations

from typing import Any, Optional

try:  # Reuse the project's indicator helpers when importable (bare import via conftest).
    from ta_signals import _ema, compute_atr, compute_rsi
except Exception:  # pragma: no cover - fallback for unusual import contexts
    _ema = compute_atr = compute_rsi = None  # type: ignore


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # Gate on/off flags (each measurable in a backtest by toggling).
    "use_trend_gate": True,
    "use_pullback_gate": True,
    "use_trigger_gate": True,
    "use_liquidity_gate": True,
    # Trend gate.
    "ema_fast": 50,
    "ema_slow": 200,
    "trend_ema": 200,        # price must be above this daily EMA for longs
    # Pullback gate (4h).
    "pullback_ema": 21,
    "rsi_period": 14,
    "rsi_pullback_lo": 40.0,
    "rsi_pullback_hi": 50.0,
    "pullback_band_atr": 1.0,   # "near the EMA21" tolerance, in ATR units
    "swing_lookback": 10,       # bars to look back for a prior swing low/high
    # Trigger gate.
    "atr_period": 14,
    "trigger_body_k": 0.5,      # candle body must exceed k * ATR
    # Stop / target.
    "atr_stop_mult": 2.0,       # cap: structural stop must be <= this * ATR
    "atr_stop_mult_tight": 1.5,  # the tighter ATR-based stop distance
    "rr": 2.0,                  # reward:risk, must be >= 2
    "min_rr": 2.0,
    # Liquidity (advisory). When a candle carries a usable 'open_time' (ms epoch)
    # we accept only the US/EU overlap hours; otherwise the gate passes (we can't
    # prove illiquidity, so we don't block on missing data).
    "liquid_hours_utc": list(range(12, 21)),  # 12:00–20:59 UTC overlap window
}


def _merge_config(config: Optional[dict]) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update(config)
    return cfg


# ---------------------------------------------------------------------------
# Safe numeric accessors
# ---------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return v


def _ohlc(candle: Any) -> Optional[tuple[float, float, float, float]]:
    """Return (open, high, low, close) as finite floats, else None."""
    if not isinstance(candle, dict):
        return None
    o = _f(candle.get("open"))
    h = _f(candle.get("high"))
    lo = _f(candle.get("low"))
    c = _f(candle.get("close"))
    if None in (o, h, lo, c):
        return None
    # Basic sanity: high must be the max, low the min.
    if h < max(o, c) or lo > min(o, c) or h < lo:
        return None
    return o, h, lo, c  # type: ignore[return-value]


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, lo: float, c: float) -> float:
    return min(o, c) - lo


def _range(h: float, lo: float) -> float:
    return h - lo


# ---------------------------------------------------------------------------
# Candlestick detectors — pure boolean functions, tolerate malformed input.
# Each takes the recent list of candles (most recent LAST) and returns bool.
# ---------------------------------------------------------------------------

def bullish_engulfing(candles: list[dict]) -> bool:
    """Last candle is a bullish candle whose body engulfs the prior bearish body."""
    if not isinstance(candles, (list, tuple)) or len(candles) < 2:
        return False
    prev = _ohlc(candles[-2])
    cur = _ohlc(candles[-1])
    if prev is None or cur is None:
        return False
    po, ph, pl, pc = prev
    o, h, lo, c = cur
    prev_bearish = pc < po
    cur_bullish = c > o
    engulfs = (o <= pc) and (c >= po) and (_body(o, c) > _body(po, pc))
    return prev_bearish and cur_bullish and engulfs


def bearish_engulfing(candles: list[dict]) -> bool:
    """Last candle is a bearish candle whose body engulfs the prior bullish body."""
    if not isinstance(candles, (list, tuple)) or len(candles) < 2:
        return False
    prev = _ohlc(candles[-2])
    cur = _ohlc(candles[-1])
    if prev is None or cur is None:
        return False
    po, ph, pl, pc = prev
    o, h, lo, c = cur
    prev_bullish = pc > po
    cur_bearish = c < o
    engulfs = (o >= pc) and (c <= po) and (_body(o, c) > _body(po, pc))
    return prev_bullish and cur_bearish and engulfs


def pin_bar(candles: list[dict], direction: str = "bullish", wick_ratio: float = 2.0) -> bool:
    """Pin bar / hammer.

    Bullish (hammer): a long LOWER wick >= wick_ratio * body, small upper wick,
    closing in the upper portion — rejection of lower prices.
    Bearish (shooting star): the mirror with a long UPPER wick.
    """
    if not isinstance(candles, (list, tuple)) or len(candles) < 1:
        return False
    cur = _ohlc(candles[-1])
    if cur is None:
        return False
    o, h, lo, c = cur
    body = _body(o, c)
    rng = _range(h, lo)
    if rng <= 0:
        return False
    up = _upper_wick(o, h, c)
    low = _lower_wick(o, lo, c)
    # A doji-ish zero-body bar uses a tiny epsilon so the ratio stays meaningful.
    body_eff = max(body, rng * 1e-9)
    # The dominant wick must be >= wick_ratio*body AND it must dominate the bar:
    # it has to be at least 60% of the total range, with the opposite wick small
    # relative to it. This rejects near-symmetric candles where one wick is only
    # marginally longer than the other.
    if direction == "bullish":
        return (
            low >= wick_ratio * body_eff
            and low >= 0.6 * rng
            and up <= 0.5 * low
        )
    if direction == "bearish":
        return (
            up >= wick_ratio * body_eff
            and up >= 0.6 * rng
            and low <= 0.5 * up
        )
    return False


# Alias: a hammer is the bullish pin bar.
def hammer(candles: list[dict], wick_ratio: float = 2.0) -> bool:
    return pin_bar(candles, direction="bullish", wick_ratio=wick_ratio)


def morning_star(candles: list[dict]) -> bool:
    """Three-candle bullish reversal: big-down, small-body star, big-up that
    closes above the midpoint of the first candle's body."""
    if not isinstance(candles, (list, tuple)) or len(candles) < 3:
        return False
    c1 = _ohlc(candles[-3])
    c2 = _ohlc(candles[-2])
    c3 = _ohlc(candles[-1])
    if c1 is None or c2 is None or c3 is None:
        return False
    o1, h1, l1, cl1 = c1
    o2, h2, l2, cl2 = c2
    o3, h3, l3, cl3 = c3
    first_bearish = cl1 < o1
    third_bullish = cl3 > o3
    b1 = _body(o1, cl1)
    b2 = _body(o2, cl2)
    b3 = _body(o3, cl3)
    if b1 <= 0 or b3 <= 0:
        return False
    small_star = b2 < b1 * 0.5 and b2 < b3 * 0.5
    midpoint1 = (o1 + cl1) / 2.0
    closes_high = cl3 > midpoint1
    return first_bearish and third_bullish and small_star and closes_high


def evening_star(candles: list[dict]) -> bool:
    """Three-candle bearish reversal (mirror of morning_star)."""
    if not isinstance(candles, (list, tuple)) or len(candles) < 3:
        return False
    c1 = _ohlc(candles[-3])
    c2 = _ohlc(candles[-2])
    c3 = _ohlc(candles[-1])
    if c1 is None or c2 is None or c3 is None:
        return False
    o1, h1, l1, cl1 = c1
    o2, h2, l2, cl2 = c2
    o3, h3, l3, cl3 = c3
    first_bullish = cl1 > o1
    third_bearish = cl3 < o3
    b1 = _body(o1, cl1)
    b2 = _body(o2, cl2)
    b3 = _body(o3, cl3)
    if b1 <= 0 or b3 <= 0:
        return False
    small_star = b2 < b1 * 0.5 and b2 < b3 * 0.5
    midpoint1 = (o1 + cl1) / 2.0
    closes_low = cl3 < midpoint1
    return first_bullish and third_bearish and small_star and closes_low


def doji(candles: list[dict], body_frac: float = 0.1) -> bool:
    """A doji: body is a tiny fraction (<= body_frac) of the full range."""
    if not isinstance(candles, (list, tuple)) or len(candles) < 1:
        return False
    cur = _ohlc(candles[-1])
    if cur is None:
        return False
    o, h, lo, c = cur
    rng = _range(h, lo)
    if rng <= 0:
        return False
    return _body(o, c) <= body_frac * rng


# ---------------------------------------------------------------------------
# Local pure indicator wrappers (fall back to local impls if ta_signals absent)
# ---------------------------------------------------------------------------

def _ema_last(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    if _ema is not None:
        series = _ema(values, period)
        return series[-1] if series else None
    # Local fallback (same recipe as ta_signals._ema).
    k = 2.0 / (period + 1)
    cur = sum(values[:period]) / period
    for v in values[period:]:
        cur = v * k + cur * (1 - k)
    return cur


def _rsi_last(closes: list[float], period: int) -> Optional[float]:
    if compute_rsi is not None:
        return compute_rsi(closes, period)
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_last(candles: list[dict], period: int) -> Optional[float]:
    if compute_atr is not None:
        return compute_atr(candles, period)
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        lo = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def _flat(reasons: list[str], gates: Optional[dict] = None) -> dict[str, Any]:
    return {
        "signal": "flat",
        "entry": None,
        "stop": None,
        "target": None,
        "rr": None,
        "reasons": reasons,
        "gates": gates or {"trend": False, "pullback": False,
                           "trigger": False, "liquidity": False},
    }


def _closes(candles: list[dict]) -> list[float]:
    out = []
    for c in candles:
        oc = _ohlc(c)
        if oc is not None:
            out.append(oc[3])
    return out


def _clean(candles: Any) -> list[dict]:
    """Keep only well-formed candle dicts (order preserved)."""
    if not isinstance(candles, (list, tuple)):
        return []
    return [c for c in candles if _ohlc(c) is not None]


def _swing_low(candles: list[dict], lookback: int) -> Optional[float]:
    window = candles[-lookback:] if len(candles) >= lookback else candles
    lows = [_ohlc(c)[2] for c in window if _ohlc(c) is not None]
    return min(lows) if lows else None


def _swing_high(candles: list[dict], lookback: int) -> Optional[float]:
    window = candles[-lookback:] if len(candles) >= lookback else candles
    highs = [_ohlc(c)[1] for c in window if _ohlc(c) is not None]
    return max(highs) if highs else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_signal(
    daily: Any,
    h4: Any,
    h1: Any,
    *,
    config: Optional[dict] = None,
) -> dict[str, Any]:
    """Evaluate the trend_pullback_v1 strategy across three timeframes.

    Args:
        daily: list of daily OHLCV candle dicts (most recent LAST).
        h4:    list of 4h OHLCV candle dicts (most recent LAST).
        h1:    list of 1h OHLCV candle dicts (most recent LAST), entry timing.
        config: optional overrides of DEFAULT_CONFIG (per-gate flags etc.).

    Returns a dict:
        {signal, entry, stop, target, rr, reasons: [...],
         gates: {trend, pullback, trigger, liquidity}}

    Never raises: any bad input yields a 'flat' result.
    """
    try:
        return _evaluate_signal_inner(daily, h4, h1, config)
    except Exception as exc:  # pragma: no cover - defensive belt-and-suspenders
        return _flat([f"internal error -> flat (safe): {exc!r}"])


def _evaluate_signal_inner(daily, h4, h1, config) -> dict[str, Any]:
    cfg = _merge_config(config)

    daily = _clean(daily)
    h4 = _clean(h4)
    h1 = _clean(h1)

    # The 1h frame is the entry-timing frame; we fall back to 4h for the trigger
    # candle if the 1h frame is missing/short.
    trigger_tf = h1 if len(h1) >= 3 else h4

    if len(daily) < 2 or len(h4) < 5 or len(trigger_tf) < 3:
        return _flat(["insufficient candle history -> flat"])

    daily_closes = _closes(daily)
    h4_closes = _closes(h4)

    # Try LONG first, then SHORT. They are mutually exclusive by trend gate.
    for side in ("long", "short"):
        res = _evaluate_side(side, daily, h4, trigger_tf, daily_closes, h4_closes, cfg)
        if res is not None:
            return res

    return _flat(["no setup: gates not aligned for long or short"],
                 gates={"trend": False, "pullback": False,
                        "trigger": False, "liquidity": False})


def _evaluate_side(side, daily, h4, trigger_tf, daily_closes, h4_closes, cfg):
    """Return a signal dict if THIS side passes the enabled gates, else None.

    Returning None lets the caller try the other side / fall through to flat.
    A wide-stop SKIP, however, returns an explicit flat result (the setup was
    valid but the risk geometry is wrong — that's a decision, not a no-setup).
    """
    is_long = side == "long"
    reasons: list[str] = []
    gates = {"trend": False, "pullback": False, "trigger": False, "liquidity": False}

    # --- Indicators ---
    ema_slow = _ema_last(daily_closes, cfg["ema_slow"])
    ema_fast = _ema_last(daily_closes, cfg["ema_fast"])
    daily_price = daily_closes[-1]

    ema_pull = _ema_last(h4_closes, cfg["pullback_ema"])
    rsi4h = _rsi_last(h4_closes, cfg["rsi_period"])
    atr4h = _atr_last(h4, cfg["atr_period"])
    h4_price = h4_closes[-1]

    # ------------------------------------------------------------------
    # GATE 1 — trend (daily)
    # ------------------------------------------------------------------
    if cfg["use_trend_gate"]:
        if ema_slow is None or ema_fast is None:
            return None  # can't establish trend -> let the other side / flat handle it
        if is_long:
            ok = daily_price > ema_slow and ema_fast > ema_slow
        else:
            ok = daily_price < ema_slow and ema_fast < ema_slow
        if not ok:
            return None
        gates["trend"] = True
        reasons.append(
            f"trend gate: daily price {daily_price:.2f} "
            f"{'>' if is_long else '<'} EMA{cfg['ema_slow']} {ema_slow:.2f} "
            f"and EMA{cfg['ema_fast']} {ema_fast:.2f} "
            f"{'>' if is_long else '<'} EMA{cfg['ema_slow']} -> "
            f"{'uptrend' if is_long else 'downtrend'}"
        )
    else:
        gates["trend"] = True
        reasons.append("trend gate disabled by config")

    # ------------------------------------------------------------------
    # GATE 2 — pullback (4h): near EMA21 / prior swing level + RSI in band
    # ------------------------------------------------------------------
    if cfg["use_pullback_gate"]:
        if ema_pull is None or rsi4h is None or atr4h is None:
            return None
        band = cfg["pullback_band_atr"] * atr4h
        near_ema = abs(h4_price - ema_pull) <= band
        if is_long:
            swing = _swing_low(h4[:-1], cfg["swing_lookback"])
            near_swing = swing is not None and abs(h4_price - swing) <= band
            rsi_ok = cfg["rsi_pullback_lo"] <= rsi4h <= cfg["rsi_pullback_hi"]
        else:
            swing = _swing_high(h4[:-1], cfg["swing_lookback"])
            near_swing = swing is not None and abs(h4_price - swing) <= band
            # Mirror band for shorts: RSI bounced up into a 50–60 retrace.
            rsi_ok = (100.0 - cfg["rsi_pullback_hi"]) <= rsi4h <= (100.0 - cfg["rsi_pullback_lo"])
        if not ((near_ema or near_swing) and rsi_ok):
            return None
        gates["pullback"] = True
        level = "EMA21" if near_ema else "swing level"
        reasons.append(
            f"pullback gate: 4h price {h4_price:.2f} pulled back to {level} "
            f"({ema_pull:.2f}) with RSI {rsi4h:.1f} in healthy-dip band"
        )
    else:
        gates["pullback"] = True
        reasons.append("pullback gate disabled by config")

    # ------------------------------------------------------------------
    # GATE 3 — candle trigger at the level (entry-timing frame)
    # ------------------------------------------------------------------
    trig = trigger_tf[-1]
    trig_ohlc = _ohlc(trig)
    atr_trig = _atr_last(trigger_tf, cfg["atr_period"]) or atr4h
    if cfg["use_trigger_gate"]:
        if trig_ohlc is None or atr_trig is None:
            return None
        to, th, tl, tc = trig_ohlc
        body = _body(to, tc)
        big_enough = body > cfg["trigger_body_k"] * atr_trig
        if is_long:
            patterns = {
                "bullish_engulfing": bullish_engulfing(trigger_tf),
                "hammer/pin_bar": pin_bar(trigger_tf, "bullish"),
                "morning_star": morning_star(trigger_tf),
            }
            in_direction = tc > to  # bullish close
        else:
            patterns = {
                "bearish_engulfing": bearish_engulfing(trigger_tf),
                "shooting_star/pin_bar": pin_bar(trigger_tf, "bearish"),
                "evening_star": evening_star(trigger_tf),
            }
            in_direction = tc < to  # bearish close
        fired = [name for name, hit in patterns.items() if hit]
        # morning/evening star's third candle defines direction; hammer can have
        # a tiny body, so allow the direction check to be satisfied by the
        # pattern itself for those, but require body>k*ATR for the others.
        pattern_ok = bool(fired) and in_direction and big_enough
        if not pattern_ok:
            return None
        gates["trigger"] = True
        reasons.append(
            f"trigger gate: {', '.join(fired)} in trend direction, "
            f"body {body:.2f} > {cfg['trigger_body_k']}*ATR ({cfg['trigger_body_k']*atr_trig:.2f})"
        )
    else:
        gates["trigger"] = True
        reasons.append("trigger gate disabled by config")
        if trig_ohlc is None:  # still need a usable trigger candle for stop geometry
            return None

    # ------------------------------------------------------------------
    # GATE 4 — liquidity (advisory; based on the trigger candle's open_time)
    # ------------------------------------------------------------------
    if cfg["use_liquidity_gate"]:
        ok, why = _liquidity_ok(trig, cfg)
        gates["liquidity"] = ok
        reasons.append(why)
        if not ok:
            return None
    else:
        gates["liquidity"] = True
        reasons.append("liquidity gate disabled by config")

    # ------------------------------------------------------------------
    # Stop / target geometry
    # ------------------------------------------------------------------
    to, th, tl, tc = trig_ohlc  # type: ignore[misc]
    entry = tc
    atr_for_stop = atr_trig if atr_trig is not None else atr4h
    if atr_for_stop is None or atr_for_stop <= 0:
        return _flat(["valid setup but ATR unavailable -> flat (cannot size stop)"],
                     gates=gates)

    if is_long:
        structural_stop = tl  # trigger candle's wick low
        structural_risk = entry - structural_stop
    else:
        structural_stop = th  # trigger candle's wick high
        structural_risk = structural_stop - entry

    if structural_risk <= 0:
        return _flat(["valid setup but trigger wick gives non-positive risk -> flat"],
                     gates=gates)

    # SKIP rule: if the structural (wick) stop is wider than 2*ATR -> flat.
    max_risk = cfg["atr_stop_mult"] * atr_for_stop
    if structural_risk > max_risk:
        reasons.append(
            f"SKIP: structural stop risk {structural_risk:.2f} > "
            f"{cfg['atr_stop_mult']}*ATR ({max_risk:.2f}) -> flat (stop too wide)"
        )
        return _flat(reasons, gates=gates)

    # Stop = the TIGHTER of (structural wick) and (1.5*ATR).
    atr_risk = cfg["atr_stop_mult_tight"] * atr_for_stop
    risk = min(structural_risk, atr_risk)
    if is_long:
        stop = entry - risk
    else:
        stop = entry + risk
    chosen = "wick" if risk == structural_risk else f"{cfg['atr_stop_mult_tight']}*ATR"
    reasons.append(
        f"stop = tighter of wick & {cfg['atr_stop_mult_tight']}*ATR -> "
        f"{chosen} at {stop:.2f} (risk {risk:.2f})"
    )

    rr = max(cfg["rr"], cfg["min_rr"])
    if is_long:
        target = entry + rr * risk
    else:
        target = entry - rr * risk
    reasons.append(
        f"target = entry {entry:.2f} {'+' if is_long else '-'} {rr}*risk -> "
        f"{target:.2f} (R:R {rr:.1f} >= {cfg['min_rr']})"
    )

    return {
        "signal": side,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
        "reasons": reasons,
        "gates": gates,
    }


def _liquidity_ok(candle: Any, cfg: dict) -> tuple[bool, str]:
    """Advisory liquidity check from the candle's open_time (ms epoch, UTC).

    If we have no timestamp we cannot prove illiquidity, so we PASS (and say so).
    """
    ot = candle.get("open_time") if isinstance(candle, dict) else None
    ot = _f(ot)
    if ot is None:
        return True, "liquidity gate: no timestamp -> passed (cannot prove thin session)"
    # ms epoch -> hour of day UTC
    import datetime as _dt
    try:
        hour = _dt.datetime.fromtimestamp(ot / 1000.0, _dt.timezone.utc).hour
    except (OverflowError, OSError, ValueError):
        return True, "liquidity gate: unparseable timestamp -> passed"
    liquid = hour in set(cfg["liquid_hours_utc"])
    if liquid:
        return True, f"liquidity gate: hour {hour:02d}:00 UTC is in the liquid overlap window"
    return False, f"liquidity gate: hour {hour:02d}:00 UTC outside liquid overlap -> blocked"
