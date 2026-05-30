"""Stdout notifier — prints heartbeats to stdout. Useful for dev and testing.

No external delivery. No credentials. Run this and you'll see each fire
printed to the terminal.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime


class StdoutNotifier:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        ncfg = (cfg.get("notifier_config") or {}).get("stdout") or {}
        self.include_timestamp = bool(ncfg.get("include_timestamp", True))

    async def setup(self) -> None:
        self.log.info("StdoutNotifier ready (no external delivery; heartbeats go to stdout)")

    async def keepalive(self) -> None:
        return

    async def send_heartbeat(self, task_id: str, trigger: str, prompt: str) -> None:
        prefix = ""
        if self.include_timestamp:
            prefix = datetime.now().isoformat(timespec="seconds") + " "
        line = f"{prefix}[♥{task_id}:{trigger}] {prompt}\n"
        # Robust on terminals whose default codec can't encode the heart glyph.
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except UnicodeEncodeError:
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write(line.encode("utf-8", errors="replace"))
                buf.flush()
            else:
                sys.stdout.write(line.encode("ascii", errors="replace").decode("ascii"))
                sys.stdout.flush()

    async def teardown(self) -> None:
        return
