"""Cross-venue price alignment.

Our quotes are centred on the same reference price everywhere, so our own
orders never diverge between venues. Other traders' orders can. When one
venue's external bid is above another venue's external ask by more than the
fees, buy the cheap side and sell the rich side. That pulls the venues back
together and keeps the difference as profit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import ArbCfg
from .models import Balance, OrderBook


@dataclass
class ArbOpportunity:
    buy_venue: str
    sell_venue: str
    buy_price: float
    sell_price: float
    amount: float
    edge_pct: float  # net of taker fees


def find_opportunity(
    ext_books: dict[str, OrderBook],
    taker_fees: dict[str, float],
    balances: dict[str, Balance],
    cfg: ArbCfg,
) -> Optional[ArbOpportunity]:
    best: Optional[ArbOpportunity] = None
    for bv, bbook in ext_books.items():
        if not bbook.asks or bv not in balances:
            continue
        ask_p, ask_a = bbook.asks[0]
        for sv, sbook in ext_books.items():
            if sv == bv or not sbook.bids or sv not in balances:
                continue
            bid_p, bid_a = sbook.bids[0]
            edge = (bid_p / ask_p - 1) * 100 - (taker_fees.get(bv, 0.001) + taker_fees.get(sv, 0.001)) * 100
            if edge < cfg.min_edge_pct:
                continue
            amount = min(
                ask_a,
                bid_a,
                cfg.max_trade_usd / ask_p,
                balances[bv].quote_free / (ask_p * 1.002),
                balances[sv].base_free,
            )
            if amount <= 0:
                continue
            if best is None or edge > best.edge_pct:
                best = ArbOpportunity(bv, sv, ask_p, bid_p, amount, edge)
    return best
