"""בדיקות _smart_trim_path: שמירת entry / peak / trough / recent בזמן trim."""
from __future__ import annotations

from demo_engine import _smart_trim_path


def _make_path(n: int) -> list[dict]:
    """יוצר path של N דגימות עם ts=0..n-1 ו-upnl_pct=i."""
    return [
        {"ts": float(i), "upnl_pct": float(i), "bid": 0.5, "balance": 100.0, "equity": 100.0}
        for i in range(n)
    ]


def test_smart_trim_under_cap_returns_same():
    p = _make_path(100)
    out = _smart_trim_path(p, max_len=200, peak_ts=None, trough_ts=None)
    assert out == p  # < cap → unchanged
    assert len(out) == 100


def test_smart_trim_preserves_entry():
    """אחרי trim הדגימה הראשונה (ts=0) נשמרת."""
    p = _make_path(1000)
    out = _smart_trim_path(p, max_len=100, peak_ts=None, trough_ts=None)
    assert len(out) == 100
    assert out[0]["ts"] == 0.0  # entry preserved


def test_smart_trim_preserves_last():
    """אחרי trim הדגימה האחרונה נשמרת."""
    p = _make_path(1000)
    out = _smart_trim_path(p, max_len=100, peak_ts=None, trough_ts=None)
    assert out[-1]["ts"] == 999.0  # last preserved


def test_smart_trim_preserves_peak():
    """ה-ts של peak צריך להופיע ב-output."""
    p = _make_path(1000)
    peak_ts = 234.0  # אמצע
    out = _smart_trim_path(p, max_len=100, peak_ts=peak_ts, trough_ts=None)
    timestamps = {s["ts"] for s in out}
    assert peak_ts in timestamps


def test_smart_trim_preserves_trough():
    p = _make_path(1000)
    trough_ts = 678.0
    out = _smart_trim_path(p, max_len=100, peak_ts=None, trough_ts=trough_ts)
    timestamps = {s["ts"] for s in out}
    assert trough_ts in timestamps


def test_smart_trim_preserves_both_peak_and_trough():
    p = _make_path(1000)
    peak_ts = 123.0
    trough_ts = 456.0
    out = _smart_trim_path(p, max_len=50, peak_ts=peak_ts, trough_ts=trough_ts)
    timestamps = {s["ts"] for s in out}
    assert peak_ts in timestamps
    assert trough_ts in timestamps
    assert 0.0 in timestamps  # entry
    assert 999.0 in timestamps  # last


def test_smart_trim_keeps_recent_window():
    """N הדגימות האחרונות (recent_n = min(100, max_len/4)) נשמרות מלאות."""
    p = _make_path(1000)
    out = _smart_trim_path(p, max_len=200, peak_ts=None, trough_ts=None)
    # recent_n = min(100, 200/4) = 50; אז ts ב-950..999 (50 דגימות) נשמרות מלאות
    ts_in_recent = sorted([s["ts"] for s in out if s["ts"] >= 950])
    assert len(ts_in_recent) == 50  # כל ה-50 האחרונים שם
    assert ts_in_recent == [float(i) for i in range(950, 1000)]


def test_smart_trim_total_size_matches_max():
    p = _make_path(2000)
    out = _smart_trim_path(p, max_len=300, peak_ts=42.0, trough_ts=1500.0)
    # output צריך להיות לכל היותר 300 (יכול להיות 1-2 פחות בגלל overlap בין must_keep ל-uniform sampling)
    assert len(out) <= 300
    assert len(out) >= 290  # אבל קרוב מאוד ל-300


def test_smart_trim_invalid_peak_ts_ignored():
    """peak_ts שלא קיים ב-path לא נופל ולא מוסיף דגימה."""
    p = _make_path(500)
    out = _smart_trim_path(p, max_len=100, peak_ts=99999.0, trough_ts=None)
    assert len(out) <= 100
    timestamps = {s["ts"] for s in out}
    assert 99999.0 not in timestamps  # לא נוסף
    assert 0.0 in timestamps  # entry עדיין שם


def test_smart_trim_5min_window_at_1s_no_trim():
    """5 דקות × 1 דגימה/שנייה = 300 דגימות < 5000 default → אין trim."""
    p = _make_path(300)
    out = _smart_trim_path(p, max_len=5000, peak_ts=150.0, trough_ts=200.0)
    assert len(out) == 300  # נשמר במלואו
    assert out == p


def test_smart_trim_15min_volatile_under_5000_cap():
    """15 דקות × 5 דגימות/שנייה = 4500 דגימות < 5000 → אין trim."""
    p = _make_path(4500)
    out = _smart_trim_path(p, max_len=5000, peak_ts=None, trough_ts=None)
    assert len(out) == 4500
