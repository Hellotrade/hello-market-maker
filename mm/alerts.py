"""Telegram alerts with de-duplication so a stuck condition doesn't spam."""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("mm.alerts")


class Alerter:
    def __init__(self, token_env: str, chat_env: str, dedupe_seconds: float, prefix: str = "HELLO MM"):
        self.token = os.environ.get(token_env, "")
        self.chat = os.environ.get(chat_env, "")
        self.dedupe = dedupe_seconds
        self.prefix = prefix
        self._last: dict[str, float] = {}
        self.sent: list[str] = []  # kept for tests and sim reports

    async def send(self, key: str, text: str, force: bool = False) -> None:
        now = time.time()
        if not force and key in self._last and now - self._last[key] < self.dedupe:
            return
        self._last[key] = now
        msg = f"[{self.prefix}] {text}"
        self.sent.append(msg)
        log.warning(msg)
        if not (self.token and self.chat):
            return
        try:
            import aiohttp

            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"chat_id": self.chat, "text": msg}, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:  # alerts must never crash the bot
            log.error("telegram send failed: %s", e)
