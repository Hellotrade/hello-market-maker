"""Plain data types shared across the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Side = Literal["buy", "sell"]


@dataclass
class OrderBook:
    venue: str
    bids: list[tuple[float, float]]  # (price, amount), best first (descending)
    asks: list[tuple[float, float]]  # (price, amount), best first (ascending)
    timestamp: float  # unix seconds

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if m is None or m <= 0:
            return None
        return (self.best_ask - self.best_bid) / m * 100


@dataclass(frozen=True)
class DesiredOrder:
    side: Side
    price: float
    amount: float
    level: int

    @property
    def notional(self) -> float:
        return self.price * self.amount


@dataclass
class OpenOrder:
    id: str
    side: Side
    price: float
    amount: float  # remaining amount

    @property
    def notional(self) -> float:
        return self.price * self.amount


@dataclass
class Balance:
    base_free: float
    base_total: float
    quote_free: float
    quote_total: float


@dataclass
class VenueState:
    """Everything fetched from one venue in one tick."""

    book: Optional[OrderBook] = None
    last: Optional[float] = None
    open_orders: list[OpenOrder] = field(default_factory=list)
    balance: Optional[Balance] = None
    error: Optional[str] = None
