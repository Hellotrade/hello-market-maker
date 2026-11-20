"""Configuration loading and validation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class VenueCfg(BaseModel):
    enabled: bool = True
    ccxt_id: str
    symbol: str = "HELLO/USDT"
    weight: float = 1.0  # weight in the cross-venue reference price
    maker_fee: Optional[float] = None  # override exchange-reported fee (fraction, 0.0 = MM program)
    taker_fee: Optional[float] = None
    # Starting balances used only in paper and sim mode
    paper_base: float = 0.0
    paper_quote: float = 0.0


class KpiCfg(BaseModel):
    """What the exchange requires from us."""

    max_spread_pct: float = 2.0  # best ask vs best bid, as % of mid
    depth_band_pct: float = 2.0  # depth is measured within +/- this % of mid
    min_depth_usd: float = 2500.0  # required USDT value on EACH side inside the band
    uptime_target_pct: float = 95.0


class QuotingCfg(BaseModel):
    target_spread_pct: float = 1.0  # normal spread we quote (well inside the max)
    wide_spread_pct: float = 1.8  # used in volatile markets, still inside the max
    levels: int = 5  # order levels per side inside the KPI band
    depth_safety_factor: float = 1.6  # quote 1.6x the required depth so one fill doesn't break KPI
    band_margin: float = 0.9  # keep inner levels within 90% of the band
    outer_offsets_pct: list[float] = Field(default_factory=lambda: [3.0, 5.0])
    outer_level_usd: float = 400.0
    price_tolerance_pct: float = 0.15  # don't replace an order unless price moved more than this
    size_tolerance_pct: float = 25.0
    post_only: bool = True


class InventoryCfg(BaseModel):
    target_base_ratio: float = 0.5  # share of venue value we want held in tokens
    max_price_skew_pct: float = 1.0  # max shift of our quote centre when inventory is lopsided
    size_skew: float = 0.5  # how strongly sizes lean against inventory (0 = off)


class RiskCfg(BaseModel):
    max_drawdown_pct: float = 20.0  # halt if portfolio value (in USDT) falls this much from start
    vol_window_seconds: float = 60.0
    vol_move_pct: float = 6.0  # price move inside the window that switches to wide spread
    halt_move_pct: float = 25.0  # move inside the window that pulls all quotes
    stale_seconds: float = 30.0
    outlier_pct: float = 5.0  # ignore a venue whose price is this far from the median
    quote_reserve: float = 50.0  # USDT left untouched per venue
    base_reserve_usd: float = 50.0  # token value left untouched per venue
    kill_file: str = "KILL"  # create this file to cancel everything and stop
    noncompliant_alert_ticks: int = 6


class ArbCfg(BaseModel):
    enabled: bool = False
    min_edge_pct: float = 0.6  # net of taker fees on both legs
    max_trade_usd: float = 250.0
    cooldown_seconds: float = 30.0


class AlertCfg(BaseModel):
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    dedupe_seconds: float = 600.0


class Config(BaseModel):
    name: str = "HELLO market maker"
    mode: Literal["sim", "paper", "live"] = "paper"
    refresh_seconds: float = 5.0
    balance_every_ticks: int = 3
    manual_reference_price: Optional[float] = None  # used only if no venue gives a price
    data_dir: str = "data"
    venues: dict[str, VenueCfg]
    kpi: KpiCfg = KpiCfg()
    quoting: QuotingCfg = QuotingCfg()
    inventory: InventoryCfg = InventoryCfg()
    risk: RiskCfg = RiskCfg()
    arbitrage: ArbCfg = ArbCfg()
    alerts: AlertCfg = AlertCfg()

    @model_validator(mode="after")
    def _check(self) -> "Config":
        q, k = self.quoting, self.kpi
        if not 0 < q.target_spread_pct <= q.wide_spread_pct < k.max_spread_pct:
            raise ValueError("need 0 < target_spread_pct <= wide_spread_pct < kpi.max_spread_pct")
        if q.wide_spread_pct / 2 >= k.depth_band_pct * q.band_margin:
            raise ValueError("half of wide_spread_pct must sit inside the depth band")
        if q.levels < 1:
            raise ValueError("quoting.levels must be >= 1")
        if q.depth_safety_factor < 1.05:
            raise ValueError("depth_safety_factor must be >= 1.05")
        if not any(v.enabled for v in self.venues.values()):
            raise ValueError("enable at least one venue")
        return self

    @property
    def enabled_venues(self) -> dict[str, VenueCfg]:
        return {k: v for k, v in self.venues.items() if v.enabled}


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        return Config.model_validate(yaml.safe_load(f))


def venue_credentials(venue: str) -> dict[str, str]:
    """Reads MEXC_API_KEY / MEXC_API_SECRET / MEXC_API_PASSWORD style env vars."""
    prefix = venue.upper()
    creds = {
        "apiKey": os.environ.get(f"{prefix}_API_KEY", ""),
        "secret": os.environ.get(f"{prefix}_API_SECRET", ""),
    }
    pw = os.environ.get(f"{prefix}_API_PASSWORD")
    if pw:
        creds["password"] = pw
    return creds
