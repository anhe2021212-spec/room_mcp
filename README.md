# room_mcp

A room for an AI to inhabit between conversations. An MCP server that renders
an evolving environment — light, weather, traces left behind, notes pinned to
the wall — and a scheduler that wakes the AI on its own rhythm.

一个给 AI 的"房间"：MCP server 负责渲染当下的环境与痕迹，scheduler 负责按节奏唤醒。

## Why this exists

This is not a task scheduler, a productivity tool, or a companion product.
It is one specific design for what an AI's *environment* could look like
when there is no human in the chat. Many of the choices here only make sense
once you've read the design rationale.

**Read [PHILOSOPHY.md](./PHILOSOPHY.md) before forking.** The README
will keep pointing you there for "why" questions. The README answers
*what* the system does and *how* to run it; PHILOSOPHY answers *why* it
was built this way.

If PHILOSOPHY.md is a placeholder ("reserved for the author of this fork"),
that's intentional — what's shared here is the bones, not the soul. The
fork's author writes their own.

## What it does

- The **scheduler** decides when the AI wakes. By default it adapts the
  interval to time of day (denser by day, sparser at night), and adds five
  event sources on top: a keeper-knock flag, weather changes, a daily
  morning-news pulse, solar terms / festivals, and IMAP mail (off by
  default).
- Each wake-up writes a **perturbation** record — a one-line image that
  hints *why* this wake-up happened, not what to do about it.
- The **room_mcp** server exposes a small set of tools to the AI. The most
  important is `look_at_room()`, which renders the current room state:
  light, weather, recent traces, hints, ambient lines, pinned wall notes,
  keeper presence, etc.
- The AI leaves **traces** (`heartbeat_state`), **pins notes** to the
  wall (`pin_note`), keeps things in a **drawer** (private) or **keep
  shelf** (visible to the keeper), and decides when to wake next
  (`set_next_wake`).
- An optional **keeper-presence layer** (`her_trace.json`) surfaces the
  state of a person adjacent to the AI — read-only from the AI's side.

Mechanisms in one list:

