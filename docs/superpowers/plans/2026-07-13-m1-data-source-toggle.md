# M1 — Binance data-source toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persisted `data_source` selector (`polymarket` | `binance`) and a header button that switches the entire BTC data pipeline (current price, price-to-beat, and the source labels/stats) between Polymarket's Chainlink stream and Binance — so all demo/stats read Binance when selected.

**Architecture:** A tiny process-global module (`engine/data_source.py`) holds the active source and is kept in sync with the persisted config on load and on every config change. The existing price functions consult it and branch. In `binance` mode the pipeline is fully Binance-consistent (current price, price-to-beat, and the already-Binance demo settlement all agree). No order-routing changes — that is M2+, out of scope here.

**Tech Stack:** Python 3 / FastAPI / Pydantic (engine), React + TypeScript + Vite (UI), pytest + pytest-asyncio + unittest.mock (engine tests), vitest (UI logic tests).

## Global Constraints

- Default `data_source` value is `"polymarket"` (no behavior change unless the user flips it) — verbatim default.
- Allowed values are exactly `"polymarket"` and `"binance"`; anything else is rejected (API) or clamped to `"polymarket"` (module).
- Follow the existing persisted-config pattern: a new field is added in FIVE places — `StrategyConfig` dataclass, `ConfigBody`, POST `/api/strategy/config` validation, `_save_persisted_config` dict, and GET `/api/strategy/config` dict.
- Engine tests live in `engine/tests/test_*.py`; `engine/tests/conftest.py` puts `engine/` on `sys.path`, so tests import bare modules (`from btc_price import ...`). Run with `python -m pytest engine/tests/<file>::<test> -v` from the repo root.
- Do NOT touch order placement (`engine/live_clob.py`, `engine/strategy_runner.py` order calls). Data-source only.
- All new user-facing UI strings are Hebrew, matching the existing app.

---

### Task 1: `engine/data_source.py` — process-global active source (pure module)

**Files:**
- Create: `engine/data_source.py`
- Test: `engine/tests/test_data_source.py`

**Interfaces:**
- Produces:
  - `VALID_DATA_SOURCES: tuple[str, str]` = `("polymarket", "binance")`
  - `get_active() -> str` — returns the current active source; defaults to `"polymarket"`.
  - `set_active(value: str) -> str` — sets and returns the normalized source; any invalid/None value normalizes to `"polymarket"`.
  - `normalize(value) -> str` — pure helper; returns `value` if in `VALID_DATA_SOURCES` else `"polymarket"`.

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/test_data_source.py
"""בדיקות למודול מקור-הנתונים הפעיל (Polymarket ⟷ Binance)."""
from __future__ import annotations

import data_source


def test_default_is_polymarket():
    # מצב טרי: ברירת מחדל חייבת להיות polymarket (בלי שינוי התנהגות).
    data_source.set_active("polymarket")
    assert data_source.get_active() == "polymarket"


def test_set_active_binance_roundtrips():
    assert data_source.set_active("binance") == "binance"
    assert data_source.get_active() == "binance"
    data_source.set_active("polymarket")  # cleanup


def test_invalid_normalizes_to_polymarket():
    assert data_source.set_active("nasdaq") == "polymarket"
    assert data_source.get_active() == "polymarket"
    assert data_source.normalize(None) == "polymarket"
    assert data_source.normalize("binance") == "binance"


def test_valid_sources_constant():
    assert data_source.VALID_DATA_SOURCES == ("polymarket", "binance")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest engine/tests/test_data_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data_source'`.

- [ ] **Step 3: Write minimal implementation**

```python
# engine/data_source.py
"""מקור-הנתונים הפעיל למחירי BTC: "polymarket" (Chainlink stream) או "binance".

מצב תהליכי יחיד ("source of truth") שנשמר מסונכרן עם הקונפיג הנשמר: main.py קורא
set_active() בטעינת הקונפיג ובכל עדכון קונפיג, וצרכני-מחיר (btc_price, main) קוראים
get_active(). מודול טהור — בלי תלות ב-runner כדי למנוע import מעגלי.
"""
from __future__ import annotations

VALID_DATA_SOURCES: tuple[str, str] = ("polymarket", "binance")
_DEFAULT = "polymarket"

_active: str = _DEFAULT


def normalize(value) -> str:
    """מחזיר את value אם הוא מקור חוקי, אחרת polymarket (ברירת מחדל בטוחה)."""
    return value if value in VALID_DATA_SOURCES else _DEFAULT


def get_active() -> str:
    return _active


def set_active(value: str) -> str:
    global _active
    _active = normalize(value)
    return _active
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest engine/tests/test_data_source.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add engine/data_source.py engine/tests/test_data_source.py
git commit -m "feat(data-source): add process-global active data-source module"
```

