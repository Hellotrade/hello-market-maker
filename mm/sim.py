"""Offline simulation: synthetic HELLO markets on three venues.

Lets us test the full engine (quoting, KPIs, inventory, risk) without any
network or API keys. The market model is deliberately unkind: price follows a
random walk with jumps, venues drift apart, other traders quote wide, and a
share of takers are "informed" (they trade when our quotes are stale).
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import Optional

from .alerts import Alerter
from .config import Config
from .engine import Engine
from .exchange import PaperAdapter, round_to_step
from .models import OrderBook

log = logging.getLogger("mm.sim")


@dataclass
class SimParams:
    start_price: float = 0.0025
    daily_vol_pct: float = 10.0
    jump_prob_per_hour: float = 0.5
    jump_size_pct: float = 4.0
    venue_drift_pct: float = 0.6  # how far a venue's external traders drift from true price
    ext_spread_pct: float = 4.0  # other traders quote this wide
    ext_depth_usd: float = 600.0  # their total size per side
    takers_per_min: float = 1.0  # per venue (~$115k/day per venue, near real HELLO volume)
    taker_usd_median: float = 60.0
    informed_share: float = 0.25
    price_tick: float = 1e-7
    amount_step: float = 0.01


class SimMarket:
    def __init__(self, p: SimParams, rng: random.Random):
        self.p, self.rng = p, rng
        self.t = 1_700_000_000.0
        self.price = p.start_price
        self.sigma_per_sec = p.daily_vol_pct / 100 / math.sqrt(86400)

    def step(self, dt: float) -> None:
        self.t += dt
        self.price *= math.exp(self.rng.gauss(0, self.sigma_per_sec * math.sqrt(dt)))
        if self.rng.random() < self.p.jump_prob_per_hour * dt / 3600:
            self.price *= 1 + self.rng.choice([-1, 1]) * self.p.jump_size_pct / 100


class SimSource:
    def __init__(self, venue: str, market: SimMarket, rng: random.Random):
        self.venue, self.m, self.rng = venue, market, rng
        self.offset = 0.0

    async def load(self) -> None:
        pass

    async def fetch_book(self) -> OrderBook:
        p = self.m.p
        # Mean-reverting venue offset
        self.offset = 0.98 * self.offset + self.rng.gauss(0, p.venue_drift_pct / 100 * 0.2)
        c = self.m.price * (1 + self.offset)
        half = p.ext_spread_pct / 200
        bids, asks = [], []
        for i in range(5):
            usd = p.ext_depth_usd / 5 * self.rng.uniform(0.5, 1.5)
            bp, ap = c * (1 - half - 0.01 * i), c * (1 + half + 0.01 * i)
            bids.append((round_to_step(bp, p.price_tick), usd / bp))
            asks.append((round_to_step(ap, p.price_tick), usd / ap))
        return OrderBook(self.venue, bids, asks, self.m.t)

    async def fetch_last(self) -> Optional[float]:
        return None

    def round_price(self, price: float) -> float:
        return round_to_step(price, self.m.p.price_tick)

    def round_amount(self, amount: float) -> float:
        return round_to_step(amount, self.m.p.amount_step, "down")

    async def close(self) -> None:
        pass


async def run_simulation(cfg: Config, hours: float = 6.0, seed: int = 7, params: Optional[SimParams] = None) -> dict:
    rng = random.Random(seed)
    sp = params or SimParams()
    market = SimMarket(sp, rng)
    clock = lambda: market.t  # noqa: E731
    adapters: dict[str, PaperAdapter] = {}
    for v, vc in cfg.enabled_venues.items():
        adapters[v] = PaperAdapter(
            v, SimSource(v, market, rng), vc.paper_base, vc.paper_quote,
            maker_fee=vc.maker_fee if vc.maker_fee is not None else 0.0,
            taker_fee=vc.taker_fee if vc.taker_fee is not None else 0.001,
            clock=clock,
        )
    alerter = Alerter("_NO_TOKEN_", "_NO_CHAT_", dedupe_seconds=1e12, prefix="SIM")
    engine = Engine(cfg, adapters, alerter, clock=clock, write_logs=False)
    if cfg.manual_reference_price is None:
        cfg.manual_reference_price = sp.start_price
    await engine.start()

    dt = cfg.refresh_seconds
    n_ticks = int(hours * 3600 / dt)
    taker_volume = {v: 0.0 for v in adapters}
    for _ in range(n_ticks):
        market.step(dt)
        for v, a in adapters.items():
            lam = sp.takers_per_min * dt / 60
            for _ in range(_poisson(rng, lam)):
                usd = sp.taker_usd_median * math.exp(rng.gauss(0, 0.8))
                side = rng.choice(["buy", "sell"])
                bids = [o.price for o in a.orders.values() if o.side == "buy"]
                asks = [o.price for o in a.orders.values() if o.side == "sell"]
                if rng.random() < sp.informed_share and bids and asks:
                    our_mid = (max(bids) + min(asks)) / 2
                    side = "buy" if market.price > our_mid else "sell"
                taker_volume[v] += a.apply_taker(side, usd)
        await engine.tick()
        if engine.halted:
            break

    fair = engine.fair or market.price
    for v, a in adapters.items():
        engine.balances[v] = await a.fetch_balance()
    result = {
        "hours": hours,
        "ticks": engine.tick_no,
        "start_price": sp.start_price,
        "end_true_price": market.price,
        "end_fair": fair,
        "halted": engine.halted,
        "uptime_pct": {v: engine.uptime.uptime_pct(v) for v in adapters},
        "volume_with_us_usd": taker_volume,
        "fills": {v: len(a.fills) for v, a in adapters.items()},
        "pnl": engine.pnl(fair),
        "stats": engine.stats,
        "alerts": len(alerter.sent),
        "alert_samples": alerter.sent[:8],
    }
    await engine.stop()
    return result


def _poisson(rng: random.Random, lam: float) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1
