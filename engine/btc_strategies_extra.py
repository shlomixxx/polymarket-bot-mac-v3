"""
btc_strategies_extra.py — THREE genuinely-different BTC strategy variants.

The HARMLESS half, again: pure, deterministic signal engines. There is **zero
real-money code here** — no exchange, no live orders, no sizing, no averaging,
no martingale. Each variant only reads OHLCV candles and returns a structured
opinion in the SAME contract as `btc_strategy.evaluate_signal`:

    {signal: 'long'|'short'|'flat', entry, stop, target, rr, reasons: [...]}

…which is exactly what `risk_engine.gate_order` and `backtester.backtest`
consume. The geometry guarantee for an actionable side is:

    long :  stop < entry < target
    short:  target < entry < stop

These three variants exist so the backtester can measure DIFFERENT hypotheses
against the same fees/slippage/funding and the same buy-and-hold baseline:

  1. `mean_reversion_v1` — the research's most promising honest test. The only
     measurable short-horizon BTC signal is MEAN-REVERSION (momentum bets the
     wrong way), so we buy OVERSOLD dips and (mirror) fade OVERBOUGHT spikes,
     with a TOGGLEABLE long-term trend filter so "buy dips, not falling knives"
     is independently measurable. Target = reversion to the mean (middle
     Bollinger). Stop = recent swing extreme or 1.5–2*ATR; SKIP if stop > 2*ATR.

  2. `ma_crossover_v1` — classic TREND following. LONG while the fast MA is above
     the slow MA (e.g. EMA50 > EMA200, a "golden cross"); SHORT while it's below
     ("death cross"). ATR stop + a trailing target. Configurable fast/slow.

  3. `donchian_breakout_v1` — LONG on a close above the prior N-period high; SHORT
     on a close below the prior N-period low. ATR stop, target = N-period channel
     width projected, exit otherwise handled by the backtester's opposite-touch.
     Configurable N.

EACH function is a strategy_fn matching the backtester contract and NEVER raises:
any bad / short / malformed input yields a well-formed 'flat' result. Two call
shapes are supported so the same function works in unit tests AND in the replay:

  * direct core:   strategy(candles, config)            -> signal dict
  * backtester:    strategy(view_by_tf, i)              -> signal dict
                   (view_by_tf == {"1h":[...], "4h":[...], ...}; we read base_tf)

NO MARTINGALE anywhere: there is simply no code path here that doubles, averages,
adds to a loser, sizes up after a loss, or widens a stop. Sizing lives nowhere in
this file by construction — it is delegated entirely to risk_engine downstream.

Candle dict shape: {"open","high","low","close"[, "volume", "open_time"]}.
"""
from __future__ import annotations

import math
from typing import Any, Optional

try:  # Reuse the project's indicator helpers when importable (bare import).
    from ta_signals import _ema, compute_atr, compute_bollinger, compute_rsi
except Exception:  # pragma: no cover - fallback for unusual import contexts
    _ema = compute_atr = compute_bollinger = compute_rsi = None  # type: ignore


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # Which timeframe a backtester {tf: candles} view should be read from.
    "base_tf": "1h",

    # ---- mean_reversion_v1 ----
    "mr_rsi_period": 14,
    "mr_rsi_oversold": 30.0,        # LONG when RSI < this …
    "mr_rsi_overbought": 70.0,      # … SHORT when RSI > this
    "mr_rsi_exit": 50.0,            # "reverted to the mean" reference (advisory)
    "mr_bb_period": 20,
    "mr_bb_std": 2.0,               # below lower band (long) / above upper (short)
    "mr_use_trend_filter": True,    # buy dips above EMA200 only (toggleable)
    "mr_trend_ema": 200,            # the long-term trend filter EMA
    "mr_atr_period": 14,
    "mr_atr_stop_mult": 1.75,       # ATR-based stop distance (1.5–2*ATR band)
    "mr_atr_stop_cap": 2.0,         # SKIP if the chosen stop > this * ATR
    "mr_swing_lookback": 10,        # recent swing low/high for the structural stop
    "mr_min_rr": 2.0,               # require target to clear this R:R

    # ---- ma_crossover_v1 ----
    "ma_fast": 50,
    "ma_slow": 200,
    "ma_atr_period": 14,
    "ma_atr_stop_mult": 2.0,        # ATR stop distance below/above entry
    "ma_rr": 2.0,                   # trailing target projected at this R:R

    # ---- donchian_breakout_v1 ----
    "dc_period": 20,                # N-period channel
    "dc_atr_period": 14,
    "dc_atr_stop_mult": 2.0,        # ATR stop distance
    "dc_rr": 2.0,                   # target projected at this R:R
}


