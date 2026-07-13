# engine/venues/__init__.py
"""get_venue(name) -> singleton Venue. Singletons preserve live_clob's client/portfolio caches."""
from __future__ import annotations

from .base import Venue, ActiveMarket, VALID_ORDER_VENUES, normalize

_INSTANCES: dict[str, Venue] = {}


def get_venue(name: str) -> Venue:
    key = normalize(name)
    inst = _INSTANCES.get(key)
    if inst is None:
        if key == "predict_fun":
            from .predict_fun import PredictFunVenue
            inst = PredictFunVenue()
        else:
            from .polymarket import PolymarketVenue
            inst = PolymarketVenue()
        _INSTANCES[key] = inst
    return inst
