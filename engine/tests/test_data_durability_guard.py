"""טסטים ל-_data_root_is_ephemeral — שומר-הסף לעמידות נתונים באתחול.

מוודא שאין אזעקת שווא ב-local/dev (אין RAILWAY env), ושבדפלוי Railway ה-helper
מזהה נכון אם DATA_ROOT הוא mountpoint אמיתי (Volume) או fallback ארעי שיימחק באתחול.
"""
from __future__ import annotations

import os
from pathlib import Path

import main as engine_main


def test_not_railway_is_not_ephemeral():
    # Local/dev: empty env -> no false alarm, even for a code-dir path that isn't a mount.
    code_dir = Path(__file__).resolve().parent
    assert engine_main._data_root_is_ephemeral(code_dir, {}) is False


def test_not_railway_ignores_mount_state(monkeypatch):
    # Even if ismount would say False, a non-Railway env must never alarm.
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    assert engine_main._data_root_is_ephemeral(Path("/app/engine"), {}) is False


def test_railway_not_mounted_is_ephemeral(monkeypatch):
    # Railway deploy + DATA_ROOT is NOT a mountpoint (ephemeral fallback) -> True.
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    env = {"RAILWAY_GIT_COMMIT_SHA": "abc123"}
    assert engine_main._data_root_is_ephemeral(Path("/app/engine"), env) is True


def test_railway_mounted_is_durable(monkeypatch):
    # Railway deploy + DATA_ROOT IS a mountpoint (real Volume) -> False.
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    env = {"RAILWAY_GIT_COMMIT_SHA": "abc123"}
    assert engine_main._data_root_is_ephemeral(Path("/data"), env) is False


def test_railway_detected_by_any_env_var(monkeypatch):
    # Any of the Railway markers should flip us into the deploy branch.
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_GIT_COMMIT_SHA"):
        assert engine_main._data_root_is_ephemeral(Path("/app/engine"), {key: "v"}) is True


def test_ismount_exception_never_false_positives(monkeypatch):
    # If the mount check blows up on an unexpected platform, never alarm.
    def _boom(p):
        raise OSError("no such platform")

    monkeypatch.setattr(os.path, "ismount", _boom)
    env = {"RAILWAY_GIT_COMMIT_SHA": "abc123"}
    assert engine_main._data_root_is_ephemeral(Path("/app/engine"), env) is False
