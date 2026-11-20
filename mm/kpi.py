"""Measures what the exchange measures: spread, depth inside the band, uptime."""
from __future__ import annotations

import csv
import math
import os
from collections import deque
from dataclasses import asdict, dataclass
from typing import Optional

from .config import KpiCfg
from .models import OpenOrder, OrderBook


@dataclass
class KpiSnapshot:
    venue: str
    timestamp: float
    mid: Optional[float]
    spread_pct: float
    bid_depth_usd: float
    ask_depth_usd: float
    own_bid_depth_usd: float
    own_ask_depth_usd: float
    spread_ok: bool
    depth_ok: bool

    @property
    def compliant(self) -> bool:
        return self.spread_ok and self.depth_ok


def _depth(levels, lo: float, hi: float) -> float:
    return sum(p * a for p, a in levels if lo <= p <= hi)


def measure(book: Optional[OrderBook], own: list[OpenOrder], k: KpiCfg, venue: str, now: float) -> KpiSnapshot:
    if book is None or book.mid is None:
        return KpiSnapshot(venue, now, None, math.inf, 0, 0, 0, 0, False, False)
    mid = book.mid
    lo, hi = mid * (1 - k.depth_band_pct / 100), mid * (1 + k.depth_band_pct / 100)
    bid_d = _depth(book.bids, lo, mid)
    ask_d = _depth(book.asks, mid, hi)
    own_bid = sum(o.notional for o in own if o.side == "buy" and lo <= o.price <= mid)
    own_ask = sum(o.notional for o in own if o.side == "sell" and mid <= o.price <= hi)
    spread = book.spread_pct
    return KpiSnapshot(
        venue=venue,
        timestamp=now,
        mid=mid,
        spread_pct=spread,
        bid_depth_usd=bid_d,
        ask_depth_usd=ask_d,
        own_bid_depth_usd=own_bid,
        own_ask_depth_usd=own_ask,
        spread_ok=spread <= k.max_spread_pct,
        depth_ok=bid_d >= k.min_depth_usd and ask_d >= k.min_depth_usd,
    )


class UptimeTracker:
    """Rolling compliance percentage per venue (default 24h window)."""

    def __init__(self, window_seconds: float = 86400):
        self.window = window_seconds
        self.samples: dict[str, deque] = {}
        self.streak: dict[str, int] = {}  # consecutive non-compliant ticks

    def add(self, snap: KpiSnapshot) -> None:
        dq = self.samples.setdefault(snap.venue, deque())
        dq.append((snap.timestamp, snap.compliant))
        while dq and dq[0][0] < snap.timestamp - self.window:
            dq.popleft()
        self.streak[snap.venue] = 0 if snap.compliant else self.streak.get(snap.venue, 0) + 1

    def uptime_pct(self, venue: str) -> Optional[float]:
        dq = self.samples.get(venue)
        if not dq:
            return None
        return 100.0 * sum(1 for _, ok in dq if ok) / len(dq)


class KpiLogger:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "kpi_log.csv")
        self._new = not os.path.exists(self.path)

    def write(self, snap: KpiSnapshot) -> None:
        row = asdict(snap)
        row["compliant"] = snap.compliant
        with open(self.path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if self._new:
                w.writeheader()
                self._new = False
            w.writerow(row)
