"""
אחסון קבוע למפתח ה-Polymarket דרך מערכת האבטחה של מערכת ההפעלה.

במק: macOS Keychain (system service)
בלינוקס: Secret Service (gnome-keyring וכדו׳)
בווינדוס: Windows Credential Manager

אם הספרייה `keyring` לא מותקנת או שאין backend זמין (Railway/Docker) —
fallback אוטומטי לקובץ ב-DATA_ROOT/.polymarket_pk עם chmod 600 (Volume של
Railway → שורד deploys). המפתח עצמו אף פעם לא עובר ללוגים.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Optional

SERVICE = "polymarket-bot"
USERNAME = "default"

_log = logging.getLogger(__name__)
_warned_unavailable = False
# After the first failed operation (import or backend), stop trying
_keyring_broken = False


def _file_name_for(service: str) -> str:
    """שם קובץ ה-fallback לפי ה-SERVICE.

    ברירת המחדל (`polymarket-bot`) שומרת על השם ההיסטורי `.polymarket_pk` כדי לא
    לשבור התקנות קיימות. שירותים אחרים (כגון `binance-futures-bot`) מקבלים קובץ
    נפרד `.<service>_pk` כך ששני המאגרים לעולם לא מתנגשים."""
    if service == SERVICE:
        return ".polymarket_pk"
    # נורמליזציה לשם קובץ בטוח (binance-futures-bot -> .binance-futures-bot_pk)
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in service)
    return f".{safe}_pk"


def _file_store_path(service: str = SERVICE) -> Path:
    """נתיב לקובץ ה-fallback. עדיפות: DATA_ROOT (Volume של Railway), אחרת ליד המודול."""
    name = _file_name_for(service)
    root = os.environ.get("DATA_ROOT")
    if root:
        return Path(root) / name
    return Path(__file__).resolve().parent / name


def _file_load(service: str = SERVICE) -> Optional[str]:
    """קורא מהקובץ. None אם לא קיים / ריק."""
    p = _file_store_path(service)
    try:
        if not p.is_file():
            return None
        content = p.read_text(encoding="utf-8").strip()
        return content or None
    except OSError as e:
        _log.warning("קריאת קובץ מפתח נכשלה: %s", e)
        return None


def _file_save(key: str, service: str = SERVICE) -> bool:
    """שומר לקובץ עם chmod 600 (owner-only). True בהצלחה."""
    p = _file_store_path(service)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # כתיבה אטומית: tmp ואז rename
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(key, encoding="utf-8")
        try:
            tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
        except OSError:
            # מערכות קבצים שלא תומכות בהרשאות (Windows) — נמשיך בלי
            pass
        os.replace(str(tmp), str(p))
        return True
    except OSError as e:
        _log.warning("שמירת קובץ מפתח נכשלה: %s", e)
        return False


def _file_delete(service: str = SERVICE) -> bool:
    """מוחק את הקובץ. True אם היה ונמחק."""
    p = _file_store_path(service)
    try:
        if not p.is_file():
            return False
        p.unlink()
        return True
    except OSError as e:
        _log.warning("מחיקת קובץ מפתח נכשלה: %s", e)
        return False


def _file_exists(service: str = SERVICE) -> bool:
    return _file_store_path(service).is_file()


def _get_keyring():
    """מנסה לטעון keyring; מחזיר module או None בלי להרעיש בלוגים.
    אם כבר נכשל פעם — לא מנסה שוב (מונע לוגים חוזרים ועבודה מיותרת ב-Railway/Linux)."""
    global _warned_unavailable, _keyring_broken
    if _keyring_broken:
        return None
    try:
        import keyring  # type: ignore
    except Exception as e:
        _keyring_broken = True
        if not _warned_unavailable:
            _warned_unavailable = True
            _log.info("keyring לא זמין (%s); fallback לקובץ", e)
        return None
    return keyring


def load_key(service: str = SERVICE) -> Optional[str]:
    """מחזיר את המפתח השמור או None.

    `service` מאפשר scope נפרד למאגרי מפתחות (ברירת מחדל: polymarket-bot).
    שירות אחר (למשל `binance-futures-bot`) ניגש ל-keyring service נפרד ולקובץ
    fallback נפרד, כך ששני המאגרים לעולם לא מתנגשים.

    סדר חיפוש:
    1. Keychain/Secret Service (אם זמין).
    2. קובץ ב-DATA_ROOT/.<service>_pk (Volume של Railway).
    """
    global _keyring_broken
    kr = _get_keyring()
    if kr is not None:
        try:
            v = kr.get_password(service, USERNAME)
            if v:
                v = v.strip()
                if v:
                    return v
        except Exception as e:
            name = type(e).__name__
            # NoKeyringError / backend errors — stop trying (Railway/Linux without desktop)
            if "NoKeyring" in name or "recommended" in str(e).lower():
                _keyring_broken = True
                _log.info("keyring backend חסר — fallback לקובץ: %s", e)
            else:
                _log.warning("קריאת keyring נכשלה — מנסה fallback: %s", e)
    # Fallback לקובץ
    return _file_load(service)


def save_key(key: str, service: str = SERVICE) -> bool:
    """שומר/מחליף מפתח. True בהצלחה.

    מנסה Keychain קודם; אם נכשל (Railway/container) — fallback לקובץ ב-DATA_ROOT.
    `service` מאפשר scope נפרד (ברירת מחדל: polymarket-bot).
    """
    k = (key or "").strip()
    if not k:
        return False
    kr = _get_keyring()
    if kr is not None:
        try:
            kr.set_password(service, USERNAME, k)
            return True
        except Exception as e:
            _log.info("keyring write נכשל — fallback לקובץ: %s", e)
    # Fallback לקובץ
    return _file_save(k, service)


def delete_key(service: str = SERVICE) -> bool:
    """מוחק מפתח שמור (משני המקומות). True אם היה מה למחוק."""
    deleted_any = False
    kr = _get_keyring()
    if kr is not None:
        try:
            kr.delete_password(service, USERNAME)
            deleted_any = True
        except Exception as e:
            name = type(e).__name__
            # PasswordDeleteError = לא היה מה למחוק; שאר השגיאות שקטות
            if name not in ("PasswordDeleteError", "NoKeyringError"):
                _log.warning("מחיקה מ-keyring נכשלה: %s", e)
    # גם מוחק מהקובץ אם קיים
    if _file_delete(service):
        deleted_any = True
    return deleted_any


def has_persisted_key(service: str = SERVICE) -> bool:
    """האם יש מפתח שמור (ב-Keychain או בקובץ)."""
    return load_key(service) is not None
