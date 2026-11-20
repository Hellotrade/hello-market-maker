"""Works out which orders to cancel and which to place.

Replacing every order on every tick burns rate limits and briefly empties the
book, so an existing order is kept if it is close enough to what we want.
"""
from __future__ import annotations

from .models import DesiredOrder, OpenOrder


def diff_orders(
    desired: list[DesiredOrder],
    open_orders: list[OpenOrder],
    price_tol_pct: float,
    size_tol_pct: float,
) -> tuple[list[OpenOrder], list[DesiredOrder]]:
    to_cancel: list[OpenOrder] = []
    to_place: list[DesiredOrder] = []
    for side in ("buy", "sell"):
        want = sorted((d for d in desired if d.side == side), key=lambda d: d.price)
        have = [o for o in open_orders if o.side == side]
        unmatched = list(have)
        for d in want:
            best, best_dist = None, None
            for o in unmatched:
                dp = abs(o.price / d.price - 1) * 100
                ds = abs(o.amount / d.amount - 1) * 100 if d.amount > 0 else 0
                if dp <= price_tol_pct and ds <= size_tol_pct and (best_dist is None or dp < best_dist):
                    best, best_dist = o, dp
            if best is not None:
                unmatched.remove(best)
            else:
                to_place.append(d)
        to_cancel.extend(unmatched)
    return to_cancel, to_place
