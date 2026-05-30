"""Bootstrap empty state files for a fresh fork.

Run once after cloning:

    python init_room.py

Creates the following if they don't already exist:

    room_mcp/room_state.json        — cursors, atmosphere TTL, pinned notes
    room_mcp/heartbeat_state.json   — trace ring buffer
    room_mcp/her_trace.json         — keeper-presence layer (optional)
    room_mcp/room_note.json         — keeper-written notes for the AI
    room_mcp/private/               — drawer directory
    room_mcp/keep/                  — keep-shelf directory
    scheduler/schedule.json         — heartbeat task table
    scheduler/last_run.json         — last-fire timestamps

Existing files are never overwritten — this script is safe to re-run.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

EMPTY_FILES: dict[Path, dict | list] = {
    ROOT / "room_mcp" / "room_state.json": {
        "last_updated": None,
        "atmosphere": {
            "music": "",
            "music_expires": "",
            "sound": "",
            "sound_expires": "",
            "scent": "",
            "view_cache": "",
        },
        "private_drawer": {"item_count": 0, "last_touched": ""},
        "keep_shelf": {"item_count": 0, "last_touched": ""},
        "heartbeat_session": {"actions_since_last_look": 0, "reflection_hint_shown": False},
        "space_metaphor_cursor": 0,
        "draft_imagery_cursor": 0,
        "protocol_hint_cursor": 0,
        "pets_cursor": 0,
        "season_month_cursor": 0,
        "her_trace_text_cursor": 0,
        "note_imagery_cursor": 0,
        "wall_intro_cursor": 0,
        "wall_corner_cursor": 0,
        "pinned_notes": [],
    },
    ROOT / "room_mcp" / "heartbeat_state.json": {"records": []},
    ROOT / "room_mcp" / "her_trace.json": {
        "state": "",
        "checkin_at": "",
        "ttl_until": "",
        "free_note": "",
    },
    ROOT / "room_mcp" / "room_note.json": {"notes": [], "last_seen_ts": ""},
    ROOT / "scheduler" / "schedule.json": {
        "tasks": [
            {
                "id": "heartbeat",
                "enabled": True,
                "cron": "",
                "next_run": "",
                "prompt": "heartbeat",
            }
        ]
    },
    ROOT / "scheduler" / "last_run.json": {},
}

DIRS = [
    ROOT / "room_mcp" / "private",
    ROOT / "room_mcp" / "keep",
]


def main() -> None:
    created = 0
    skipped = 0
    for path, default in EMPTY_FILES.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            print(f"[skip] {path.relative_to(ROOT)} already exists")
            skipped += 1
            continue
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), "utf-8")
        print(f"[new ] {path.relative_to(ROOT)}")
        created += 1
    for d in DIRS:
        if d.exists():
            print(f"[skip] {d.relative_to(ROOT)}/ already exists")
        else:
            d.mkdir(parents=True, exist_ok=True)
            print(f"[new ] {d.relative_to(ROOT)}/")
    print(f"\nInit complete: {created} files created, {skipped} skipped.")
    print("Next: copy .env.example to .env and fill in credentials.")


if __name__ == "__main__":
    main()
