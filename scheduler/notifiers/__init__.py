"""Pluggable notifier interface for the scheduler.

The scheduler doesn't care HOW a heartbeat reaches the AI — Telegram, IPC
pipe, webhook, IRC, anything. It just delegates to a Notifier instance.

To add your own:

    1. Subclass `Notifier` in this package.
    2. Add a `load(cfg, logger)` factory entry in `_LOADERS` below.
    3. Set `"notifier": "<your_name>"` in scheduler_config.json.

See README.md in this directory for the contract and a walk-through.
"""
from __future__ import annotations

import logging
from typing import Protocol


class Notifier(Protocol):
    """Minimum surface a notifier must implement.

    Implementations are async. All four methods are called from the scheduler's
    single event loop. None of them should raise — wrap your own errors and
    let the scheduler log them.
    """

    async def setup(self) -> None:
        """One-time initialization (login, open connections, etc.)."""

    async def keepalive(self) -> None:
        """Called every tick (~30s). Useful for liveness checks. May be no-op."""

    async def send_heartbeat(self, task_id: str, trigger: str, prompt: str) -> None:
        """Deliver one heartbeat. Format and delivery are up to the implementation.

        Implementations are also responsible for any post-send concerns
        (auto-delete, retries, rate-limiting) internally — the scheduler
        doesn't track sent messages.
        """

    async def teardown(self) -> None:
        """Optional cleanup. Default implementations may leave empty."""


def load_notifier(cfg: dict, logger: logging.Logger | None = None) -> Notifier:
    """Factory. Picks an implementation by `cfg["notifier"]`.

    Defaults to "telegram" if not specified. Unknown names raise ValueError.
    """
    name = (cfg.get("notifier") or "telegram").strip().lower()
    loader = _LOADERS.get(name)
    if loader is None:
        raise ValueError(
            f"Unknown notifier '{name}'. Known: {sorted(_LOADERS)}. "
            f"Add a loader to scheduler/notifiers/__init__.py to plug in your own."
        )
    return loader(cfg, logger or logging.getLogger("scheduler"))


def _load_telegram(cfg: dict, logger: logging.Logger) -> Notifier:
    from .telegram import TelegramNotifier
    return TelegramNotifier(cfg, logger)


def _load_stdout(cfg: dict, logger: logging.Logger) -> Notifier:
    from .stdout import StdoutNotifier
    return StdoutNotifier(cfg, logger)


_LOADERS = {
    "telegram": _load_telegram,
    "stdout": _load_stdout,
}
