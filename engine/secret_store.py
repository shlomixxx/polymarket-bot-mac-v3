"""
אחסון קבוע למפתח ה-Polymarket דרך מערכת האבטחה של מערכת ההפעלה.

במק: macOS Keychain (system service)
בלינוקס: Secret Service (gnome-keyring וכדו׳)
בווינדוס: Windows Credential Manager

אם הספרייה `keyring` לא מותקנת או שאין backend זמין — כל הפונקציות
מחזירות False/None בלי להפיל את המנוע. המפתח עצמו אף פעם לא עובר ללוגים.
"""
from __future__ import annotations

import logging
from typing import Optional

SERVICE = "polymarket-bot"
USERNAME = "default"

_log = logging.getLogger(__name__)
_warned_unavailable = False


def _get_keyring():
    """מנסה לטעון keyring; מחזיר module או None בלי להרעיש בלוגים."""
    global _warned_unavailable
    try:
        import keyring  # type: ignore
    except Exception as e:
        if not _warned_unavailable:
            _warned_unavailable = True
            _log.info("keyring לא זמין (%s); שמירת מפתח קבועה מכובה", e)
        return None
    return keyring


def load_key() -> Optional[str]:
    """מחזיר את המפתח השמור או None."""
    kr = _get_keyring()
    if kr is None:
        return None
    try:
        v = kr.get_password(SERVICE, USERNAME)
    except Exception as e:
        _log.warning("קריאת keyring נכשלה: %s", e)
        return None
    if v is None:
        return None
    v = v.strip()
    return v or None


def save_key(key: str) -> bool:
    """שומר/מחליף מפתח. True בהצלחה."""
    kr = _get_keyring()
    if kr is None:
        return False
    k = (key or "").strip()
    if not k:
        return False
    try:
        kr.set_password(SERVICE, USERNAME, k)
    except Exception as e:
        _log.warning("כתיבה ל-keyring נכשלה: %s", e)
        return False
    return True


def delete_key() -> bool:
    """מוחק מפתח שמור. True אם היה מה למחוק ונמחק בהצלחה."""
    kr = _get_keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, USERNAME)
    except Exception as e:
        # PasswordDeleteError נזרקת כשאין מה למחוק — נחזיר False בלי להטריד
        name = type(e).__name__
        if name not in ("PasswordDeleteError", "NoKeyringError"):
            _log.warning("מחיקה מ-keyring נכשלה: %s", e)
        return False
    return True


def has_persisted_key() -> bool:
    """האם יש מפתח שמור ב-keyring."""
    return load_key() is not None
