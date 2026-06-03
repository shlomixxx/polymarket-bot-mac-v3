"""עוזר ETag / 304 Not Modified ל-endpoints תצוגתיים (תשתית 0.3 ב-API_RESOURCE_TASKS.md).

שימוש ב-handler של FastAPI:

    from fastapi import Request
    from _http_cache import etag_json_response

    @app.get("/api/demo/snapshot")
    async def demo_snapshot(request: Request):
        payload = {...}
        return etag_json_response(payload, request.headers.get("if-none-match"))

כש-If-None-Match תואם ל-ETag הנוכחי -> מוחזר 304 עם גוף ריק (חוסך את העברת הגוף).
ה-ETag מחושב תמיד מה-payload הנוכחי, כך שכל שינוי אמיתי שובר אותו מיד — אין הקפאה בטיימר.

Guardrail: עוטפים בזה אך ורק קריאות GET תצוגתיות. אסור להחזיר 304 על נתיב שמזין החלטת מסחר.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from starlette.responses import Response


def _serialize(payload: Any) -> bytes:
    """סריאליזציה דטרמיניסטית (sort_keys) כדי שאותו תוכן ייתן אותו ETag ללא תלות בסדר המפתחות."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    ).encode("utf-8")


def make_etag(body: bytes) -> str:
    """ETag חלש מבוסס-תוכן, עטוף במרכאות לפי תקן HTTP."""
    return '"' + hashlib.sha1(body).hexdigest()[:20] + '"'


def etag_json_response(payload: Any, if_none_match: Optional[str]) -> Response:
    """בונה תגובת JSON עם כותרת ETag, או 304 ריק כש-If-None-Match תואם."""
    body = _serialize(payload)
    etag = make_etag(body)
    if if_none_match is not None and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=body, media_type="application/json", headers={"ETag": etag})
