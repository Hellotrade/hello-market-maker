"""Risk controls: kill switch, drawdown halt, volatility handling."""
from __future__ import annotations

import os
from collections import deque
from typing import Optional

from .config import RiskCfg


class RiskManager:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg
        self.start_value: Optional[float] = None
        self.prices: deque[tuple[float, float]] = deque()

    def kill_requested(self) -> bool:
        return os.path.exists(self.cfg.kill_file)

    def set_start_value(self, value: float) -> None:
        if self.start_value is None and value > 0:
            self.start_value = value

    def drawdown_pct(self, value: float) -> float:
        if not self.start_value:
            return 0.0
        return max(0.0, (1 - value / self.start_value) * 100)

    def drawdown_breached(self, value: float) -> bool:
        return self.drawdown_pct(value) >= self.cfg.max_drawdown_pct

    def record_price(self, t: float, price: float) -> None:
        self.prices.append((t, price))
        while self.prices and self.prices[0][0] < t - self.cfg.vol_window_seconds:
            self.prices.popleft()

    def window_move_pct(self) -> float:
        if len(self.prices) < 2:
            return 0.0
        ps = [p for _, p in self.prices]
        return (max(ps) / min(ps) - 1) * 100

    def volatile(self) -> bool:
        return self.window_move_pct() >= self.cfg.vol_move_pct

    def halt_move(self) -> bool:
        return self.window_move_pct() >= self.cfg.halt_move_pct
