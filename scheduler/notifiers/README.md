# Notifiers

The scheduler delivers heartbeats through a pluggable notifier interface.
**Telegram is the default**, but it's one implementation path, not the only one.

## Why this is pluggable

The scheduler's job is to decide *when* to fire and *what to say*. Getting that
message to the AI is a separate problem with many reasonable solutions:

- Telegram → MCP client (default)
- Direct IPC to a local AI process
- HTTP webhook to a self-hosted AI service
- Email to a polling agent
- Signal / Matrix / Discord / IRC
- A file the AI polls

Pick the one that fits your deployment. The scheduler doesn't care.

## The contract

A notifier implements four async methods (see `Notifier` protocol in
`__init__.py`):

| Method | Called | Purpose |
|---|---|---|
| `setup()` | Once at start | Login, open connections |
| `keepalive()` | Every tick (~30s) | Liveness check; usually no-op |
| `send_heartbeat(task_id, trigger, prompt)` | On each fire | Deliver one heartbeat |
| `teardown()` | At shutdown | Clean up |

The implementation is also responsible for any transport-level side effects
(auto-delete after delay, retries, rate-limiting). The scheduler does not
track sent messages.

## Writing your own

Create `notifiers/yours.py`:

```python
import logging

class YourNotifier:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger

    async def setup(self) -> None:
        # open connection, login, etc.
        ...

    async def keepalive(self) -> None:
        # Optional. Usually no-op.
        return

    async def send_heartbeat(self, task_id: str, trigger: str, prompt: str) -> None:
        # Format and deliver however you want.
        ...

    async def teardown(self) -> None:
        return
```

Register it in `notifiers/__init__.py`:

```python
def _load_yours(cfg, logger):
    from .yours import YourNotifier
    return YourNotifier(cfg, logger)

_LOADERS["yours"] = _load_yours
```

Set the notifier in `scheduler_config.json`:

```json
{
  "notifier": "yours",
  "notifier_config": {
    "yours": { ... your config ... }
  }
}
```

That's it.

## Built-in notifiers

### `telegram`

Sends heartbeats as the keeper's user account to a private bot. The bot then
forwards them to the AI's MCP-capable client.

Requires Telegram credentials in the environment — see `.env.example` at the
repo root.

This is the default and what the original system runs on.

### `stdout`

Prints heartbeats to stdout. No external delivery. Use this for dev / testing
or as a starting point when wiring up your own transport.
