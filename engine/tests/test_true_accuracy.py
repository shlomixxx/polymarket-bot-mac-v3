"""בדיקות ל-true_directional_accuracy — הנגזרת ה-read-only של הדיוק הכיווני האמיתי.

ה-dashboard מציג ~72% "אחוז ניצחונות" שזו תווית ה-TP/P&L (settlement_status WIN/LOSS,
רובן יציאות-TP קטנות). ה-GROUND-TRUTH הוא side==resolved_outcome. הבדיקות מוודאות:
  • הדיוק נמדד מ-resolved_outcome מול side, *לעולם* לא מ-settlement_status.
  • שורות לא-פתורות (resolved_outcome מחוץ ל-{Up,Down}) מוחרגות מהמכנה.
  • tp_win_rate_pct מחושב *רק* מ-settlement_status (WIN/(WIN+LOSS)).
  • mean_fill_price_pct הוא ה"מחיר ההוגן" הייחוס.
"""
import pytest

from demo_engine import true_directional_accuracy


def _row(side, resolved_outcome, settlement_status, avg_fill_price=0.5):
    return {
        "side": side,
        "resolved_outcome": resolved_outcome,
        "settlement_status": settlement_status,
        "avg_fill_price": avg_fill_price,
    }


def test_accuracy_uses_resolved_outcome_not_settlement_status():
    # 4 שורות פתורות: 1 כיוון-נכון (Up==Up) ו-3 כיוון-שגוי — דיוק אמיתי = 25%.
    # אבל כל ה-4 מתויגות WIN ב-settlement_status (תווית ה-TP): tp_win_rate = 100%.
    # זה בדיוק הפער שהמשימה מתארת — חובה שהדיוק *לא* יושפע מ-settlement_status.
    rows = [
        _row("Up", "Up", "WIN"),      # כיוון נכון
        _row("Up", "Down", "WIN"),    # כיוון שגוי (אבל TP-win)
        _row("Down", "Up", "WIN"),    # כיוון שגוי (אבל TP-win)
        _row("Down", "Up", "WIN"),    # כיוון שגוי (אבל TP-win)
    ]
    res = true_directional_accuracy(rows)
    assert res["n_resolved"] == 4
    assert res["directional_accuracy_pct"] == pytest.approx(25.0)
    # tp_win_rate מ-settlement_status בלבד — מטעה לעומת הדיוק האמיתי.
    assert res["tp_win_rate_pct"] == pytest.approx(100.0)
    # הוכחה שהדיוק שונה לחלוטין מ-tp_win_rate.
    assert res["directional_accuracy_pct"] != res["tp_win_rate_pct"]


def test_unresolved_rows_excluded_from_denominator():
    # רק 2 פתורות (Up,Down); SETTLE_UNKNOWN/PENDING/None/void לא נספרות במכנה.
    rows = [
        _row("Up", "Up", "WIN"),          # פתורה, נכונה
        _row("Down", "Down", "LOSS"),     # פתורה, נכונה
        _row("Up", "UNKNOWN", "WIN"),     # לא-פתורה — מוחרגת
        _row("Up", "PENDING", "PENDING"), # לא-פתורה — מוחרגת
        _row("Up", None, "WIN"),          # לא-פתורה — מוחרגת
        _row("Up", "", "WIN"),            # לא-פתורה — מוחרגת
    ]
    res = true_directional_accuracy(rows)
    assert res["n_resolved"] == 2
    assert res["directional_accuracy_pct"] == pytest.approx(100.0)
    assert res["n_total"] == 6


def test_tp_win_rate_only_from_win_loss_settlement_status():
    # tp_win_rate = WIN/(WIN+LOSS). PENDING/UNKNOWN לא נכנסות למונה/מכנה של ה-TP.
    rows = [
        _row("Up", "Up", "WIN"),
        _row("Up", "Up", "WIN"),
        _row("Down", "Down", "LOSS"),
        _row("Up", "PENDING", "PENDING"),  # לא WIN ולא LOSS — מתעלמים ב-tp_win_rate
        _row("Up", "UNKNOWN", "UNKNOWN"),
    ]
    res = true_directional_accuracy(rows)
    # 2 WIN מתוך 3 (WIN+LOSS) = 66.67%
    assert res["tp_win_rate_pct"] == pytest.approx(66.67, abs=0.01)


def test_mean_fill_price_is_fair_price_reference():
    # avg_fill_price נשמר כשבר [0,1]; מוצג כאחוז. ממוצע 0.40 ו-0.54 -> 47%.
    rows = [
        _row("Up", "Up", "WIN", avg_fill_price=0.40),
        _row("Down", "Up", "LOSS", avg_fill_price=0.54),
    ]
    res = true_directional_accuracy(rows)
    assert res["mean_fill_price_pct"] == pytest.approx(47.0)
    # דיוק 50% ≈ מחיר הוגן 47% — אין edge משמעותי, בדיוק ההמחשה שב-UI.
    assert res["directional_accuracy_pct"] == pytest.approx(50.0)


def test_empty_and_all_unresolved_return_none_metrics():
    assert true_directional_accuracy([]) == {
        "directional_accuracy_pct": None,
        "n_resolved": 0,
        "mean_fill_price_pct": None,
        "tp_win_rate_pct": None,
        "n_total": 0,
    }
    # שורות בלי resolved_outcome פתור ובלי WIN/LOSS — הכל None פרט ל-n_total.
    rows = [_row("Up", "PENDING", "PENDING"), _row("Down", None, "")]
    res = true_directional_accuracy(rows)
    assert res["directional_accuracy_pct"] is None
    assert res["mean_fill_price_pct"] is None
    assert res["tp_win_rate_pct"] is None
    assert res["n_resolved"] == 0
    assert res["n_total"] == 2


def test_resilient_to_malformed_rows_and_bad_fill_price():
    rows = [
        _row("Up", "Up", "WIN", avg_fill_price="not-a-number"),  # fill price פגום — מדולג
        _row("Down", "Down", "LOSS", avg_fill_price=None),       # fill price חסר — מדולג
        None,                                                     # שורה לא-dict — מדולגת
        "garbage",
    ]
    res = true_directional_accuracy(rows)
    assert res["n_resolved"] == 2
    assert res["directional_accuracy_pct"] == pytest.approx(100.0)
    # לא נצברו מחירים תקינים -> None.
    assert res["mean_fill_price_pct"] is None


def test_missing_side_counts_as_incorrect_not_crash():
    rows = [
        _row(None, "Up", "WIN"),   # side חסר — לא נכון, אבל נספר במכנה (resolved)
        _row("Up", "Up", "WIN"),   # נכון
    ]
    res = true_directional_accuracy(rows)
    assert res["n_resolved"] == 2
    assert res["directional_accuracy_pct"] == pytest.approx(50.0)
