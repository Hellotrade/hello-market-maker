# HELLO Market Maker

A basic market maker for HELLO/USDT on MEXC and Gate (Bybit ready when HELLO lists there).
Its job is to meet the exchange liquidity obligations around the clock:

- **Spread** of 2% or less between best bid and best ask
- **Depth** of at least $2,500 on each side within ±2% of the mid price
- **Uptime**: both of the above, at least 95% of the time

It also keeps the HELLO price the same on every exchange and earns the spread on the flow it handles.
This is deliberately the simple version. It does not try to predict price or support it.

## How it works

Every 5 seconds the bot:

1. **Fetches** the order book, last trade, our open orders and balances from each exchange.
2. **Prices**: removes our own orders from each book, takes each venue's price (mid of other traders' orders, or last trade), drops outliers and takes the weighted median. One reference price for all venues, which keeps prices aligned.
3. **Quotes** a ladder on each venue: 5 levels per side between 0.5% and 1.8% from the reference, together carrying $4,000 per side (1.6x the $2,500 KPI, so one fill can't break it), plus small extra orders at 3% and 5%.
4. **Leans against inventory**: holding too many tokens moves quotes down by up to 1% and sizes sales up, holding too much USDT does the opposite. Size never drops below the KPI minimum.
5. **Reconciles**: only replaces orders that moved more than 0.15% in price or 25% in size, cancels first, places the KPI levels first. All orders are post-only, so the bot never trades against itself.
6. **Measures** the KPIs exactly as the exchange does and keeps a rolling 24h uptime per venue (`data/kpi_log.csv`).
7. **Protects**: kill switch file, drawdown halt, wider spread in fast markets, pulls quotes on extreme moves or stale data, Telegram alerts for KPI failures and funding shortfalls.

Optional (off by default): cross-exchange arbitrage when other traders' prices drift apart by more than fees.

## Capital needed

Simulation (see below) shows each venue needs about **$12k USDT plus $12k of HELLO** to hold the KPI through normal volatility.
With half that, uptime fell to 36 to 90% whenever the price moved sharply.
For MEXC and Gate that is about **$48k in total**, using dedicated sub-accounts.

Expect to rebalance by hand, especially:
- in a rally the bot sells HELLO into the move and runs short of tokens on the ask side (top up from treasury);
- in a sell-off it buys and runs short of USDT on the bid side.
The bot alerts on Telegram as soon as either side can't be fully funded.

## Quick start

```bash
pip install -r requirements.txt

# 1. Check the current order books against the KPIs (no API keys needed)
python -m mm kpi

# 2. Offline simulation: synthetic market, full engine, no network
python -m mm sim --hours 24 --seed 7

# 3. Paper trading: real market data, virtual orders and balances
python -m mm run --mode paper

# 4. Live
cp .env.example .env    # add API keys and Telegram details
python -m mm run --mode live --confirm-live
```

Stop at any time with Ctrl+C (cancels all orders) or `touch KILL` (same, from another terminal).
Emergency: `python -m mm cancel-all --confirm-live`.

## Going live checklist

1. Apply for the MEXC and Gate market maker programs (zero or negative maker fees, higher API limits). Set `maker_fee: 0.0` in the config once active.
2. One dedicated sub-account per exchange, funded with the capital above.
3. API keys with **trade permission only, withdrawals disabled, IP whitelist** to the server.
4. Run on a cloud server (Tokyo or Singapore region), not a laptop.
5. Run `python -m mm kpi` to see the current state of the books, then a day of paper mode.
6. Go live with `depth_safety_factor` as configured and watch the Telegram alerts and `data/kpi_log.csv` for the first week.
7. Only then consider `arbitrage.enabled: true`.

## Configuration

Everything is in `config/hello.yaml` and commented. The key settings:

| Setting | Default | Meaning |
|---|---|---|
| `kpi.max_spread_pct` | 2.0 | exchange requirement |
| `kpi.min_depth_usd` | 2500 | per side, within `kpi.depth_band_pct` |
| `quoting.target_spread_pct` | 1.0 | what we normally quote |
| `quoting.wide_spread_pct` | 1.8 | in fast markets |
| `quoting.depth_safety_factor` | 1.6 | quote 1.6x the required depth |
| `inventory.max_price_skew_pct` | 1.0 | how hard quotes lean against inventory |
| `risk.max_drawdown_pct` | 20 | halt if portfolio value falls this much |

For SUPRA or any other pair, copy the config, change `symbol` and enable the venues where it trades.

## Simulation results

`mm/sim.py` models an unfriendly market: random walk with ~10% daily volatility and sudden 4% jumps, venues drifting apart, other traders quoting 4% wide, and a quarter of takers trading only when our quotes are stale.
With the default config (6-hour runs, 5 seeds):

| Scenario | Worst-venue KPI uptime |
|---|---|
| No jumps, recommended capital | 100% on all seeds |
| With jumps, recommended capital | 82 to 100% |
| With jumps, half the capital | 36 to 84% |

Profit versus simply holding the inventory ranged from about +$650 to -$2,600 per 6 hours.
It is positive in sideways markets and negative in strong trends, when the bot ends up on the wrong side of the move.
Treat a basic market maker as roughly break-even infrastructure that meets listing obligations, not a profit engine.
The simulation is synthetic. Paper mode on the real books is the next check.

## Rules this bot follows

- No wash trading or fake volume. Our bids and asks never cross, and all orders are post-only.
- Cross-venue arbitrage only trades against other people's orders.
- It doesn't buy to hold up the price. That belongs to a later phase and needs legal review first.

## Project layout

```
mm/
  config.py         settings and validation
  pricing.py        reference price, own-order stripping, outlier rejection
  quoting.py        KPI ladder and balance capping
  inventory.py      inventory skew
  order_manager.py  cancel/place diff
  kpi.py            spread, depth, uptime, CSV log
  risk.py           kill switch, drawdown, volatility
  arbitrage.py      cross-venue alignment
  exchange.py       ccxt live adapter and paper adapter
  engine.py         main loop
  sim.py            offline market simulator
  alerts.py         Telegram
tests/              unit tests and simulation tests (pytest)
```

## Roadmap

- Websocket order books (faster reaction, fewer API calls)
- PancakeSwap (BSC) price as an extra reference input
- Automatic rebalancing between exchanges
- Phase 2: smarter quoting using volume and liquidity signals
