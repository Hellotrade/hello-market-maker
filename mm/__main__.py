"""Command line entry point.

    python -m mm kpi     --config config/hello.yaml           # check live books, no keys needed
    python -m mm sim     --config config/hello.yaml --hours 24
    python -m mm run     --config config/hello.yaml --mode paper
    python -m mm run     --config config/hello.yaml --mode live --confirm-live
    python -m mm cancel-all --config config/hello.yaml --confirm-live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .alerts import Alerter
from .config import Config, load_config, load_dotenv, venue_credentials
from .engine import Engine
from .exchange import CcxtAdapter, PaperAdapter, PublicCcxtSource
from .kpi import measure


def build_adapters(cfg: Config, mode: str) -> dict:
    adapters = {}
    for v, vc in cfg.enabled_venues.items():
        if mode == "live":
            creds = venue_credentials(v)
            if not creds["apiKey"] or not creds["secret"]:
                sys.exit(f"missing {v.upper()}_API_KEY / {v.upper()}_API_SECRET in environment or .env")
            adapters[v] = CcxtAdapter(v, vc, creds)
        elif mode == "paper":
            adapters[v] = PaperAdapter(
                v, PublicCcxtSource(v, vc), vc.paper_base, vc.paper_quote,
                maker_fee=vc.maker_fee if vc.maker_fee is not None else 0.0,
                taker_fee=vc.taker_fee if vc.taker_fee is not None else 0.001,
            )
        else:
            raise ValueError(mode)
    return adapters


async def cmd_kpi(cfg: Config) -> None:
    import time

    for v, vc in cfg.enabled_venues.items():
        src = PublicCcxtSource(v, vc)
        try:
            await src.load()
            book = await src.fetch_book()
            s = measure(book, [], cfg.kpi, v, time.time())
            status = "OK  " if s.compliant else "FAIL"
            print(f"{status} {v:6s} {vc.symbol}: mid {s.mid:.6g}  spread {s.spread_pct:.2f}%  "
                  f"bid depth ${s.bid_depth_usd:,.0f}  ask depth ${s.ask_depth_usd:,.0f}  "
                  f"(need <= {cfg.kpi.max_spread_pct}% and >= ${cfg.kpi.min_depth_usd:,.0f} within +/-{cfg.kpi.depth_band_pct}%)")
        except Exception as e:
            print(f"ERR  {v:6s} {vc.symbol}: {e}")
        finally:
            await src.close()


async def cmd_run(cfg: Config, mode: str) -> None:
    adapters = build_adapters(cfg, mode)
    alerter = Alerter(cfg.alerts.telegram_bot_token_env, cfg.alerts.telegram_chat_id_env,
                      cfg.alerts.dedupe_seconds, prefix=f"{cfg.name} [{mode}]")
    engine = Engine(cfg, adapters, alerter)
    await alerter.send("startup", f"starting in {mode} mode on {', '.join(adapters)}", force=True)
    await engine.run()


async def cmd_cancel_all(cfg: Config) -> None:
    adapters = build_adapters(cfg, "live")
    for v, a in adapters.items():
        try:
            await a.load()
            await a.cancel_all()
            print(f"{v}: all {a.symbol} orders cancelled")
        except Exception as e:
            print(f"{v}: cancel failed: {e}")
        finally:
            await a.close()


async def cmd_sim(cfg: Config, hours: float, seed: int) -> None:
    from .sim import run_simulation

    res = await run_simulation(cfg, hours=hours, seed=seed)
    print(json.dumps(res, indent=2, default=lambda x: round(x, 4) if isinstance(x, float) else str(x)))


def main() -> None:
    ap = argparse.ArgumentParser(prog="mm", description="HELLO basic market maker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("kpi", "sim", "run", "cancel-all"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config/hello.yaml")
        p.add_argument("-v", "--verbose", action="store_true")
        if name == "run":
            p.add_argument("--mode", choices=["paper", "live"], default=None)
        if name in ("run", "cancel-all"):
            p.add_argument("--confirm-live", action="store_true", help="required for anything that touches real funds")
        if name == "sim":
            p.add_argument("--hours", type=float, default=6.0)
            p.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "sim" and not args.verbose:
        logging.getLogger("mm").setLevel(logging.ERROR)
    load_dotenv()
    cfg = load_config(args.config)

    if args.cmd == "kpi":
        asyncio.run(cmd_kpi(cfg))
    elif args.cmd == "sim":
        asyncio.run(cmd_sim(cfg, args.hours, args.seed))
    elif args.cmd == "cancel-all":
        if not args.confirm_live:
            sys.exit("cancel-all touches live accounts: add --confirm-live")
        asyncio.run(cmd_cancel_all(cfg))
    else:
        mode = args.mode or cfg.mode
        if mode == "sim":
            sys.exit("use `python -m mm sim` for simulation")
        if mode == "live" and not args.confirm_live:
            sys.exit("live mode trades real funds: add --confirm-live")
        asyncio.run(cmd_run(cfg, mode))


if __name__ == "__main__":
    main()
