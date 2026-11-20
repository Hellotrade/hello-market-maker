"""The main loop: fetch, price, quote, reconcile, measure, repeat."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .alerts import Alerter
from .arbitrage import find_opportunity
from .config import Config
from .exchange import Adapter
from .inventory import inventory_skew
from .kpi import KpiLogger, KpiSnapshot, UptimeTracker, measure
from .models import Balance, DesiredOrder, VenueState
from .order_manager import diff_orders
from .pricing import external_book, reference_price, venue_price
from .quoting import cap_to_balance, build_ladder
from .risk import RiskManager

log = logging.getLogger("mm.engine")


class Engine:
    def __init__(self, cfg: Config, adapters: dict[str, Adapter], alerter: Alerter,
                 clock=time.time, write_logs: bool = True):
        self.cfg = cfg
        self.adapters = adapters
        self.alerter = alerter
        self.clock = clock
        self.risk = RiskManager(cfg.risk)
        self.uptime = UptimeTracker()
        self.kpi_log = KpiLogger(cfg.data_dir) if write_logs else None
        self.balances: dict[str, Balance] = {}
        self.fair: Optional[float] = None
        self.tick_no = 0
        self.halted = False
        self.last_kpis: dict[str, KpiSnapshot] = {}
        self.start_base = 0.0
        self.start_quote = 0.0
        self._last_arb = 0.0
        self.stats = {"placed": 0, "cancelled": 0, "errors": 0, "arbs": 0}

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await asyncio.gather(*(a.load() for a in self.adapters.values()))
        # Clean slate: remove anything left from a previous run.
        await asyncio.gather(*(a.cancel_all() for a in self.adapters.values()), return_exceptions=True)
        for v, a in self.adapters.items():
            self.balances[v] = await a.fetch_balance()
        self.start_base = sum(b.base_total for b in self.balances.values())
        self.start_quote = sum(b.quote_total for b in self.balances.values())
        log.info("started: %d venues, base=%.2f quote=%.2f", len(self.adapters), self.start_base, self.start_quote)

    async def stop(self) -> None:
        await asyncio.gather(*(a.cancel_all() for a in self.adapters.values()), return_exceptions=True)
        await asyncio.gather(*(a.close() for a in self.adapters.values()), return_exceptions=True)

    async def run(self) -> None:
        await self.start()
        try:
            while not self.halted:
                t0 = time.time()
                try:
                    await self.tick()
                except Exception as e:  # never die silently mid-loop
                    self.stats["errors"] += 1
                    log.exception("tick failed")
                    await self.alerter.send("tick_error", f"tick error: {e}")
                await asyncio.sleep(max(0.0, self.cfg.refresh_seconds - (time.time() - t0)))
        finally:
            await self.stop()

    # ---------------------------------------------------------------- tick
    async def _fetch(self, venue: str, a: Adapter, with_balance: bool) -> VenueState:
        st = VenueState()
        try:
            calls = [a.fetch_book(), a.fetch_last(), a.fetch_open_orders()]
            if with_balance:
                calls.append(a.fetch_balance())
            res = await asyncio.gather(*calls)
            st.book, st.last, st.open_orders = res[0], res[1], res[2]
            st.balance = res[3] if with_balance else self.balances.get(venue)
        except Exception as e:
            st.error = str(e)
        return st

    def portfolio_value(self, price: float) -> float:
        return sum(b.base_total * price + b.quote_total for b in self.balances.values())

    async def _pull_all(self, reason: str) -> None:
        await asyncio.gather(*(a.cancel_all() for a in self.adapters.values()), return_exceptions=True)
        await self.alerter.send(f"pull:{reason}", f"all quotes pulled: {reason}", force=True)

    async def tick(self) -> None:
        now = self.clock()
        self.tick_no += 1
        cfg = self.cfg
        with_bal = self.tick_no % cfg.balance_every_ticks == 1 or cfg.balance_every_ticks == 1

        if self.risk.kill_requested():
            await self._pull_all("kill file present")
            self.halted = True
            return

        venues = list(self.adapters)
        states = dict(zip(venues, await asyncio.gather(
            *(self._fetch(v, self.adapters[v], with_bal) for v in venues))))

        healthy: dict[str, VenueState] = {}
        for v, st in states.items():
            if st.error or st.book is None:
                await self.alerter.send(f"fetch:{v}", f"{v}: data fetch failed ({st.error})")
                continue
            if now - st.book.timestamp > cfg.risk.stale_seconds:
                await self.alerter.send(f"stale:{v}", f"{v}: stale order book, pulling quotes")
                try:
                    await self.adapters[v].cancel_all()
                except Exception:
                    pass
                continue
            if st.balance is not None:
                self.balances[v] = st.balance
            healthy[v] = st

        # --- reference price from books with our own orders stripped out
        ext = {v: external_book(st.book, st.open_orders) for v, st in healthy.items()}
        prices = {v: venue_price(ext[v], healthy[v].last) for v in healthy}
        weights = {v: cfg.venues[v].weight for v in healthy}
        ref = reference_price(prices, weights, cfg.risk.outlier_pct)
        if ref is not None:
            self.fair = ref.price
            for v, p in ref.rejected.items():
                await self.alerter.send(f"outlier:{v}", f"{v}: price {p:.6g} is an outlier vs {ref.price:.6g}, not quoting there")
        elif self.fair is None and cfg.manual_reference_price:
            self.fair = cfg.manual_reference_price
        if self.fair is None:
            await self.alerter.send("noprice", "no reference price available; set manual_reference_price")
            return
        fair = self.fair
        self.risk.record_price(now, fair)

        # --- portfolio risk
        value = self.portfolio_value(fair)
        self.risk.set_start_value(value)
        if self.risk.drawdown_breached(value):
            await self._pull_all(f"drawdown {self.risk.drawdown_pct(value):.1f}% hit limit")
            self.halted = True
            return
        if self.risk.halt_move():
            await self._pull_all(f"price moved {self.risk.window_move_pct():.1f}% in {cfg.risk.vol_window_seconds:.0f}s")
            return
        wide = self.risk.volatile()

        # --- quote each healthy venue
        quotable = [v for v in healthy if ref is None or v not in ref.rejected]
        await asyncio.gather(*(self._quote_venue(v, healthy[v], fair, wide) for v in quotable))

        # --- cross-venue alignment
        if cfg.arbitrage.enabled and now - self._last_arb >= cfg.arbitrage.cooldown_seconds:
            await self._arbitrage({v: ext[v] for v in quotable})

        # --- KPI measurement on the books we fetched this tick
        for v, st in healthy.items():
            snap = measure(st.book, st.open_orders, cfg.kpi, v, now)
            self.last_kpis[v] = snap
            self.uptime.add(snap)
            if self.kpi_log:
                self.kpi_log.write(snap)
            if self.uptime.streak.get(v, 0) >= cfg.risk.noncompliant_alert_ticks:
                await self.alerter.send(
                    f"kpi:{v}",
                    f"{v}: KPI failing for {self.uptime.streak[v]} ticks "
                    f"(spread {snap.spread_pct:.2f}%, bid ${snap.bid_depth_usd:,.0f}, ask ${snap.ask_depth_usd:,.0f})",
                )

        if self.tick_no % max(1, int(60 / max(cfg.refresh_seconds, 0.1))) == 0:
            self.log_status(fair, value)

    async def _quote_venue(self, v: str, st: VenueState, fair: float, wide: bool) -> None:
        cfg, a = self.cfg, self.adapters[v]
        bal = self.balances.get(v)
        if bal is None:
            return
        skew = inventory_skew(bal.base_total, bal.quote_total, fair, cfg.inventory.target_base_ratio)
        ladder = build_ladder(fair, skew, cfg.quoting, cfg.kpi, cfg.inventory, wide=wide)
        quote_budget = bal.quote_total / (1 + a.maker_fee) - cfg.risk.quote_reserve
        base_budget = bal.base_total - cfg.risk.base_reserve_usd / fair
        ladder, shortfalls = cap_to_balance(ladder, quote_budget, base_budget)
        for s in shortfalls:
            unit = "USDT" if s.side == "buy" else "tokens"
            await self.alerter.send(
                f"funds:{v}:{s.side}",
                f"{v}: not enough {unit} for full {s.side} ladder (need {s.needed:,.2f}, have {s.available:,.2f}). Rebalance.",
            )
        ladder = self._round(a, ladder, fair)
        to_cancel, to_place = diff_orders(
            ladder, st.open_orders, cfg.quoting.price_tolerance_pct, cfg.quoting.size_tolerance_pct)

        # Cancel first so the freed balance can fund replacements.
        for o in to_cancel:
            try:
                await a.cancel(o.id)
                self.stats["cancelled"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                log.warning("%s cancel %s failed: %s", v, o.id, e)
        # Place inner levels first: they carry the KPI.
        for d in sorted(to_place, key=lambda d: d.level):
            try:
                await a.create_limit(d.side, d.price, d.amount, cfg.quoting.post_only)
                self.stats["placed"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                log.info("%s place %s %.8g x %.4f failed: %s", v, d.side, d.price, d.amount, e)

    def _round(self, a: Adapter, orders: list[DesiredOrder], fair: float) -> list[DesiredOrder]:
        seen: set[tuple[str, float]] = set()
        out = []
        for d in orders:
            p, amt = a.round_price(d.price), a.round_amount(d.amount)
            if p <= 0 or amt <= 0 or amt < a.min_amount or p * amt < a.min_cost:
                continue
            key = (d.side, p)
            if key in seen:
                continue
            seen.add(key)
            out.append(DesiredOrder(d.side, p, amt, d.level))
        # Our own bids and asks must never cross (would self-trade).
        bids = [d.price for d in out if d.side == "buy"]
        asks = [d.price for d in out if d.side == "sell"]
        if bids and asks and max(bids) >= min(asks):
            mid = (max(bids) + min(asks)) / 2
            out = [d for d in out if (d.side == "buy" and d.price < mid) or (d.side == "sell" and d.price > mid)]
        return out

    async def _arbitrage(self, ext_books) -> None:
        cfg = self.cfg
        taker = {v: self.adapters[v].taker_fee for v in ext_books}
        opp = find_opportunity(ext_books, taker, self.balances, cfg.arbitrage)
        if opp is None:
            return
        self._last_arb = self.clock()
        buy_a, sell_a = self.adapters[opp.buy_venue], self.adapters[opp.sell_venue]
        amt = buy_a.round_amount(opp.amount)
        if amt <= 0:
            return
        res = await asyncio.gather(
            buy_a.create_ioc("buy", opp.buy_price, amt),
            sell_a.create_ioc("sell", opp.sell_price, sell_a.round_amount(amt)),
            return_exceptions=True,
        )
        self.stats["arbs"] += 1
        failed = [r for r in res if isinstance(r, Exception)]
        await self.alerter.send(
            "arb",
            f"arb: buy {opp.buy_venue} @ {opp.buy_price:.6g}, sell {opp.sell_venue} @ {opp.sell_price:.6g}, "
            f"{amt:.2f} tokens, edge {opp.edge_pct:.2f}%" + (f", {len(failed)} leg(s) failed: {failed}" if failed else ""),
            force=bool(failed),
        )

    # ------------------------------------------------------------- report
    def pnl(self, price: float) -> dict:
        base = sum(b.base_total for b in self.balances.values())
        quote = sum(b.quote_total for b in self.balances.values())
        hold_value = self.start_base * price + self.start_quote
        return {
            "base_delta": base - self.start_base,
            "quote_delta": quote - self.start_quote,
            "value_now": base * price + quote,
            "value_if_held": hold_value,
            "vs_hold": base * price + quote - hold_value,
        }

    def log_status(self, fair: float, value: float) -> None:
        parts = []
        for v, s in self.last_kpis.items():
            up = self.uptime.uptime_pct(v)
            parts.append(f"{v}: spr {s.spread_pct:.2f}% bid ${s.bid_depth_usd:,.0f} ask ${s.ask_depth_usd:,.0f} up {up:.1f}%")
        p = self.pnl(fair)
        log.info(f"fair {fair:.6g} | value ${value:,.0f} | tokens {p['base_delta']:+,.0f} | "
                 f"USDT {p['quote_delta']:+,.2f} | vs hold ${p['vs_hold']:+,.2f} | " + " | ".join(parts))