def _merge_config(config: Optional[dict]) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update(config)
    return cfg


# ---------------------------------------------------------------------------
# Safe numeric accessors (identical contract to btc_strategy._f / _ohlc)
# ---------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / +-inf
        return None
    return v


def _ohlc(candle: Any) -> Optional[tuple[float, float, float, float]]:
    """Return (open, high, low, close) as finite floats, else None."""
    if not isinstance(candle, dict):
        return None
    o = _f(candle.get("open"))
    h = _f(candle.get("high"))
    lo = _f(candle.get("low"))
    cl = _f(candle.get("close"))
    if None in (o, h, lo, cl):
        return None
    # Basic sanity: high is the max, low the min.
    if h < max(o, cl) or lo > min(o, cl) or h < lo:
        return None
    return o, h, lo, cl  # type: ignore[return-value]


def _clean(candles: Any) -> list[dict]:
    """Keep only well-formed candle dicts (order preserved)."""
    if not isinstance(candles, (list, tuple)):
        return []
    return [c for c in candles if _ohlc(c) is not None]


def _closes(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for c in candles:
        oc = _ohlc(c)
        if oc is not None:
            out.append(oc[3])
    return out


# ---------------------------------------------------------------------------
# Local pure indicator fallbacks (used if ta_signals is not importable).
# Each mirrors the ta_signals recipe exactly so results are identical.
# ---------------------------------------------------------------------------

def _ema_last(values: list[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    if _ema is not None:
        series = _ema(values, period)
        return series[-1] if series else None
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
        try:
            return compute_atr(candles, period)
        except Exception:  # pragma: no cover - ta_signals indexes raw keys
            pass
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        oc = _ohlc(candles[i])
        pc_oc = _ohlc(candles[i - 1])
        if oc is None or pc_oc is None:
            return None
        _, h, lo, _c = oc
        pc = pc_oc[3]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _bollinger(closes: list[float], period: int, num_std: float):
    """Return (mid, upper, lower) for the trailing `period` closes, else
    (None, None, None). Uses ta_signals' population-std recipe."""
    if period <= 0 or len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    std = math.sqrt(var)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def _swing_low(candles: list[dict], lookback: int) -> Optional[float]:
    window = candles[-lookback:] if lookback and len(candles) >= lookback else candles
    lows = [_ohlc(c)[2] for c in window if _ohlc(c) is not None]  # type: ignore[index]
    return min(lows) if lows else None


def _swing_high(candles: list[dict], lookback: int) -> Optional[float]:
    window = candles[-lookback:] if lookback and len(candles) >= lookback else candles
    highs = [_ohlc(c)[1] for c in window if _ohlc(c) is not None]  # type: ignore[index]
    return max(highs) if highs else None


# ---------------------------------------------------------------------------
# Flat result + the (view|candles, i|config) calling-convention shim
# ---------------------------------------------------------------------------

def _flat(reasons: list[str]) -> dict[str, Any]:
    return {
        "signal": "flat",
        "entry": None,
        "stop": None,
        "target": None,
        "rr": None,
        "reasons": list(reasons),
    }


def _resolve_candles(a: Any, b: Any, cfg: dict) -> list[dict]:
    """Accept BOTH supported call shapes and return a clean candle list.

    * core / unit-test shape:  fn(candles_list, config_dict_or_None)
    * backtester shape:        fn(view_by_tf_dict, bar_index_int)

    In the backtester shape we read the configured base timeframe (default
    '1h'). The backtester already slices each tf for no-look-ahead, so we only
    ever see CLOSED bars [0..i]; we never index `i` ourselves.
    """
    # backtester: a is a {tf: candles} dict, b is the integer bar index.
    if isinstance(a, dict) and not _looks_like_candle(a):
        base_tf = cfg.get("base_tf", "1h")
        series = a.get(base_tf)
        if series is None:
            # Fall back to the single longest series present (robustness).
            lists = [v for v in a.values() if isinstance(v, (list, tuple))]
            series = max(lists, key=len) if lists else []
        return _clean(series)
    # core shape: a is the candle list itself.
    return _clean(a)


def _looks_like_candle(d: dict) -> bool:
    return all(k in d for k in ("open", "high", "low", "close"))


def _resolve_config(a: Any, b: Any) -> dict[str, Any]:
    """Pull the config out of whichever argument carries it.

    In the core shape the 2nd arg is the config dict (or None). In the
    backtester shape the 2nd arg is the bar index (an int) and there is no
    per-call config, so we use defaults.
    """
    if isinstance(b, dict):
        return _merge_config(b)
    return _merge_config(None)


# ---------------------------------------------------------------------------
# Geometry helper — build a long/short signal and ENFORCE stop<entry<target.
# Returns a flat result (never an exception) if the geometry is invalid.
# ---------------------------------------------------------------------------

def _build_signal(side: str, entry: float, stop: float, target: float,
                  reasons: list[str]) -> dict[str, Any]:
    en = _f(entry)
    st = _f(stop)
    tg = _f(target)
    if en is None or st is None or tg is None or en <= 0:
        return _flat(reasons + ["invalid entry/stop/target -> flat"])
    if side == "long":
        if not (st < en < tg):
            return _flat(reasons + [
                f"long geometry invalid (need stop<{en:.4f}<target; "
                f"got stop {st:.4f}, target {tg:.4f}) -> flat"
            ])
        risk = en - st
        rr = (tg - en) / risk if risk > 0 else 0.0
    elif side == "short":
        if not (tg < en < st):
            return _flat(reasons + [
                f"short geometry invalid (need target<{en:.4f}<stop; "
                f"got stop {st:.4f}, target {tg:.4f}) -> flat"
            ])
        risk = st - en
        rr = (en - tg) / risk if risk > 0 else 0.0
    else:  # pragma: no cover - defensive
        return _flat(reasons + [f"unknown side {side!r} -> flat"])
    return {
        "signal": side,
        "entry": en,
        "stop": st,
        "target": tg,
        "rr": rr,
        "reasons": list(reasons),
    }


# ===========================================================================
# 1. mean_reversion_v1 — the promising one (buy oversold dips / fade spikes)
# ===========================================================================

def mean_reversion_v1(a: Any = None, b: Any = None) -> dict[str, Any]:
    """Mean-reversion: buy OVERSOLD dips, fade OVERBOUGHT spikes.

    LONG when price is oversold — RSI(period) < oversold OR close below the lower
    Bollinger band — AND (if the trend filter is on) the close is still above the
    long-term trend EMA, so we buy dips in an uptrend, not falling knives.
    Target = reversion to the mean (the middle Bollinger band). Stop = the recent
    swing low OR `mr_atr_stop_mult`*ATR, whichever is TIGHTER; SKIP -> flat if the
    chosen stop is wider than `mr_atr_stop_cap`*ATR. SHORT is the mirror.

    The trend filter is toggleable via `mr_use_trend_filter` so its contribution
    is independently measurable in a backtest.

    Never raises -> 'flat' on any bad/short/malformed input.
    """
    try:
        cfg = _resolve_config(a, b)
        candles = _resolve_candles(a, b, cfg)
        return _mean_reversion_inner(candles, cfg)
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        return _flat([f"internal error -> flat (safe): {exc!r}"])


def _mean_reversion_inner(candles: list[dict], cfg: dict) -> dict[str, Any]:
    rsi_p = int(cfg["mr_rsi_period"])
    bb_p = int(cfg["mr_bb_period"])
    atr_p = int(cfg["mr_atr_period"])
    trend_p = int(cfg["mr_trend_ema"])
    use_trend = bool(cfg["mr_use_trend_filter"])

    # Enough history for every indicator we touch (Bollinger, RSI, ATR, and the
    # trend EMA when enabled).
    need = max(bb_p, rsi_p + 1, atr_p + 1, (trend_p if use_trend else 0))
    if len(candles) < need:
        return _flat([f"insufficient history for mean_reversion ({len(candles)}<{need}) -> flat"])

    closes = _closes(candles)
    price = closes[-1]

    rsi = _rsi_last(closes, rsi_p)
    mid, upper, lower = _bollinger(closes, bb_p, _f(cfg["mr_bb_std"]) or 2.0)
    atr = _atr_last(candles, atr_p)
    trend_ema = _ema_last(closes, trend_p) if use_trend else None

    if rsi is None or mid is None or atr is None or atr <= 0:
        return _flat(["mean_reversion: indicators unavailable -> flat"])
    if use_trend and trend_ema is None:
        return _flat(["mean_reversion: trend EMA unavailable -> flat"])

    oversold = (rsi < _f(cfg["mr_rsi_oversold"])) or (lower is not None and price < lower)
    overbought = (rsi > _f(cfg["mr_rsi_overbought"])) or (upper is not None and price > upper)

    reasons: list[str] = []
    if use_trend:
        reasons.append(f"trend filter ON: EMA{trend_p}={trend_ema:.2f}")
    else:
        reasons.append("trend filter OFF (measuring its contribution)")

    # ---- LONG: oversold, and (filter) still above the long-term trend. ----
    if oversold and not overbought:
        if use_trend and not (price > trend_ema):  # type: ignore[operator]
            return _flat(reasons + [
                f"oversold but price {price:.2f} <= trend EMA{trend_p} "
                f"{trend_ema:.2f} -> skip falling knife (flat)"  # type: ignore[str-format]
            ])
        reasons.append(
            f"LONG setup: RSI {rsi:.1f} < {cfg['mr_rsi_oversold']} or close "
            f"{price:.2f} < lowerBB {lower:.2f} -> oversold"
            if lower is not None else
            f"LONG setup: RSI {rsi:.1f} oversold"
        )
        # Stop = tighter of (recent swing low) and (mr_atr_stop_mult*ATR).
        swing = _swing_low(candles[:-1], int(cfg["mr_swing_lookback"]))
        atr_risk = (_f(cfg["mr_atr_stop_mult"]) or 1.75) * atr
        struct_risk = (price - swing) if (swing is not None and swing < price) else None
        risk, chosen = _choose_risk(struct_risk, atr_risk)
        cap = (_f(cfg["mr_atr_stop_cap"]) or 2.0) * atr
        if risk > cap:
            return _flat(reasons + [
                f"SKIP: stop risk {risk:.2f} > {cfg['mr_atr_stop_cap']}*ATR "
                f"({cap:.2f}) -> flat (stop too wide)"
            ])
        stop = price - risk
        # Target = reversion to the mean (mid band), but never below the min R:R.
        min_rr = _f(cfg["mr_min_rr"]) or 2.0
        target = max(mid, price + min_rr * risk)
        reasons.append(
            f"stop = tighter of swing-low & {cfg['mr_atr_stop_mult']}*ATR -> "
            f"{chosen} (risk {risk:.2f}); target = revert-to-mean {mid:.2f} "
            f"(>= {min_rr}R)"
        )
        return _build_signal("long", price, stop, target, reasons)

    # ---- SHORT: overbought, and (filter) still below the long-term trend. ----
    if overbought and not oversold:
        if use_trend and not (price < trend_ema):  # type: ignore[operator]
            return _flat(reasons + [
                f"overbought but price {price:.2f} >= trend EMA{trend_p} "
                f"{trend_ema:.2f} -> skip catching a rocket (flat)"  # type: ignore[str-format]
            ])
        reasons.append(
            f"SHORT setup: RSI {rsi:.1f} > {cfg['mr_rsi_overbought']} or close "
            f"{price:.2f} > upperBB {upper:.2f} -> overbought"
            if upper is not None else
            f"SHORT setup: RSI {rsi:.1f} overbought"
        )
        swing = _swing_high(candles[:-1], int(cfg["mr_swing_lookback"]))
        atr_risk = (_f(cfg["mr_atr_stop_mult"]) or 1.75) * atr
        struct_risk = (swing - price) if (swing is not None and swing > price) else None
        risk, chosen = _choose_risk(struct_risk, atr_risk)
        cap = (_f(cfg["mr_atr_stop_cap"]) or 2.0) * atr
        if risk > cap:
            return _flat(reasons + [
                f"SKIP: stop risk {risk:.2f} > {cfg['mr_atr_stop_cap']}*ATR "
                f"({cap:.2f}) -> flat (stop too wide)"
            ])
        stop = price + risk
        min_rr = _f(cfg["mr_min_rr"]) or 2.0
        target = min(mid, price - min_rr * risk)
        reasons.append(
            f"stop = tighter of swing-high & {cfg['mr_atr_stop_mult']}*ATR -> "
            f"{chosen} (risk {risk:.2f}); target = revert-to-mean {mid:.2f} "
            f"(>= {min_rr}R)"
        )
        return _build_signal("short", price, stop, target, reasons)

    return _flat(reasons + [
        f"no extreme: RSI {rsi:.1f} in band and price within Bollinger -> flat"
    ])


def _choose_risk(struct_risk: Optional[float], atr_risk: float) -> tuple[float, str]:
    """The TIGHTER of a positive structural risk and the ATR risk.

    If the structural distance is missing / non-positive, fall back to ATR. This
    never widens a stop relative to ATR — it can only tighten it (or keep ATR).
    """
    if struct_risk is not None and struct_risk > 0:
        if struct_risk <= atr_risk:
            return struct_risk, "swing"
        return atr_risk, "ATR"
    return atr_risk, "ATR"


# ===========================================================================
# 2. ma_crossover_v1 — classic trend (golden/death cross)
# ===========================================================================

def ma_crossover_v1(a: Any = None, b: Any = None) -> dict[str, Any]:
    """Trend following on a fast/slow EMA cross.

    LONG while EMA(fast) > EMA(slow) (a "golden cross" regime); SHORT while
    EMA(fast) < EMA(slow) ("death cross"); flat when they're equal / unavailable.
    Stop = `ma_atr_stop_mult`*ATR from entry; target projected at `ma_rr` R:R
    (the backtester trails the position out via stop/target touch, so the target
    acts as the trailing take-profit). Configurable fast/slow.

    Never raises -> 'flat' on any bad/short/malformed input.
    """
    try:
        cfg = _resolve_config(a, b)
        candles = _resolve_candles(a, b, cfg)
        return _ma_crossover_inner(candles, cfg)
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        return _flat([f"internal error -> flat (safe): {exc!r}"])


def _ma_crossover_inner(candles: list[dict], cfg: dict) -> dict[str, Any]:
    fast = int(cfg["ma_fast"])
    slow = int(cfg["ma_slow"])
    atr_p = int(cfg["ma_atr_period"])
    if fast <= 0 or slow <= 0 or fast >= slow:
        return _flat([f"ma_crossover: need 0<fast<slow (got {fast},{slow}) -> flat"])

    need = max(slow, atr_p + 1)
    if len(candles) < need:
        return _flat([f"insufficient history for ma_crossover ({len(candles)}<{need}) -> flat"])

    closes = _closes(candles)
    price = closes[-1]
    ema_fast = _ema_last(closes, fast)
    ema_slow = _ema_last(closes, slow)
    atr = _atr_last(candles, atr_p)
    if ema_fast is None or ema_slow is None or atr is None or atr <= 0:
        return _flat(["ma_crossover: indicators unavailable -> flat"])

    stop_mult = _f(cfg["ma_atr_stop_mult"]) or 2.0
    rr = _f(cfg["ma_rr"]) or 2.0
    risk = stop_mult * atr

    if ema_fast > ema_slow:
        reasons = [
            f"golden cross: EMA{fast} {ema_fast:.2f} > EMA{slow} {ema_slow:.2f} "
            f"-> uptrend (LONG)",
            f"stop = {stop_mult}*ATR ({risk:.2f}) below entry; "
            f"target = {rr}R trailing",
        ]
        return _build_signal("long", price, price - risk, price + rr * risk, reasons)

    if ema_fast < ema_slow:
        reasons = [
            f"death cross: EMA{fast} {ema_fast:.2f} < EMA{slow} {ema_slow:.2f} "
            f"-> downtrend (SHORT)",
            f"stop = {stop_mult}*ATR ({risk:.2f}) above entry; "
            f"target = {rr}R trailing",
        ]
        return _build_signal("short", price, price + risk, price - rr * risk, reasons)

    return _flat([f"EMA{fast} == EMA{slow} -> no trend (flat)"])


# ===========================================================================
# 3. donchian_breakout_v1 — N-period channel breakout
# ===========================================================================

def donchian_breakout_v1(a: Any = None, b: Any = None) -> dict[str, Any]:
    """Donchian channel breakout.

    LONG when the latest close prints above the highest HIGH of the prior N bars
    (a new N-period high); SHORT when it closes below the lowest LOW of the prior
    N bars. Stop = `dc_atr_stop_mult`*ATR from entry; target projected at `dc_rr`
    R:R. The backtester's opposite-touch exit closes a position that reverses
    into the other side of the channel via the stop. Configurable N (`dc_period`).

    Never raises -> 'flat' on any bad/short/malformed input.
    """
    try:
        cfg = _resolve_config(a, b)
        candles = _resolve_candles(a, b, cfg)
        return _donchian_inner(candles, cfg)
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        return _flat([f"internal error -> flat (safe): {exc!r}"])


def _donchian_inner(candles: list[dict], cfg: dict) -> dict[str, Any]:
    n = int(cfg["dc_period"])
    atr_p = int(cfg["dc_atr_period"])
    if n <= 0:
        return _flat([f"donchian: dc_period must be > 0 (got {n}) -> flat"])

    # We compare the latest CLOSED bar to the channel of the N bars BEFORE it.
    need = max(n + 1, atr_p + 1)
    if len(candles) < need:
        return _flat([f"insufficient history for donchian ({len(candles)}<{need}) -> flat"])

    prior = candles[:-1]
    cur = _ohlc(candles[-1])
    if cur is None:
        return _flat(["donchian: malformed latest bar -> flat"])
    _o, _h, _lo, close = cur

    channel = prior[-n:]
    prior_high = _swing_high(channel, n)
    prior_low = _swing_low(channel, n)
    atr = _atr_last(candles, atr_p)
    if prior_high is None or prior_low is None or atr is None or atr <= 0:
        return _flat(["donchian: indicators unavailable -> flat"])

    stop_mult = _f(cfg["dc_atr_stop_mult"]) or 2.0
    rr = _f(cfg["dc_rr"]) or 2.0
    risk = stop_mult * atr

    if close > prior_high:
        reasons = [
            f"breakout UP: close {close:.2f} > prior-{n}-high {prior_high:.2f} (LONG)",
            f"stop = {stop_mult}*ATR ({risk:.2f}) below entry; target = {rr}R",
        ]
        return _build_signal("long", close, close - risk, close + rr * risk, reasons)

    if close < prior_low:
        reasons = [
            f"breakout DOWN: close {close:.2f} < prior-{n}-low {prior_low:.2f} (SHORT)",
            f"stop = {stop_mult}*ATR ({risk:.2f}) above entry; target = {rr}R",
        ]
        return _build_signal("short", close, close + risk, close - rr * risk, reasons)

    return _flat([
        f"inside channel: prior-{n}-low {prior_low:.2f} <= close {close:.2f} "
        f"<= prior-{n}-high {prior_high:.2f} -> flat"
    ])


# ===========================================================================
# Backtester factories — return a fresh strat(view, i) for a config combo.
# These mirror run_btc_backtest.make_strategy so each variant drops straight
# into backtester.backtest / grid_search / walk_forward.
# ===========================================================================

def _make(core, params: dict):
    cfg = _merge_config(params if isinstance(params, dict) else {})

    def strat(view, i):  # noqa: ARG001 - i unused; backtester pre-slices the view
        candles = _resolve_candles(view, i, cfg)
        sig = core(candles, cfg)
        side = sig.get("signal")
        if side not in ("long", "short"):
            return {"signal": "flat", "entry": None, "stop": None, "target": None}
        return {
            "signal": side,
            "entry": sig.get("entry"),
            "stop": sig.get("stop"),
            "target": sig.get("target"),
        }

    return strat


def make_mean_reversion(params: Optional[dict] = None):
    """Factory: a backtester strategy_fn for mean_reversion_v1."""
    return _make(lambda candles, cfg: _mean_reversion_inner(candles, cfg), params or {})


def make_ma_crossover(params: Optional[dict] = None):
    """Factory: a backtester strategy_fn for ma_crossover_v1."""
    return _make(lambda candles, cfg: _ma_crossover_inner(candles, cfg), params or {})


def make_donchian_breakout(params: Optional[dict] = None):
    """Factory: a backtester strategy_fn for donchian_breakout_v1."""
    return _make(lambda candles, cfg: _donchian_inner(candles, cfg), params or {})