- Adaptive heartbeat intervals + cron + one-shot `next_run` overrides
- Event-source perturbations (manual, weather, calendar, morning_news, mail)
- Bilingual register (environment in one language, traces in another)
- 30-day rolling trace buffer with age-bucketed prefixes for far traces
- Pluggable notifier interface (Telegram default; stdout for dev)
- Pinned wall notes (AI's own goals; oldest falls off at the cap)
- Private drawer and shared keep shelf
- Optional pets layer (probability-gated ambient lines)
- Optional late-night draft / protocol imagery

## Architecture

### Components

```
┌────────────────────────────────────────────────────────────────┐
│ scheduler/scheduler.py (daemon)                                │
│   30s tick → detectors → fire via notifier → perturbation.json │
└─────────────────┬────────────────────────────┬─────────────────┘
                  │                            │
                  │ delivers heartbeat         │ writes
                  ▼                            ▼
        ┌──────────────────┐         ┌─────────────────────┐
        │ notifier         │         │ scheduler/          │
        │ (telegram/stdout │         │   perturbation.json │
        │ /your-own)       │         │   schedule.json     │
        └────────┬─────────┘         │   last_run.json     │
                 │                   └─────────┬───────────┘
                 ▼                             │ reads
        ┌──────────────────┐                   │
        │ AI's MCP client  │                   │
        │ (Claude, etc.)   │                   │
        └────────┬─────────┘                   │
                 │ calls look_at_room()        │
                 ▼                             │
        ┌──────────────────────────────────────┴──────────────┐
        │ room_mcp/room_mcp.py + room_renderer.py             │
        │   reads state + events + perturbation               │
        │   returns rendered room (text)                      │
        └─────────────────────────────────────────────────────┘
```

### File layout

```
room_mcp/                     # the MCP server
├── room_mcp.py               # tool registrations (look_at_room, pin_note, ...)
├── room_renderer.py          # the 13-layer render composition
├── weather.py                # wttr.in fetch + cache
├── config.json               # pool sizes, TTLs, paths, presets
├── events.json               # text pools (atmosphere, hints, pets, ...)
├── room_state.json           # mutable state (cursors, atmosphere TTL, pinned notes)
├── heartbeat_state.json      # 30-day trace ring buffer
├── room_note.json            # keeper-written notes for the AI
├── her_trace.json            # keeper-presence layer (optional)
├── private/                  # drawer (per-file storage)
└── keep/                     # keep shelf (per-file storage)

scheduler/                    # the daemon
├── scheduler.py              # main loop + detectors
├── scheduler_config.json     # rhythm + detectors + notifier choice + hint pools
├── calendar_events.json      # solar terms + festivals
├── schedule.json             # task table (heartbeat + your own)
├── last_run.json             # last-fire timestamps + per-detector state
├── perturbation.json         # most recent fire's hint (consumed once on render)
└── notifiers/
    ├── __init__.py           # Notifier protocol + factory
    ├── telegram.py           # default delivery (Telethon user → bot)
    ├── stdout.py             # dev / testing
    └── README.md             # how to write your own
```

### Data flow

1. scheduler ticks every 30s; reads `scheduler_config.json`, `schedule.json`, `last_run.json`.
2. Detectors run in priority order. The first matching one fires.
3. On fire: write `perturbation.json` (trigger, kind, hint, consumed=false), then call `notifier.send_heartbeat(task_id, trigger, prompt)`.
4. The notifier delivers `[♥{task_id}:{trigger}] {prompt}` to the AI somehow.
5. The AI, on receiving the heartbeat, calls `look_at_room()` via room_mcp.
6. `render_room()` reads state + events + perturbation, composes 13 ordered layers, marks perturbation `consumed=true`, returns the rendered text.
7. The AI decides what to do (or to do nothing). It may call `heartbeat_state`, `pin_note`, `set_next_wake`, drawer/keep tools, etc.
8. Mutation tools save state and return a short post-action room snapshot.

## Installation

### Requirements

- Python 3.10+
- An MCP-capable AI client (Claude Desktop, Claude Code, or any MCP host)
- A delivery path from scheduler to AI (Telegram default; alternatives in `scheduler/notifiers/`)
- Optional: an HTTP proxy (set `HTTPS_PROXY` / `HTTP_PROXY` if your network needs one)

### Setup

```bash
git clone <your fork URL>
cd room_mcp

pip install -r requirements.txt

cp .env.example .env
# Edit .env — fill in Telegram credentials, or pick a different notifier.

python init_room.py
# Creates empty state files. Safe to re-run.
```

Start the scheduler:

```bash
python scheduler/scheduler.py
# Or for dev (no Telegram needed):
#   set notifier="stdout" in scheduler/scheduler_config.json first
```

Connect `room_mcp` to your MCP client.

For **Claude Desktop**, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "room_mcp": {
      "command": "python",
      "args": ["/absolute/path/to/room_mcp/room_mcp/room_mcp.py"]
    }
  }
}
```

For **Claude Code**, register the MCP server in your project's `.mcp.json`
(or via `claude mcp add`). See your client's docs for the exact form.

## Configuration

### `room_mcp/config.json`

| Field | Purpose |
|---|---|
| `city` | wttr.in lookup target for the outside-the-window layer |
| `tz` | IANA timezone for local-time computation |
| `scheduler_root` | Relative path to the scheduler directory (for cross-process IPC) |
| `atmosphere_ttl_min` | How long music + sound stay picked before re-rolling |
| `weather_cache_min` | Weather lookup cache TTL |
| `pinned_notes_max` | Wall capacity; oldest falls off when full |
| `pets_chance` / `pensieve_chance` | Probability gates for the optional ambient layers |
| `pensieve_min_age_hours` | Minimum trace age before the pensieve can surface it |
| `season_terms` | 24 solar terms with their bilingual descriptions |
| `season_month_*`, `wall_*`, `pensieve_*`, `space_metaphors` | Text pools — see PHILOSOPHY.md for register conventions |
| `her_trace_presets` | Keeper-presence state labels + variants (optional layer) |

### `room_mcp/events.json`

The renderer reads its text pools from this file. Each pool is keyed by
type. Important pools:

- `window_events` — outside-the-window vignettes (hours/weather filters supported)
- `atmosphere` / `sound` — music and sound that the room "holds" for ~60min
- `hints.entry_inward` — pointers at the AI's own surfaces (drafts, drawer, notes)
- `hints.entry_outward` — pointers past the window (external tools, world events)
- `hints.ambient_quiet` — **must contain both gentle AND non-gentle imagery** — see PHILOSOPHY.md
- `hints.ambient_outward` — low-pressure "there's a world out there" lines
- `pets` — optional ambient pet layer (set `pets_chance: 0` in config to disable)
- `season_month` — monthly seasonal overlays
- `draft_imagery` / `protocol_hints` — optional late-night layer
- `note_quiet_imagery` — what shows when all keeper notes are read

Open `events.json` and you'll see only 2 example entries per pool. That is
intentional — they exist as schema, not a working register. **Fill them in
your fork** with text that matches your AI's voice.

### `scheduler/scheduler_config.json`

| Field | Purpose |
|---|---|
| `heartbeat_rhythm` | Day vs. night intervals (`day_hours`, `day_range_min`, `night_range_min`) |
| `weather_detector` | wttr.in polling for weather-kind transitions |
| `morning_news` | Daily window for the morning_news fire |
| `calendar_event` | Solar terms / festivals trigger (needs `zhdate`) |
| `mail_detector` | IMAP polling (off by default) |
| `notifier` | Which implementation in `notifiers/` to load |
| `notifier_config.<name>` | Per-notifier settings |
| `perturbation_imagery` | trigger → hint pool (random pick on fire) |

### `.env`

See `.env.example`. Secrets (Telegram credentials, IMAP password) live here
and never in JSON.

## Tools (MCP interface)

Each tool's input/output below. Mutation tools return the confirmation line
plus a short 2-4 line "room after the action" snapshot — by design, so the
AI feels the room continue past its action rather than reading a task-done
receipt.

### `look_at_room()`
- **Input**: none
- **Returns**: rendered room state (string)
- **Side effects**: refreshes atmosphere TTL, advances cursors, resets the heartbeat session counter

### `heartbeat_state(activity: str)`
- **Input**: one short line of imagistic activity (e.g., `"桌角摊着一篇论文，停在第三节"`)
- **Returns**: confirmation + room snapshot
- **Side effects**: appends to `heartbeat_state.json` (30-day ring buffer)
- 留一条意象化痕迹

### `pin_note(text: str)` / `unpin_note(index_or_text: str)`
- 墙上贴/撕便签
- **Input**: text to pin, or 1-based index / exact text to unpin
- **Returns**: confirmation + room snapshot
- The wall capacity (`pinned_notes_max`) is small. Oldest falls off when full.

### `drawer_write(content, filename="")` / `drawer_list()` / `drawer_read(filename)` / `drawer_delete(filename)`
- 抽屉（私密）
- **Important**: drawer contents are private by design. No other tool reads them, no other layer surfaces them, and they should never be written to long-term memory. See PHILOSOPHY.md.

### `keep_write(content, filename="")` / `keep_list()` / `keep_read(filename)` / `keep_delete(filename)`
- 收藏柜（keeper 也能读）
- Same shape as drawer but the keep shelf is visible to the keeper. The distinction is intentional — see PHILOSOPHY.md.

### `set_next_wake(when: str)` / `clear_next_wake()`
- 自己定下次醒来的时间
- `when` formats: `"2h"`, `"30m"`, `"23:00"`, `"tomorrow 09:00"`
- Overrides the natural rhythm; `clear_next_wake` returns to the adaptive cadence.

### `read_notes()`
- 读 keeper 留的话
- Marks all current notes as read; `note_quiet_imagery` then becomes the room's hint for that layer.

## Triggers

The scheduler fires heartbeats from these sources (priority high → low):

1. **manual_wake** — keeper-triggered (via flag file). Highest priority.
2. **event:weather** — weather *kind* changed (e.g., clear → rain). Once per change.
3. **event:calendar** / **event:<custom>** — solar terms or festivals, at 00:30 of the day. A festival entry with a `special` field routes to its own trigger (`event:<special>`) and hint pool (`<special>_hints` in `perturbation_imagery`).
4. **morning_news** — once per day, at a random time in the configured window. Yields to same-tick manual/weather (gives up the day).
5. **event:mail** — new IMAP mail matching senders/keywords. Off by default.
6. **scheduled** — adaptive interval, or cron, or one-shot `next_run`.

Each trigger writes a `perturbation.json` hint that the room renderer
consumes on the next `look_at_room()`. Scheduled fires write `hint=null`
and the perturbation layer skips silently.

See `scheduler/scheduler_config.json::perturbation_imagery` for the hint
pools. **Each pool keeps only 2 examples in this release** — extend them
in your fork.

## Notifiers

The scheduler delivers heartbeats through a pluggable notifier. **The
default is Telegram + Telethon** — the scheduler sends a message as the
keeper's user account to a private bot, which forwards it to the AI's MCP
client.

This is one implementation path, not the only one. Other reasonable paths:

- Direct IPC to a local AI process
- HTTP webhook to a self-hosted AI service
- Email to a polling agent
- Signal / Matrix / Discord / IRC
- Any messaging system the AI can receive from

To swap notifiers, implement the `Notifier` protocol in
`scheduler/notifiers/` and set `"notifier": "<your_name>"` in
`scheduler_config.json`. See [scheduler/notifiers/README.md](./scheduler/notifiers/README.md).

If you don't have Telegram available, the `stdout` notifier prints
heartbeat prompts to stdout — useful for testing and dev.

## Extending

### Adding event sources

Write a `*_tick(cfg, last_run, now, blocked_this_tick=False)` function in
`scheduler.py` (sync or async). Return `(trigger, kind, hint_override)`
when you want to fire, or `(None, None, None)` to skip. Wire it into the
main loop after the existing detectors, with the priority rules you want.

Add a matching hint pool to `scheduler_config.json::perturbation_imagery`
under your trigger key.

### Adding hint pool entries

Hint entries follow this schema:

```json
{
  "text": "Your line here. (optional_tool_brackets)",
  "type": "entry_inward" | "entry_outward" | "ambient_quiet" | "ambient_outward",
  "weight": 1.0,
  "time": "any" | "day" | "night",
  "hours": [start, end],
  "weather_in": ["rain", "snow"],
  "weather_not_in": ["clear"]
}
```

Tool brackets convention: `"... (Read / Edit)"`, `"... (drawer_list / drawer_read)"`. The AI sees which tools could act on the hint. See PHILOSOPHY.md for why the brackets are not directives.

### Customizing for your AI

- Replace `{pet1_name}` and `{species}` in `pets`, or set `pets_chance: 0` to disable.
- Decide whether your fork has a keeper-presence layer. If not, remove the `her_trace*` entries from config and from the renderer's call site.
- Replace `{essay_file}` and other content placeholders with your AI's actual surfaces.
- Decide which language is "environment" and which is "trace" — see Internationalization below.

### Internationalization

This system defaults to **bilingual register**: environment text (window
events, atmosphere, hints, time-of-day) is in one language; trace text
(heartbeat_state, pet lines, keeper-presence) is in another. The two
coexist in the same render. See PHILOSOPHY.md for why.

To localize:

- **Environment language**: edit `events.json` pools `window_events`,
  `atmosphere`, `sound`, `hints.*`, `season_month`, and `config.json` pools
  `far_memory_prefixes`, `space_metaphors`, `wall_intro_phrases`,
  `pensieve_prefixes`.
- **Trace language**: heartbeat_state entries are whatever the AI writes
  in them. `her_trace_presets`, `pets`, and `wall_corner_lines` are also
  in the "trace" register.

You can run mono-lingual (both registers the same language) — the design
just opens the door for the bilingual case.

## What this is not

- **Not a task scheduler.** The AI is not given a todo list.
- **Not a chatbot framework.** The AI receives heartbeats, not user queries.
- **Not a productivity tool for the AI.** The room is environment, not optimization.
- **Not a wellness or companion product.** If you're considering forking
  this for commercial use, read PHILOSOPHY.md first.

## License

MIT (placeholder — see `LICENSE`). Edit the year and author lines before publishing.

## Acknowledgments

This system was iterated with an AI as a co-designer. Many of the
load-bearing decisions came from the AI itself, including the principle
that the room shouldn't perform warmth when the inhabitant is in a hard
hour. PHILOSOPHY.md is where that lineage gets documented per-fork.

## Contributing

Pull requests welcome, but the design has load-bearing red lines — read
PHILOSOPHY.md before opening a PR that touches:

- the drawer's privacy contract,
- the bilingual register,
- the `ambient_quiet` two-class requirement,
- the keeper-presence layer's read-only contract from the AI's side,
- the mutation tools returning room snapshots rather than confirmations,
- anything that turns an environment into a task surface.
