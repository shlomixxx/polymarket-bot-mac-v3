"""
ולידציית גודל עסקה מול מינימום Polymarket — עם buffer קטן מעל המינימום.
"""
from __future__ import annotations

# "קצת מעל" המינימום — מונע דחייה בגלל עיגול צף
EXCHANGE_MIN_BUFFER = 0.01


def effective_minimum_contracts(order_min_size: float) -> float:
    """מינימום אפקטיבי לבדיקה (מינימום השוק + buffer)."""
    return float(order_min_size) + EXCHANGE_MIN_BUFFER


def validate_contracts_for_market(
    contracts: float,
    order_min_size: float,
    *,
    bump_if_needed: bool = True,
) -> tuple[bool, float, str | None]:
    """
    מחזיר (ok, contracts_to_use, error_message).
    אם bump_if_needed וחוזים בין order_min_size ל-effective_min — מעלה ל-effective_min.
    """
    oms = float(order_min_size)
    eff = effective_minimum_contracts(oms)
    c = float(contracts)
    if c < oms:
        return False, c, f"מתחת למינימום השוק ({oms} חוזים)"
    if c < eff:
        if bump_if_needed:
            return True, eff, None
        return False, c, f"חייב לפחות {eff:.2f} חוזים (מינימום {oms} + buffer)"
    return True, c, None


def contracts_meet_market_minimum(contracts: float, order_min_size: float) -> bool:
    ok, _, _ = validate_contracts_for_market(contracts, order_min_size, bump_if_needed=True)
    return ok
