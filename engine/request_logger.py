"""Request logger middleware + client log sink.

Writes every HTTP request (server-side) and frontend ``api()`` call (client-side)
to ``engine/logs/requests.jsonl`` as JSONL, so a 1-2 hour run can be analyzed
offline to find duplicate/unnecessary API calls.

Disable with environment variable ``LOG_REQUESTS=0``.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Request


_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_PATH = _LOG_DIR / "requests.jsonl"
_ROTATE_BYTES = 50 * 1024 * 1024

_ENABLED = os.environ.get("LOG_REQUESTS", "1").strip() not in ("0", "false", "False", "")

_SKIP_PATH_PREFIXES = ("/api/_log/",)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _maybe_rotate() -> None:
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _ROTATE_BYTES:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            _LOG_PATH.rename(_LOG_DIR / f"requests.jsonl.{ts}")
    except OSError:
        pass


def _write_line(payload: dict[str, Any]) -> None:
    if not _ENABLED:
        return
    try:
        _ensure_log_dir()
        _maybe_rotate()
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, separators=(",", ":"), default=str))
            f.write("\n")
    except Exception:
        pass


def log_event(**fields: Any) -> None:
    """Append an arbitrary structured line. Safe to call from anywhere."""
    payload = {"ts": _now_iso(), **fields}
    _write_line(payload)


def is_enabled() -> bool:
    return _ENABLED


def make_request_id() -> str:
    return _short_id()


def init_request_logger(app: FastAPI) -> None:
    """Wire the HTTP middleware and the ``/api/_log/client-request`` sink."""
    if _ENABLED:

        @app.middleware("http")
        async def _request_log_mw(request: Request, call_next):
            path = request.url.path
            if any(path.startswith(p) for p in _SKIP_PATH_PREFIXES):
                return await call_next(request)

            rid = _short_id()
            start = time.perf_counter()
            request.state.request_id = rid

            response = None
            status = 0
            try:
                response = await call_next(request)
                status = response.status_code
            except Exception:
                status = 500
                raise
            finally:
                duration_ms = round((time.perf_counter() - start) * 1000.0, 3)
                client_ip = request.client.host if request.client else None
                ua = request.headers.get("user-agent", "") or ""
                log_event(
                    source="server",
                    request_id=rid,
                    method=request.method,
                    path=path,
                    query=str(request.url.query or ""),
                    status=status,
                    duration_ms=duration_ms,
                    client_ip=client_ip,
                    user_agent=ua[:200] if ua else None,
                )
                if response is not None:
                    try:
                        response.headers["X-Request-Id"] = rid
                    except Exception:
                        pass
            return response

    @app.post("/api/_log/client-request", include_in_schema=False)
    async def _client_log(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not _ENABLED:
            return {"ok": True, "written": 0}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return {"ok": False, "written": 0}
        written = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            row = dict(e)
            row["source"] = "client"
            if not row.get("ts"):
                row["ts"] = _now_iso()
            _write_line(row)
            written += 1
        return {"ok": True, "written": written}

    log_event(kind="logger_init", source="server", enabled=_ENABLED)