---

### Task 2: Persist the `data_source` config field end-to-end (dataclass + ConfigBody + validation + persist + GET) and sync the module

**Files:**
- Modify: `engine/strategy_runner.py:106` (add field to `StrategyConfig`, next to `order_mode`)
- Modify: `engine/main.py` — `ConfigBody` (~1567), POST validation (~1614, after the `order_mode` check), `_load_persisted_config` (~366-375), `_save_persisted_config` dict (~434), GET config dict (~1716)
- Test: `engine/tests/test_data_source_config.py`

**Interfaces:**
- Consumes: `data_source.set_active`, `data_source.get_active` (Task 1).
- Produces: `StrategyConfig.data_source: str` (default `"polymarket"`); config JSON key `"data_source"`; module stays in sync with `runner.rt.config.data_source` after load and after any config POST.

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/test_data_source_config.py
"""מקור-הנתונים נשמר בקונפיג, מסונכרן למודול, ומאומת ב-API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
import data_source


@pytest.fixture()
def client():
    return TestClient(main.app)


def _full_config_body(**overrides):
    # גוף קונפיג מלא-מספיק: מתחילים מברירות-המחדל של הקונפיג הנוכחי ומעדכנים.
    body = main.ConfigBody().model_dump()
    body.update(overrides)
    return body


def test_default_data_source_is_polymarket(client):
    r = client.get("/api/strategy/config")
    assert r.status_code == 200
    assert r.json()["data_source"] == "polymarket"


def test_post_binance_persists_and_syncs_module(client):
    r = client.post("/api/strategy/config", json=_full_config_body(data_source="binance"))
    assert r.status_code == 200
    assert main.runner.rt.config.data_source == "binance"
    assert data_source.get_active() == "binance"          # module synced
    assert client.get("/api/strategy/config").json()["data_source"] == "binance"
    # cleanup
    client.post("/api/strategy/config", json=_full_config_body(data_source="polymarket"))


def test_post_invalid_data_source_rejected(client):
    r = client.post("/api/strategy/config", json=_full_config_body(data_source="kraken"))
    assert r.status_code == 400
```

> Note: if `ConfigBody()` requires no args (all fields have defaults), `_full_config_body` works as-is. If the smoke suite already has a config-body helper (see `engine/tests/test_api_smoke.py`), reuse it instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest engine/tests/test_data_source_config.py -v`
Expected: FAIL — `KeyError: 'data_source'` (GET) / `data_source` not on config.

- [ ] **Step 3a: Add the field to `StrategyConfig`** (`engine/strategy_runner.py`, immediately after line 106 `order_mode`):

```python
    order_mode: Literal["limit", "market"] = "limit"
    # מקור נתוני BTC: "polymarket" (Chainlink stream) או "binance". שולט מאיפה נקראים
    # מחיר נוכחי, "מחיר לנצח", והסטטיסטיקה/דמו. אינו משנה לאן נשלחות פקודות (זה M2).
    data_source: Literal["polymarket", "binance"] = "polymarket"
```

- [ ] **Step 3b: Add to `ConfigBody`** (`engine/main.py`, right after the `order_mode: str = "limit"` line):

```python
    order_mode: str = "limit"
    data_source: str = "polymarket"  # polymarket | binance — מקור נתוני BTC
```

- [ ] **Step 3c: Validate in POST handler** (`engine/main.py`, right after the `order_mode` check at line 1613-1614):

```python
    if body.order_mode not in ("limit", "market"):
        raise HTTPException(400, "order_mode must be 'limit' or 'market'")
    if body.data_source not in ("polymarket", "binance"):
        raise HTTPException(400, "data_source must be 'polymarket' or 'binance'")
```

- [ ] **Step 3d: Sync the module after the setattr loop** (`engine/main.py`, in `strategy_config`, immediately after line 1652 `setattr(c, k, v)` loop closes / before `c.side_preference` line 1653):

```python
    c.side_preference = body.side_preference  # type: ignore
    import data_source as _data_source
    _data_source.set_active(runner.rt.config.data_source)
```

- [ ] **Step 3e: Restore + sync on load** (`engine/main.py`, at the end of `_load_persisted_config`, after the `live_trading` block at line 381):

```python
        if "live_trading" in data:
            try:
                runner.rt.live_trading = bool(data.get("live_trading"))
            except Exception:
                runner.rt.live_trading = False
        import data_source as _data_source
        _data_source.set_active(getattr(runner.rt.config, "data_source", "polymarket"))
```

- [ ] **Step 3f: Persist** (`engine/main.py`, in `_save_persisted_config` dict, right after line 434 `"order_mode": ...`):

```python
            "order_mode": getattr(c, "order_mode", "limit"),
            "data_source": str(getattr(c, "data_source", "polymarket")),
```

- [ ] **Step 3g: Return in GET** (`engine/main.py`, in `get_strategy_config`, right after line 1716 `"order_mode": ...`):

```python
        "order_mode": getattr(c, "order_mode", "limit"),
        "data_source": str(getattr(c, "data_source", "polymarket")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest engine/tests/test_data_source_config.py -v`
Expected: PASS (3 passed). Also run the smoke suite to catch regressions: `python -m pytest engine/tests/test_api_smoke.py -v`.

- [ ] **Step 5: Commit**

```bash
git add engine/strategy_runner.py engine/main.py engine/tests/test_data_source_config.py
git commit -m "feat(data-source): persist data_source config field + sync module on load/update"
```

---

### Task 3: Dedicated header endpoint `POST/GET /api/data-source` (the button target)

**Files:**
- Modify: `engine/main.py` — add a `DataSourceBody` model + two routes, modeled on `ModeBody` / `strategy_mode` (lines 1758-1795)
- Test: `engine/tests/test_data_source_endpoint.py`

**Interfaces:**
- Consumes: `data_source` module (Task 1), `_save_persisted_config` (Task 2).
- Produces: `POST /api/data-source {data_source}` → `{"ok": True, "data_source": <normalized>}`; `GET /api/data-source` → `{"data_source": <active>}`. POST updates `runner.rt.config.data_source`, calls `data_source.set_active(...)`, and persists.

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/test_data_source_endpoint.py
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
import data_source


@pytest.fixture()
def client():
    return TestClient(main.app)


def test_get_returns_active(client):
    data_source.set_active("polymarket")
    main.runner.rt.config.data_source = "polymarket"
    assert client.get("/api/data-source").json() == {"data_source": "polymarket"}


def test_post_switches_to_binance(client):
    r = client.post("/api/data-source", json={"data_source": "binance"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "data_source": "binance"}
    assert main.runner.rt.config.data_source == "binance"
    assert data_source.get_active() == "binance"
    client.post("/api/data-source", json={"data_source": "polymarket"})  # cleanup


def test_post_invalid_rejected(client):
    assert client.post("/api/data-source", json={"data_source": "x"}).status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest engine/tests/test_data_source_endpoint.py -v`
Expected: FAIL — 404 on `/api/data-source`.

- [ ] **Step 3: Add the model + routes** (`engine/main.py`, immediately after the `strategy_mode` handler block ends around line 1795; place near the other config routes):

```python
class DataSourceBody(BaseModel):
    data_source: str  # polymarket | binance


@app.get("/api/data-source")
async def get_data_source():
    return {"data_source": getattr(runner.rt.config, "data_source", "polymarket")}


@app.post("/api/data-source")
async def set_data_source(body: DataSourceBody):
    import data_source as _data_source
    if body.data_source not in _data_source.VALID_DATA_SOURCES:
        raise HTTPException(400, "data_source must be 'polymarket' or 'binance'")
    runner.rt.config.data_source = body.data_source  # type: ignore
    _data_source.set_active(body.data_source)
    _save_persisted_config()
    return {"ok": True, "data_source": _data_source.get_active()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest engine/tests/test_data_source_endpoint.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add engine/main.py engine/tests/test_data_source_endpoint.py
git commit -m "feat(data-source): add GET/POST /api/data-source header endpoint"
```

---

### Task 4: Route current BTC price by active source (`btc_price.fetch_btc_current_usd`)

**Files:**
- Modify: `engine/btc_price.py:238-254` (`fetch_btc_current_usd`)
- Test: `engine/tests/test_btc_price_data_source.py`

**Interfaces:**
- Consumes: `data_source.get_active()` (Task 1), existing `fetch_btc_spot_usdt`.
- Produces: `fetch_btc_current_usd()` returns `(price, "binance")` when active source is `binance` (Chainlink stream is NOT consulted); unchanged `(price, "chainlink_stream" | "binance_fallback")` when `polymarket`.

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/test_btc_price_data_source.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import btc_price
import data_source


@pytest.mark.asyncio
async def test_binance_mode_uses_binance_and_labels_binance():
    data_source.set_active("binance")
    try:
        with patch("btc_price.fetch_btc_spot_usdt", AsyncMock(return_value=101_000.0)) as spot:
            price, source = await btc_price.fetch_btc_current_usd()
        assert (price, source) == (101_000.0, "binance")
        spot.assert_awaited_once()
    finally:
        data_source.set_active("polymarket")


@pytest.mark.asyncio
async def test_polymarket_mode_prefers_chainlink_stream():
    data_source.set_active("polymarket")
    fake_stream = type("S", (), {"get_current_price": staticmethod(lambda: {"value": 99_000.0})})()
    with patch.dict("sys.modules", {"chainlink_price_stream": type("M", (), {"chainlink_stream": fake_stream})}):
        price, source = await btc_price.fetch_btc_current_usd()
    assert (price, source) == (99_000.0, "chainlink_stream")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest engine/tests/test_btc_price_data_source.py -v`
Expected: FAIL — `test_binance_mode...` fails because current code tries Chainlink first and labels `binance_fallback`, not `binance`.

- [ ] **Step 3: Modify `fetch_btc_current_usd`** (`engine/btc_price.py:238-254`) — add the binance-mode short-circuit at the top:

```python
async def fetch_btc_current_usd() -> tuple[float, str]:
    """מחיר BTC נוכחי לפי מקור-הנתונים הפעיל.

    מצב "binance": קורא ישירות Binance spot ומחזיר source="binance".
    מצב "polymarket": מעדיף את פיד Chainlink Data Stream של Polymarket (המקור שלפיו
    השוק נסגר); נופל ל-Binance spot רק אם הפיד לא טרי/לא זמין (source="binance_fallback").
    """
    import data_source
    if data_source.get_active() == "binance":
        price = await fetch_btc_spot_usdt()
        return price, "binance"
    try:
        from chainlink_price_stream import chainlink_stream

        cur = chainlink_stream.get_current_price()
        if cur is not None:
            return float(cur["value"]), "chainlink_stream"
    except Exception:
        pass
    price = await fetch_btc_spot_usdt()
    return price, "binance_fallback"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest engine/tests/test_btc_price_data_source.py engine/tests/test_btc_window_prices.py -v`
Expected: PASS (new tests pass; existing btc-window tests still pass).

- [ ] **Step 5: Commit**

```bash
git add engine/btc_price.py engine/tests/test_btc_price_data_source.py
git commit -m "feat(data-source): route current BTC price to Binance when data_source=binance"
```

---

### Task 5: Route price-to-beat by active source (`/api/market/current`) and tag demo trades

**Files:**
- Modify: `engine/main.py:1177-1205` (the price-to-beat resolution block in `market_current`)
- Modify: `engine/demo_engine.py` (settlement record — add `data_source` tag to the completed-trade dict; region ~1310-1329)
- Test: `engine/tests/test_market_ptb_data_source.py`

**Interfaces:**
- Consumes: `data_source.get_active()`, `fetch_open_price_at_window_start`, `chainlink_stream.get_price_to_beat`.
- Produces: in `binance` mode `market_current` returns `price_to_beat_source == "binance_1m"` (Chainlink block skipped); each recorded demo trade carries `data_source`.

- [ ] **Step 1: Write the failing test** (extract the PTB decision into a pure, testable helper first):

```python
# engine/tests/test_market_ptb_data_source.py
from __future__ import annotations

import main


def test_binance_mode_prefers_binance_ptb():
    # helper טהור: בהינתן מקור="binance" + open של Binance, בוחר binance_1m ולא chainlink.
    val, src = main._resolve_ptb_for_source(
        active="binance", binance_open=100_000.0, chainlink_ptb=99_950.0,
    )
    assert (val, src) == (100_000.0, "binance_1m")


def test_polymarket_mode_prefers_chainlink_ptb():
    val, src = main._resolve_ptb_for_source(
        active="polymarket", binance_open=100_000.0, chainlink_ptb=99_950.0,
    )
    assert (val, src) == (99_950.0, "chainlink_stream")


def test_polymarket_mode_falls_back_to_binance_when_no_chainlink():
    val, src = main._resolve_ptb_for_source(
        active="polymarket", binance_open=100_000.0, chainlink_ptb=None,
    )
    assert (val, src) == (100_000.0, "binance_1m_fallback")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest engine/tests/test_market_ptb_data_source.py -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute '_resolve_ptb_for_source'`.

- [ ] **Step 3a: Add the pure helper** (`engine/main.py`, near `_price_to_beat_note`, above `market_current`):

```python
def _resolve_ptb_for_source(
    *, active: str, binance_open: float | None, chainlink_ptb: float | None
) -> tuple[float | None, str]:
    """בוחר את "מחיר לנצח" והמקור שלו לפי מקור-הנתונים הפעיל (טהור, נבדק ביחידה)."""
    if active == "binance":
        if binance_open is not None:
            return binance_open, "binance_1m"
        return None, "pending"
    # polymarket: Chainlink stream מדויק קודם, אחרת Binance 1m כ-fallback
    if chainlink_ptb is not None:
        return chainlink_ptb, "chainlink_stream"
    if binance_open is not None:
        return binance_open, "binance_1m_fallback"
    return None, "pending"
```

- [ ] **Step 3b: Use it in `market_current`** — replace the Chainlink-first block at `engine/main.py:1177-1186` with a source-aware resolution. In `binance` mode fetch the Binance open and skip Chainlink:

```python
    # מקור "מחיר לנצח" לפי מקור-הנתונים הפעיל.
    import data_source as _data_source
    _active = _data_source.get_active()
    if _active == "binance":
        try:
            b_open = await asyncio.wait_for(fetch_open_price_at_window_start(m.epoch), timeout=3.0)
        except Exception:
            b_open = None
        cached_open, cached_ptb_source = _resolve_ptb_for_source(
            active="binance", binance_open=b_open, chainlink_ptb=None,
        )
    elif cached_ptb_source != "chainlink_stream":
        try:
            ptb_cl = chainlink_stream.get_price_to_beat(m.epoch)
            if ptb_cl is not None:
                cached_open = ptb_cl
                cached_ptb_source = "chainlink_stream"
        except Exception:
            pass
```

> The existing background `_populate_price_to_beat_fallback` task (lines 1144-1175) only fills fallbacks when the source is not already Chainlink; in `binance` mode we set `binance_1m` synchronously above, so leave that task as-is (it will not override a `binance_1m` value — extend its guard to also skip when `cached_ptb_source == "binance_1m"` if you observe it clobbering).

- [ ] **Step 3c: Tag demo trades** (`engine/demo_engine.py`, where the completed/settled trade dict is built ~1310-1329): add the active source to the record:

```python
    import data_source
    trade["data_source"] = data_source.get_active()
```

(Place it where `trade` / the settlement record dict is assembled, alongside `resolved_outcome` / `settlement_won`. Match the actual local variable name in that block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest engine/tests/test_market_ptb_data_source.py engine/tests/test_demo_engine.py -v`
Expected: PASS (helper tests pass; demo-engine suite still green).

- [ ] **Step 5: Commit**

```bash
git add engine/main.py engine/demo_engine.py engine/tests/test_market_ptb_data_source.py
git commit -m "feat(data-source): route price-to-beat by source + tag demo trades with data_source"
```

---

### Task 6: UI — header toggle button (Polymarket ⟷ Binance) + hydration + source badge

**Files:**
- Modify: `src/App.tsx` — state (~near 1679), hydration (in `refresh()` guard ~2062 / read-only block ~2033), header JSX (~2567), and a small badge near the stats/dash headers
- Test: manual (repo has no React DOM tests; vitest is `node` env, `*.test.ts` only — see plan note)

**Interfaces:**
- Consumes: `GET /api/strategy/config` (`data_source` field, Task 2), `POST /api/data-source` (Task 3), the `api()` helper (`src/api.ts`).
- Produces: a header button that flips `dataSource` state optimistically and persists via `POST /api/data-source`; a visible badge showing the active source.

- [ ] **Step 1: Add state** (`src/App.tsx`, next to the `liveMode` state ~1679):

```tsx
  const [dataSource, setDataSource] = useState<"polymarket" | "binance">("polymarket");
```

- [ ] **Step 2: Hydrate from config** (`src/App.tsx`, inside the read-only/live block that always applies, e.g. right after the mode hydration ~2039 — data_source is not an "edited form field", so apply it every refresh, not inside the dirty-guard):

```tsx
        if (c.data_source === "polymarket" || c.data_source === "binance") setDataSource(c.data_source);
```

> `c` here is the fetched config object. If the read-only block uses a differently-named binding, mirror the existing mode-hydration line in that same block.

- [ ] **Step 3: Add the header button** (`src/App.tsx`, inside the header flex `<div>` at line 2567, as a sibling before the live-trade `<Button>`):

```tsx
          <button
            type="button"
            className="header-mode-btn"
            data-venue={dataSource}
            title="מקור נתוני BTC — לוח, סטטיסטיקה ודמו"
            onClick={async () => {
              const next = dataSource === "polymarket" ? "binance" : "polymarket";
              setDataSource(next); // אופטימי
              try {
                await api("/api/data-source", { method: "POST", body: JSON.stringify({ data_source: next }) });
                void refresh();
              } catch (e) {
                setDataSource(dataSource); // rollback
                alert(e instanceof Error ? e.message : "כשל בעדכון מקור נתונים");
              }
            }}
          >
            נתונים: {dataSource === "binance" ? "₿ Binance" : "🟣 Polymarket"}
          </button>
```

- [ ] **Step 4: Add a source badge** near the stats/dash section headers so a Binance-based number is never mistaken for Polymarket — e.g. reuse the existing `badge-mode` span pattern (line 2568). Insert wherever the demo/stats header renders:

```tsx
          <span className="badge-mode" title="מקור הנתונים של המספרים בעמוד">
            מקור: {dataSource === "binance" ? "₿ Binance" : "🟣 Polymarket"}
          </span>
```

- [ ] **Step 5: Build + manual verification**

Run: `npm run build`
Expected: `tsc --noEmit && vite build` passes (no type errors).

Manual check (engine running, `npm run dev`):
1. Header shows "נתונים: 🟣 Polymarket" by default.
2. Click it → shows "₿ Binance"; `curl localhost:8767/api/data-source` returns `{"data_source":"binance"}`.
3. Reload the page → button still shows Binance (persisted + hydrated).
4. `/api/btc/live` `source` field becomes `"binance"`; `/api/market/current` `price_to_beat_source` becomes `"binance_1m"`.
5. Flip back to Polymarket → source labels return to `chainlink_stream`.

- [ ] **Step 6: Commit**

```bash
git add src/App.tsx
git commit -m "feat(data-source): header Polymarket/Binance toggle + source badge"
```

---

## Self-Review notes
- **Spec coverage:** M1 items from the spec — data_source field (Task 2), engine wiring for current price (Task 4) + price-to-beat (Task 5), settlement/stats consistency in Binance mode (Task 5 tag + the pipeline now reads one source), header toggle + badge (Task 6). The dedicated button endpoint (Task 3) implements the "button" UX. Order-routing, Venue seam, Predict.fun, real-money gates = M2-M4, intentionally out of scope.
- **Type consistency:** `data_source` string used identically across dataclass/ConfigBody/JSON/UI; source labels `"binance"`, `"binance_1m"`, `"chainlink_stream"` used consistently between Tasks 4/5 and the Task 6 manual checks.
- **Known residual (documented, not a bug):** in `polymarket` mode the demo still settles on the Binance 1m proxy (pre-existing behavior); full Chainlink-historical settlement for polymarket-mode is deferred. In `binance` mode the whole pipeline is Binance-consistent, which is the M1 goal.
- **Line numbers** are from the 2026-07-13 reading of the files and may drift; match on the surrounding code shown, not the raw line number.
