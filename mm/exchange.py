"""Exchange adapters.

CcxtAdapter   - real MEXC / Bybit / Gate via ccxt (live trading).
PaperAdapter  - real or simulated market data, virtual balances and orders.
                Nothing is sent to the exchange. Use this before going live.
"""
from __future__ import annotations

import itertools
import logging
import math
import time
from typing import Optional, Protocol

from .config import VenueCfg
from .models import Balance, OpenOrder, OrderBook, Side

log = logging.getLogger("mm.exchange")


class InsufficientFunds(Exception):
    pass


class Adapter(Protocol):
    venue: str
    maker_fee: float
    taker_fee: float
    min_amount: float
    min_cost: float

    async def load(self) -> None: ...
    async def fetch_book(self) -> OrderBook: ...
    async def fetch_last(self) -> Optional[float]: ...
    async def fetch_balance(self) -> Balance: ...
    async def fetch_open_orders(self) -> list[OpenOrder]: ...
    async def create_limit(self, side: Side, price: float, amount: float, post_only: bool) -> str: ...
    async def create_ioc(self, side: Side, price: float, amount: float) -> str: ...
    async def cancel(self, order_id: str) -> None: ...
    async def cancel_all(self) -> None: ...
    def round_price(self, price: float) -> float: ...
    def round_amount(self, amount: float) -> float: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------- ccxt
class CcxtAdapter:
    def __init__(self, venue: str, cfg: VenueCfg, creds: Optional[dict] = None):
        import ccxt.async_support as ccxt_async

        self.venue = venue
        self.cfg = cfg
        self.symbol = cfg.symbol
        opts: dict = {"enableRateLimit": True, **(creds or {})}
        if cfg.ccxt_id == "bybit":
            opts["options"] = {"defaultType": "spot"}
        self.ex = getattr(ccxt_async, cfg.ccxt_id)(opts)
        self.maker_fee = cfg.maker_fee if cfg.maker_fee is not None else 0.002
        self.taker_fee = cfg.taker_fee if cfg.taker_fee is not None else 0.002
        self.min_amount = 0.0
        self.min_cost = 0.0
        self.market: dict = {}

    async def load(self) -> None:
        await self.ex.load_markets()
        if self.symbol not in self.ex.markets:
            raise ValueError(f"{self.venue}: symbol {self.symbol} not listed")
        self.market = self.ex.market(self.symbol)
        if self.cfg.maker_fee is None and self.market.get("maker") is not None:
            self.maker_fee = float(self.market["maker"])
        if self.cfg.taker_fee is None and self.market.get("taker") is not None:
            self.taker_fee = float(self.market["taker"])
        limits = self.market.get("limits") or {}
        self.min_amount = float((limits.get("amount") or {}).get("min") or 0)
        self.min_cost = float((limits.get("cost") or {}).get("min") or 0)

    async def fetch_book(self) -> OrderBook:
        ob = await self.ex.fetch_order_book(self.symbol, limit=50)
        ts = (ob.get("timestamp") or time.time() * 1000) / 1000
        return OrderBook(
            self.venue,
            [(float(p), float(a)) for p, a, *_ in ob["bids"]],
            [(float(p), float(a)) for p, a, *_ in ob["asks"]],
            ts,
        )

    async def fetch_last(self) -> Optional[float]:
        t = await self.ex.fetch_ticker(self.symbol)
        return float(t["last"]) if t.get("last") else None

    async def fetch_balance(self) -> Balance:
        b = await self.ex.fetch_balance()
        base = b.get(self.market["base"], {}) or {}
        quote = b.get(self.market["quote"], {}) or {}
        return Balance(
            float(base.get("free") or 0),
            float(base.get("total") or 0),
            float(quote.get("free") or 0),
            float(quote.get("total") or 0),
        )

    async def fetch_open_orders(self) -> list[OpenOrder]:
        raw = await self.ex.fetch_open_orders(self.symbol)
        out = []
        for o in raw:
            remaining = o.get("remaining")
            if remaining is None:
                remaining = (o.get("amount") or 0) - (o.get("filled") or 0)
            out.append(OpenOrder(str(o["id"]), o["side"], float(o["price"]), float(remaining)))
        return out

    async def create_limit(self, side: Side, price: float, amount: float, post_only: bool) -> str:
        params = {"postOnly": True} if post_only else {}
        o = await self.ex.create_order(self.symbol, "limit", side, amount, price, params)
        return str(o["id"])

    async def create_ioc(self, side: Side, price: float, amount: float) -> str:
        o = await self.ex.create_order(self.symbol, "limit", side, amount, price, {"timeInForce": "IOC"})
        return str(o["id"])

    async def cancel(self, order_id: str) -> None:
        await self.ex.cancel_order(order_id, self.symbol)

    async def cancel_all(self) -> None:
        if self.ex.has.get("cancelAllOrders"):
            try:
                await self.ex.cancel_all_orders(self.symbol)
                return
            except Exception as e:
                log.warning("%s cancel_all_orders failed, falling back: %s", self.venue, e)
        for o in await self.fetch_open_orders():
            try:
                await self.cancel(o.id)
            except Exception as e:
                log.warning("%s cancel %s failed: %s", self.venue, o.id, e)

    def round_price(self, price: float) -> float:
        return float(self.ex.price_to_precision(self.symbol, price))

    def round_amount(self, amount: float) -> float:
        return float(self.ex.amount_to_precision(self.symbol, amount))

    async def close(self) -> None:
        await self.ex.close()


