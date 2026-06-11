"""בדיקות לאחסון מפתח דרך קובץ (Railway fallback) ב-secret_store.

הקובץ נשמר ב-DATA_ROOT/.polymarket_pk עם chmod 600, שורד restarts.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest


def _isolated_secret_store(tmp_dir: Path):
    """מאתחל secret_store עם DATA_ROOT נקי + Keychain מנוטרל (יבוא חוזר)."""
    os.environ["DATA_ROOT"] = str(tmp_dir)
    import importlib
    import secret_store
    importlib.reload(secret_store)
    # נטרל את ה-keyring שלא יפריע (Mac במחשב פיתוח יש Keychain אמיתי)
    secret_store._keyring_broken = True
    return secret_store


def test_save_and_load_via_file(tmp_path: Path):
    """save_key → load_key מחזיר את אותו ערך, שורד בין importsa."""
    s = _isolated_secret_store(tmp_path)
    assert s.save_key("0xMYSECRETKEY123") is True
    # ה-keyring מנוטרל → load_key חוזר ל-file fallback
    assert s.load_key() == "0xMYSECRETKEY123"


def test_file_persists_across_module_reload(tmp_path: Path):
    """שמירה ב-instance אחד נטענת באחר אחרי reload (מדמה restart)."""
    s1 = _isolated_secret_store(tmp_path)
    assert s1.save_key("0xPERSISTKEY") is True

    # restart simulation: reload המודול עם אותו DATA_ROOT
    import importlib
    import secret_store
    importlib.reload(secret_store)
    secret_store._keyring_broken = True  # שוב מנטרלים Keychain

    assert secret_store.load_key() == "0xPERSISTKEY"


def test_has_persisted_key_returns_true_after_save(tmp_path: Path):
    s = _isolated_secret_store(tmp_path)
    assert s.has_persisted_key() is False
    s.save_key("0xANOTHER")
    assert s.has_persisted_key() is True


def test_delete_removes_file(tmp_path: Path):
    s = _isolated_secret_store(tmp_path)
    s.save_key("0xTOBEDELETED")
    assert s._file_exists() is True
    assert s.delete_key() is True
    assert s._file_exists() is False
    assert s.load_key() is None


def test_file_permissions_are_owner_only(tmp_path: Path):
    """chmod 600 — רק owner קורא וכותב."""
    s = _isolated_secret_store(tmp_path)
    s.save_key("0xCHECKPERMS")
    p = s._file_store_path()
    mode = stat.S_IMODE(p.stat().st_mode)
    # ב-Linux/Mac: 0o600
    if os.name == "posix":
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_empty_key_not_saved(tmp_path: Path):
    s = _isolated_secret_store(tmp_path)
    assert s.save_key("") is False
    assert s.save_key("   ") is False
    assert s.load_key() is None


def test_save_atomic_via_tmp_then_replace(tmp_path: Path):
    """ה-save משתמש ב-tmp+replace, לא בכתיבה ישירה — לא משאיר קובץ חתוך אם נופל באמצע."""
    s = _isolated_secret_store(tmp_path)
    s.save_key("0xFIRST")
    s.save_key("0xSECOND")  # מחליף בלי לאבד את הראשון אם save שני נכשל
    assert s.load_key() == "0xSECOND"
    # לא צריך להיות tmp file שנשאר
    p = s._file_store_path()
    leftovers = [x for x in p.parent.iterdir() if x.name.startswith(".polymarket_pk.tmp")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# SERVICE SCOPING — distinct services map to distinct, non-colliding stores.
# Guards the Binance-vs-Polymarket key collision the wrapper depends on.
# ---------------------------------------------------------------------------

def test_distinct_services_use_distinct_files_no_collision(tmp_path: Path):
    """המאגר של polymarket-bot והמאגר של binance-futures-bot לא מתנגשים: כתיבה
    לאחד לא דורסת/נקראת מהשני, ונשמרים בשני קבצים שונים."""
    s = _isolated_secret_store(tmp_path)
    # legacy/default service (polymarket) keeps the historical filename
    assert s.save_key("0xPOLY", service="polymarket-bot") is True
    # binance service goes to a SEPARATE file
    assert s.save_key("BINKEY\nBINSECRET", service="binance-futures-bot") is True

    # each reads back ONLY its own value
    assert s.load_key(service="polymarket-bot") == "0xPOLY"
    assert s.load_key(service="binance-futures-bot") == "BINKEY\nBINSECRET"

    # they are physically different files
    p_poly = s._file_store_path("polymarket-bot")
    p_bin = s._file_store_path("binance-futures-bot")
    assert p_poly != p_bin
    assert p_poly.name == ".polymarket_pk"           # historical name preserved
    assert p_bin.name == ".binance-futures-bot_pk"   # distinct binance file
    assert p_poly.is_file() and p_bin.is_file()


def test_default_service_is_polymarket_backcompat(tmp_path: Path):
    """ללא service מפורש — ההתנהגות ההיסטורית (polymarket) נשמרת."""
    s = _isolated_secret_store(tmp_path)
    s.save_key("0xLEGACY")  # no service arg
    assert s.load_key() == "0xLEGACY"
    # and it's reachable explicitly under the polymarket service too
    assert s.load_key(service="polymarket-bot") == "0xLEGACY"
    # but NOT under the binance service (no collision)
    assert s.load_key(service="binance-futures-bot") is None


def test_delete_is_scoped_per_service(tmp_path: Path):
    """מחיקה של שירות אחד לא נוגעת בשני."""
    s = _isolated_secret_store(tmp_path)
    s.save_key("0xPOLY", service="polymarket-bot")
    s.save_key("BINKEY\nBINSECRET", service="binance-futures-bot")
    assert s.delete_key(service="binance-futures-bot") is True
    # polymarket survives
    assert s.load_key(service="polymarket-bot") == "0xPOLY"
    assert s.load_key(service="binance-futures-bot") is None
