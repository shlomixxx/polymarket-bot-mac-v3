"""כתיבה אטומית של קבצי state/config (tmp + os.replace).

מטרה: למנוע השחתת JSON אם התהליך קורס באמצע כתיבה. שדרוג מ-`write_text` ישיר
שעלול להשאיר קובץ חתוך/ריק.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """כתיבה אטומית: tmp באותו ספרייה ואז os.replace.

    os.replace הוא atomic על POSIX וגם על Windows; מבטיח שקורא בו זמני יראה
    או את התוכן הישן או את התוכן החדש — לעולם לא חצי קובץ.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=ensure_ascii))
