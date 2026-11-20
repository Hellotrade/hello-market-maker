"""Builds the order ladder that satisfies the exchange KPIs."""
from __future__ import annotations

from dataclasses import dataclass

from .config import InventoryCfg, KpiCfg, QuotingCfg
from .models import DesiredOrder


def build_ladder(
    fair: float,
    skew: float,
    q: QuotingCfg,
    k: KpiCfg,
    inv: InventoryCfg,
    wide: bool = False,
) -> list[DesiredOrder]:
    """Symmetric ladder around a (skewed) centre.

    Inner levels sit inside the KPI depth band and together carry
    min_depth_usd * depth_safety_factor per side. Outer levels add extra
    liquidity further out and do not count toward the KPI.
    """
    spread = q.wide_spread_pct if wide else q.target_spread_pct
    half = spread / 200.0
    band = k.depth_band_pct / 100.0 * q.band_margin
    center = fair * (1 - skew * inv.max_price_skew_pct / 100.0)

    if q.levels == 1:
        offsets = [half]
    else:
        offsets = [half + (band - half) * i / (q.levels - 1) for i in range(q.levels)]

    per_level = k.min_depth_usd * q.depth_safety_factor / q.levels
    # Lean sizes against inventory, but never below the KPI minimum (+5%).
    floor = 1.05 / q.depth_safety_factor
    bid_mult = max(floor, 1 - inv.size_skew * skew)
    ask_mult = max(floor, 1 + inv.size_skew * skew)

    orders: list[DesiredOrder] = []
    for i, off in enumerate(offsets):
        bp, ap = center * (1 - off), center * (1 + off)
        orders.append(DesiredOrder("buy", bp, per_level * bid_mult / bp, i))
        orders.append(DesiredOrder("sell", ap, per_level * ask_mult / ap, i))

    for j, off_pct in enumerate(q.outer_offsets_pct):
        off = off_pct / 100.0
        if off <= band:
            continue
        lvl = q.levels + j
        bp, ap = center * (1 - off), center * (1 + off)
        orders.append(DesiredOrder("buy", bp, q.outer_level_usd / bp, lvl))
        orders.append(DesiredOrder("sell", ap, q.outer_level_usd / ap, lvl))
    return orders


@dataclass
class Shortfall:
    side: str
    needed: float  # in quote (buy) or base (sell)
    available: float


def cap_to_balance(
    orders: list[DesiredOrder],
    quote_budget: float,
    base_budget: float,
) -> tuple[list[DesiredOrder], list[Shortfall]]:
    """Scale each side down proportionally if we can't fund it.

    Budgets should be total balance minus reserve, since we cancel stale orders
    before placing new ones and those funds come back.
    Inner (KPI) levels are funded first, then outer levels.
    """
    shortfalls: list[Shortfall] = []
    out: list[DesiredOrder] = []
    for side, budget, cost in (
        ("buy", max(0.0, quote_budget), lambda o: o.notional),
        ("sell", max(0.0, base_budget), lambda o: o.amount),
    ):
        side_orders = sorted((o for o in orders if o.side == side), key=lambda o: o.level)
        need = sum(cost(o) for o in side_orders)
        if need <= budget:
            out.extend(side_orders)
            continue
        shortfalls.append(Shortfall(side, need, budget))
        remaining = budget
        for o in side_orders:
            c = cost(o)
            if c <= remaining:
                out.append(o)
                remaining -= c
            elif remaining > 0:
                frac = remaining / c
                out.append(DesiredOrder(o.side, o.price, o.amount * frac, o.level))
                remaining = 0
    return out, shortfalls