# -------------------------------------------------------------------- paper
class MarketSource(Protocol):
    async def load(self) -> None: ...
    async def fetch_book(self) -> OrderBook: ...
    async def fetch_last(self) -> Optional[float]: ...
    def round_price(self, price: float) -> float: ...
    def round_amount(self, amount: float) -> float: ...
    async def close(self) -> None: ...


class PublicCcxtSource:
    """Read-only live market data for paper trading."""

    def __init__(self, venue: str, cfg: VenueCfg):
        self.inner = CcxtAdapter(venue, cfg, creds=None)

    async def load(self) -> None:
        await self.inner.load()

    async def fetch_book(self) -> OrderBook:
        return await self.inner.fetch_book()

    async def fetch_last(self) -> Optional[float]:
        return await self.inner.fetch_last()

    def round_price(self, price: float) -> float:
        return self.inner.round_price(price)

    def round_amount(self, amount: float) -> float:
        return self.inner.round_amount(amount)

    async def close(self) -> None:
        await self.inner.close()


class PaperAdapter:
    """Virtual orders and balances on top of a market data source.

    Fill model:
    * if the external market crosses one of our orders, it fills at our price
      (someone else's order would have traded with ours);
    * apply_taker() lets a simulation send market orders that sweep the
      combined book (our orders plus external ones).
    With live data, real takers can't see our virtual orders, so paper mode
    under-counts fills. It is for checking quoting and KPIs, not P&L.
    """

    _ids = itertools.count(1)

    def __init__(self, venue: str, source: MarketSource, base: float, quote: float,
                 maker_fee: float = 0.0, taker_fee: float = 0.001, clock=time.time):
        self.venue = venue
        self.source = source
        self.base = base
        self.quote = quote
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.min_amount = 0.0
        self.min_cost = 1.0
        self.orders: dict[str, OpenOrder] = {}
        self.last: Optional[float] = None
        self.fills: list[tuple[float, Side, float, float]] = []  # (t, our side, price, amount)
        self.clock = clock
        self._ext: Optional[OrderBook] = None

    async def load(self) -> None:
        await self.source.load()

    # -- internal fills
    def _fill(self, o: OpenOrder, amount: float, price: float) -> None:
        if o.side == "buy":
            self.base += amount
            self.quote -= price * amount * (1 + self.maker_fee)
        else:
            self.base -= amount
            self.quote += price * amount * (1 - self.maker_fee)
        o.amount -= amount
        self.last = price
        self.fills.append((self.clock(), o.side, price, amount))
        if o.amount <= 1e-12:
            self.orders.pop(o.id, None)

    def _match_external(self, ext: OrderBook) -> None:
        for o in list(self.orders.values()):
            if o.side == "buy" and ext.best_ask is not None and ext.best_ask <= o.price:
                self._fill(o, o.amount, o.price)
            elif o.side == "sell" and ext.best_bid is not None and ext.best_bid >= o.price:
                self._fill(o, o.amount, o.price)

    def _merged(self, ext: OrderBook) -> OrderBook:
        bids = list(ext.bids) + [(o.price, o.amount) for o in self.orders.values() if o.side == "buy"]
        asks = list(ext.asks) + [(o.price, o.amount) for o in self.orders.values() if o.side == "sell"]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return OrderBook(self.venue, bids, asks, ext.timestamp)

    def apply_taker(self, side: Side, quote_usd: float) -> float:
        """A market order from someone else. side is the TAKER's side.
        Sweeps our orders and the external book best-price first. Returns USD traded with us."""
        ext = self._ext
        ours = [o for o in self.orders.values() if o.side == ("sell" if side == "buy" else "buy")]
        levels: list[tuple[float, float, Optional[OpenOrder]]] = [(o.price, o.amount, o) for o in ours]
        if ext is not None:
            levels += [(p, a, None) for p, a in (ext.asks if side == "buy" else ext.bids)]
        levels.sort(key=lambda x: x[0] if side == "buy" else -x[0])
        remaining, with_us = quote_usd, 0.0
        for price, amount, order in levels:
            if remaining <= 0:
                break
            take = min(amount, remaining / price)
            remaining -= take * price
            if order is not None:
                self._fill(order, take, price)
                with_us += take * price
            else:
                self.last = price
        return with_us

    # -- adapter API
    async def fetch_book(self) -> OrderBook:
        ext = await self.source.fetch_book()
        self._ext = ext
        self._match_external(ext)
        return self._merged(ext)

    async def fetch_last(self) -> Optional[float]:
        if self.last is not None:
            return self.last
        return await self.source.fetch_last()

    async def fetch_balance(self) -> Balance:
        locked_q = sum(o.price * o.amount * (1 + self.maker_fee) for o in self.orders.values() if o.side == "buy")
        locked_b = sum(o.amount for o in self.orders.values() if o.side == "sell")
        return Balance(self.base - locked_b, self.base, self.quote - locked_q, self.quote)

    async def fetch_open_orders(self) -> list[OpenOrder]:
        return [OpenOrder(o.id, o.side, o.price, o.amount) for o in self.orders.values()]

    async def create_limit(self, side: Side, price: float, amount: float, post_only: bool) -> str:
        bal = await self.fetch_balance()
        if side == "buy" and price * amount * (1 + self.maker_fee) > bal.quote_free + 1e-9:
            raise InsufficientFunds(f"{self.venue} buy needs {price * amount:.2f} USDT")
        if side == "sell" and amount > bal.base_free + 1e-9:
            raise InsufficientFunds(f"{self.venue} sell needs {amount:.4f} base")
        ext = self._ext
        if post_only and ext is not None:
            if side == "buy" and ext.best_ask is not None and price >= ext.best_ask:
                raise ValueError("post-only order would take")
            if side == "sell" and ext.best_bid is not None and price <= ext.best_bid:
                raise ValueError("post-only order would take")
        oid = f"p{next(self._ids)}"
        self.orders[oid] = OpenOrder(oid, side, price, amount)
        return oid

    async def create_ioc(self, side: Side, price: float, amount: float) -> str:
        ext = self._ext
        oid = f"p{next(self._ids)}"
        if ext is None:
            return oid
        levels = ext.asks if side == "buy" else ext.bids
        remaining = amount
        for p, a in levels:
            if remaining <= 0 or (side == "buy" and p > price) or (side == "sell" and p < price):
                break
            take = min(a, remaining)
            remaining -= take
            if side == "buy":
                self.base += take
                self.quote -= p * take * (1 + self.taker_fee)
            else:
                self.base -= take
                self.quote += p * take * (1 - self.taker_fee)
            self.last = p
        return oid

    async def cancel(self, order_id: str) -> None:
        self.orders.pop(order_id, None)

    async def cancel_all(self) -> None:
        self.orders.clear()

    def round_price(self, price: float) -> float:
        return self.source.round_price(price)

    def round_amount(self, amount: float) -> float:
        return self.source.round_amount(amount)

    async def close(self) -> None:
        await self.source.close()


def round_to_step(value: float, step: float, mode: str = "nearest") -> float:
    if step <= 0:
        return value
    n = value / step
    n = math.floor(n + 1e-9) if mode == "down" else round(n)
    return round(n * step, 12)
