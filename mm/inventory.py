"""Inventory skew: lean quotes against whatever we hold too much of."""
from __future__ import annotations


def inventory_skew(base_total: float, quote_total: float, price: float, target_base_ratio: float) -> float:
    """Return skew in [-1, 1].

    +1 means we hold far too many tokens (quote lower, sell more, buy less).
    -1 means we hold far too few tokens (quote higher, buy more, sell less).
    """
    base_value = base_total * price
    total = base_value + quote_total
    if total <= 0:
        return 0.0
    ratio = base_value / total
    if ratio >= target_base_ratio:
        denom = 1 - target_base_ratio
    else:
        denom = target_base_ratio
    if denom <= 0:
        return 0.0
    return max(-1.0, min(1.0, (ratio - target_base_ratio) / denom))
