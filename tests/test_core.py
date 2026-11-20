import asyncio
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mm.config import Config, InventoryCfg, KpiCfg, QuotingCfg, load_config  # noqa: E402
from mm.inventory import inventory_skew  # noqa: E402
from mm.kpi import UptimeTracker, measure  # noqa: E402
from mm.models import DesiredOrder, OpenOrder, OrderBook  # noqa: E402
from mm.order_manager import diff_orders  # noqa: E402
from mm.pricing import external_book, reference_price  # noqa: E402
from mm.quoting import build_ladder, cap_to_balance  # noqa: E402

CFG_PATH = ROOT / "config" / "hello.yaml"


def ladder_book(orders, venue="x"):
    bids = sorted([(o.price, o.amount) for o in orders if o.side == "buy"], key=lambda x: -x[0])
    asks = sorted([(o.price, o.amount) for o in orders if o.side == "sell"], key=lambda x: x[0])
    return OrderBook(venue, bids, asks, 0)


@pytest.mark.parametrize("skew", [-1.0, -0.5, 0.0, 0.5, 1.0])
@pytest.mark.parametrize("wide", [False, True])
def test_ladder_meets_kpi_at_any_skew(skew, wide):
    q, k, inv = QuotingCfg(), KpiCfg(), InventoryCfg()
    orders = build_ladder(0.01, skew, q, k, inv, wide=wide)
    snap = measure(ladder_book(orders), [], k, "x", 0)
    assert snap.spread_ok, snap
    assert snap.bid_depth_usd >= k.min_depth_usd and snap.ask_depth_usd >= k.min_depth_usd, snap


def test_ladder_survives_losing_best_level():
    q, k, inv = QuotingCfg(), KpiCfg(), InventoryCfg()
    orders = build_ladder(0.01, 0.0, q, k, inv)
    without_best = [o for o in orders if o.level != 0]
    snap = measure(ladder_book(without_best), [], k, "x", 0)
    assert snap.depth_ok and snap.spread_ok


def test_skew_direction():
    assert inventory_skew(1_000_000, 0, 0.01, 0.5) == 1.0  # all tokens
    assert inventory_skew(0, 1000, 0.01, 0.5) == -1.0  # all USDT
    assert inventory_skew(100_000, 1000, 0.01, 0.5) == 0.0
    long_ladder = build_ladder(0.01, 1.0, QuotingCfg(), KpiCfg(), InventoryCfg())
    flat_ladder = build_ladder(0.01, 0.0, QuotingCfg(), KpiCfg(), InventoryCfg())
    best_ask = lambda L: min(o.price for o in L if o.side == "sell")  # noqa: E731
    assert best_ask(long_ladder) < best_ask(flat_ladder)  # long tokens -> sell cheaper


def test_cap_funds_inner_levels_first():
    orders = build_ladder(0.01, 0.0, QuotingCfg(), KpiCfg(), InventoryCfg())
    capped, short = cap_to_balance(orders, quote_budget=3000, base_budget=1e12)
    assert [s.side for s in short] == ["buy"]
    bids = [o for o in capped if o.side == "buy"]
    assert abs(sum(o.notional for o in bids) - 3000) < 1e-6
    assert max(o.level for o in bids) < QuotingCfg().levels  # outer levels dropped


def test_diff_keeps_close_orders():
    want = [DesiredOrder("buy", 0.0100, 1000, 0), DesiredOrder("sell", 0.0102, 1000, 0)]
    have = [OpenOrder("a", "buy", 0.010001, 990), OpenOrder("b", "sell", 0.0110, 1000)]
    cancel, place = diff_orders(want, have, 0.15, 25)
    assert [o.id for o in cancel] == ["b"]
    assert [d.side for d in place] == ["sell"]


def test_external_book_strips_own_orders():
    book = OrderBook("x", [(0.0099, 500), (0.0098, 100)], [(0.0101, 300)], 0)
    own = [OpenOrder("1", "buy", 0.0099, 500), OpenOrder("2", "sell", 0.0101, 100)]
    ext = external_book(book, own)
    assert ext.bids == [(0.0098, 100)]
    assert ext.asks == [(0.0101, 200)]


def test_reference_rejects_outlier():
    ref = reference_price({"a": 0.0100, "b": 0.0101, "c": 0.0130}, {}, outlier_pct=5)
    assert "c" in ref.rejected and 0.0100 <= ref.price <= 0.0101


def test_uptime_tracker():
    k = KpiCfg()
    up = UptimeTracker()
    good = build_ladder(0.01, 0, QuotingCfg(), k, InventoryCfg())
    for t in range(9):
        up.add(measure(ladder_book(good), [], k, "x", t))
    up.add(measure(None, [], k, "x", 9))
    assert up.uptime_pct("x") == pytest.approx(90.0)
    assert up.streak["x"] == 1


def test_config_rejects_spread_above_kpi():
    raw = load_config(CFG_PATH).model_dump()
    raw["quoting"]["wide_spread_pct"] = 2.5
    with pytest.raises(ValueError):
        Config.model_validate(raw)


@pytest.mark.parametrize("seed", [11, 23, 42])
def test_simulation_normal_market_meets_kpi(seed):
    """No price jumps: the bot must hold the KPI >= 95% of the time on every venue."""
    from mm.sim import SimParams, run_simulation

    cfg = copy.deepcopy(load_config(CFG_PATH))
    res = asyncio.run(run_simulation(cfg, hours=2, seed=seed, params=SimParams(jump_prob_per_hour=0)))
    assert not res["halted"]
    for v, up in res["uptime_pct"].items():
        assert up >= 95, (v, up)


def test_simulation_crash_raises_funding_alerts():
    """A sharp sell-off should drain USDT and the bot must say so rather than fail silently."""
    from mm.sim import SimParams, run_simulation

    cfg = copy.deepcopy(load_config(CFG_PATH))
    res = asyncio.run(run_simulation(cfg, hours=1, seed=3, params=SimParams(jump_prob_per_hour=6, jump_size_pct=5)))
    assert res["alerts"] > 0
    assert any("not enough" in a or "KPI failing" in a or "pulled" in a for a in res["alert_samples"])
