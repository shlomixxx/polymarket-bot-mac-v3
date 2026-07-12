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
