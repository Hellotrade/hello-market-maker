"""Reference (fair) price across venues.

Our own orders are removed from each book before pricing. Otherwise the bot
would chase its own quotes: inventory skew shifts our quotes, the book mid
moves, the next tick reads that mid as the new price and skews again, and the
price drifts away. Fills against us still move the price through the last
trade, which is how genuine buying and selling reaches the reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import OpenOrder, OrderBook

MAX_EXTERNAL_SPREAD_PCT = 5.0  # wider than this, the external mid is not informative


def external_book(book: OrderBook, own: list[OpenOrder]) -> OrderBook:
    """Return the book with our own resting orders subtracted."""

    def strip(levels: list[tuple[float, float]], side: str) -> list[tuple[float, float]]:
        mine: dict[float, float] = {}
        for o in own:
            if o.side == side:
                mine[o.price] = mine.get(o.price, 0.0) + o.amount
        out = []
        for price, amount in levels:
            for p, a in list(mine.items()):
                if abs(p - price) <= max(abs(price), 1e-18) * 1e-9:
                    take = min(a, amount)
                    amount -= take
                    mine[p] = a - take
            if amount > max(1e-12, 1e-9 * amount):
                out.append((price, amount))
        return out

    return OrderBook(
        venue=book.venue,
        bids=strip(book.bids, "buy"),
        asks=strip(book.asks, "sell"),
        timestamp=book.timestamp,
    )


def venue_price(ext: Optional[OrderBook], last: Optional[float]) -> Optional[float]:
    if ext is not None and ext.mid and ext.spread_pct is not None and ext.spread_pct <= MAX_EXTERNAL_SPREAD_PCT:
        return ext.mid
    if last and last > 0:
        return last
    if ext is not None and ext.mid:
        return ext.mid
    return None


def weighted_median(values: list[tuple[float, float]]) -> float:
    """values: (value, weight)."""
    vals = sorted(values)
    total = sum(w for _, w in vals)
    acc = 0.0
    for v, w in vals:
        acc += w
        if acc >= total / 2:
            return v
    return vals[-1][0]


@dataclass
class RefPrice:
    price: float
    used: dict[str, float]  # venue -> price used
    rejected: dict[str, float]


def reference_price(
    prices: dict[str, Optional[float]],
    weights: dict[str, float],
    outlier_pct: float,
) -> Optional[RefPrice]:
    valid = {v: p for v, p in prices.items() if p and p > 0}
    if not valid:
        return None
    med = weighted_median([(p, 1.0) for p in valid.values()])
    used = {v: p for v, p in valid.items() if abs(p / med - 1) * 100 <= outlier_pct}
    rejected = {v: p for v, p in valid.items() if v not in used}
    price = weighted_median([(p, weights.get(v, 1.0)) for v, p in used.items()])
    return RefPrice(price=price, used=used, rejected=rejected)
