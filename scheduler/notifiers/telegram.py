"""Telegram notifier — default delivery path.

Sends heartbeats as the keeper's user account to a private bot. The bot then
forwards them to the AI's MCP client.

Reads credentials from environment (see .env.example at the repo root):

    TG_API_ID            — from https://my.telegram.org/apps
    TG_API_HASH          — same source
    TG_BOT_USERNAME      — the bot's @username (without the @)
    TG_USER_SESSION_PATH — where Telethon stores the user session file
                            (default: ./scheduler/session)
    TG_DELETE_DELAY_SECONDS — how long after sending to auto-delete; 0 disables

Optional proxy via HTTPS_PROXY / HTTP_PROXY env vars.
"""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from pathlib import Path


def _parse_proxy() -> tuple | None:
    """Convert HTTPS_PROXY / HTTP_PROXY env into a Telethon proxy tuple, or None."""
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not raw:
        return None
    try:
        import socks  # type: ignore
    except ImportError:
        return None
    parsed = urllib.parse.urlparse(raw)
    if not parsed.hostname:
        return None
    scheme = (parsed.scheme or "http").lower()
    proxy_type = socks.HTTP if scheme.startswith("http") else socks.SOCKS5
    port = parsed.port or (80 if scheme.startswith("http") else 1080)
    return (proxy_type, parsed.hostname, port)


class TelegramNotifier:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        ncfg = (cfg.get("notifier_config") or {}).get("telegram") or {}
        self.delete_delay = int(
            os.environ.get("TG_DELETE_DELAY_SECONDS",
                           ncfg.get("delete_delay_seconds", 300))
        )
        self.send_timeout = float(ncfg.get("send_timeout_seconds", 30))
        self.delete_timeout = float(ncfg.get("delete_timeout_seconds", 15))
        self.max_keepalive_fails = int(ncfg.get("max_keepalive_fails", 5))

        self.api_id_raw = os.environ.get("TG_API_ID", "").strip()
        self.api_hash = os.environ.get("TG_API_HASH", "").strip()
        self.bot_username = os.environ.get("TG_BOT_USERNAME", "").strip()
        self.session_path = os.environ.get("TG_USER_SESSION_PATH", "./scheduler/session").strip()

        self._client = None
        self._bot_entity = None
        self._keepalive_fails = 0

    async def setup(self) -> None:
        if not (self.api_id_raw and self.api_hash and self.bot_username):
            raise RuntimeError(
                "TelegramNotifier needs TG_API_ID, TG_API_HASH, TG_BOT_USERNAME "
                "in the environment (see .env.example)."
            )
        try:
            api_id = int(self.api_id_raw)
        except ValueError:
            raise RuntimeError(f"TG_API_ID is not a number: {self.api_id_raw!r}")

        # Lazy import — keep telethon optional for forks using a different notifier
        from telethon import TelegramClient

        proxy = _parse_proxy()
        session_file = str(Path(self.session_path).resolve())
        self._client = TelegramClient(
            session_file, api_id, self.api_hash,
            proxy=proxy,
            connection_retries=5,
            retry_delay=2,
            auto_reconnect=True,
            request_retries=3,
        )
        await self._client.start()
        self.log.info("Telethon client started")
        self._bot_entity = await self._client.get_entity(self.bot_username)
        self.log.info(f"Bot entity resolved: {self._bot_entity.id}")

    async def keepalive(self) -> None:
        if not self._client:
            return
        try:
            await asyncio.wait_for(self._client.get_me(), timeout=10)
            self._keepalive_fails = 0
        except Exception as e:
            self._keepalive_fails += 1
            self.log.warning(
                f"Keepalive failed ({self._keepalive_fails}/{self.max_keepalive_fails}): {e}"
            )
            if self._keepalive_fails >= self.max_keepalive_fails:
                self.log.warning("Too many keepalive failures, forcing full reconnect...")
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(10)
                await self._client.connect()
                await self._client.start()
                self._bot_entity = await self._client.get_entity(self.bot_username)
                self._keepalive_fails = 0
                self.log.info(f"Reconnected, bot entity: {self._bot_entity.id}")

    async def send_heartbeat(self, task_id: str, trigger: str, prompt: str) -> None:
        if not (self._client and self._bot_entity):
            raise RuntimeError("TelegramNotifier not set up — call setup() first.")

        msg_text = f"[♥{task_id}:{trigger}] {prompt}"

        if not self._client.is_connected():
            self.log.warning("Client disconnected, reconnecting...")
            await self._client.connect()

        msg = await asyncio.wait_for(
            self._client.send_message(self._bot_entity, msg_text),
            timeout=self.send_timeout,
        )
        self.log.info(f"Sent [{task_id}:{trigger}] msg_id={msg.id}")

        # Auto-delete after delay (fire-and-forget)
        if self.delete_delay > 0:
            asyncio.create_task(self._delete_later(msg, task_id))

    async def _delete_later(self, msg, task_id: str) -> None:
        try:
            await asyncio.sleep(self.delete_delay)
            await asyncio.wait_for(msg.delete(), timeout=self.delete_timeout)
            self.log.info(f"Deleted [{task_id}] msg_id={msg.id}")
        except Exception as e:
            self.log.warning(f"Delete failed [{task_id}]: {e}")

    async def teardown(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
