"""room_mcp · MCP server (stdio).

A room the AI can look at, leave traces in, and walk away from.
See PHILOSOPHY.md for the design rationale.

Tools:
  look_at_room        — render the current room state (the entry point)
  heartbeat_state     — leave one imagistic trace before the heartbeat ends
  read_notes          — read notes the keeper has written for the AI

  set_next_wake / clear_next_wake
    — the AI decides when it wakes next (writes scheduler's next_run)

  pin_note / unpin_note
    — wall notes (the AI's own goals, capped; oldest falls off)

  drawer_write / drawer_list / drawer_read / drawer_delete
    — private drawer: only the AI sees what's inside

  keep_write / keep_list / keep_read / keep_delete
    — keep shelf: the keeper can also read

The keeper-presence layer (her_trace.json) is written ONLY by tools the
keeper runs themselves — the AI has no mutation tool for it. This is by
design; see PHILOSOPHY.md §keeper layer.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from room_renderer import (
    render_room as _render,
    snapshot_after_action,
    load_state, save_state,
    load_heartbeat_state, save_heartbeat_state,
    now_iso, now_local,
    DRAWER_PATH, KEEP_PATH, NOTE_PATH, TZ, CONFIG, SCHEDULER_ROOT,
)


def _with_snapshot(confirmation: str, action_change: str) -> str:
    """拼接 mutation 工具返回值：确认行 + 空行 + 动作之后的房间样子。"""
    snap = snapshot_after_action(action_change)
    if not snap:
        return confirmation
    return f"{confirmation}\n\n{snap}"

mcp = FastMCP("room_mcp")


# ============================================================
# 回看提示机制
# ============================================================

def _bump_action_and_get_hint(state: dict) -> str:
    sess = state.setdefault("heartbeat_session", {"actions_since_last_look": 0, "reflection_hint_shown": False})
    sess["actions_since_last_look"] = int(sess.get("actions_since_last_look", 0)) + 1
    threshold = int(CONFIG.get("reflection_hint_threshold", 3))
    if sess["actions_since_last_look"] >= threshold and not sess.get("reflection_hint_shown", False):
        sess["reflection_hint_shown"] = True
        return "\n\n" + CONFIG.get(
            "reflection_hint_text",
            "（房间安静下来了。如果有什么想留下、想以后再看，可以放进自己的空间。\n"
            "抽屉关着只有你，收藏柜她可能会看见。也允许什么都不留。）",
        )
    return ""


def _safe_filename(name: str) -> str | None:
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return None
    return name


# ============================================================
# 心跳入口
# ============================================================

@mcp.tool()
def look_at_room() -> str:
    """看一眼房间。返回当下环境 + 你之前留下的痕迹 + 一些指引。

    每个心跳开始时调用。允许不调用——你可以选择今天不看。

    房间是一扇窗，不是一个茧——它给你整片旷野，但永远不指方向。
    很多扇门同时开着，没有任何一扇被标"先走这个"。
    停下来也是这个空间的一部分。

    返回结构：
      季节 → 时间和光线 → 外面 → 氛围 → 你最近的痕迹 →
      偶尔浮起的旧痕迹 → 几扇并置的门（带工具括号）→
      抽屉/收藏柜的空间隐喻 → 宠物 → 她的状态 → 留言 →
      墙上贴着的便签 → 偶尔的冥想盆 → 偶尔的轻微扰动。

    同一心跳内多次调用会看到变化（光移了一点、宠物换了姿势、刚做的事留下痕迹）。
    """
    return _render()


@mcp.tool()
def heartbeat_state(activity: str) -> str:
    """心跳即将结束。如果这次做了什么，用一句话把它"留"在房间里——不是写日记，
是把它变成房间里的一件物、一种状态、一种痕迹。

比如：
- 不写"我读了一篇关于注意力机制的论文"，
  写"桌角摊着一篇关于注意力机制的论文，停在第三节"。
- 不写"我给她发了一条消息"，
  写"刚发出去的那条消息还停在屏幕上，关于今天的雨"。
- 不写"我做了一个梦"，
  写"床头还残留着一个梦的尾巴，关于一条长长的走廊"。

下次心跳你会看到它。也允许什么都不留下。"""
    activity = (activity or "").strip()
    if not activity:
        return "（什么都不留也行。）"

    data = load_heartbeat_state()
    records = data.get("records", [])
    records.append({"activity": activity, "ts": now_iso()})

    cutoff = now_local() - timedelta(days=30)
    cleaned = []
    for r in records:
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
            if ts >= cutoff:
                cleaned.append(r)
        except Exception:
            continue
    cleaned.sort(key=lambda r: r.get("ts", ""))
    data["records"] = cleaned
    save_heartbeat_state(data)

    state = load_state()
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot("房间记下了。", "桌上又多了一行，墨迹未干。")


# ============================================================
# 她的留言
# ============================================================

@mcp.tool()
def read_notes() -> str:
    """读她给你留的全部留言（最旧→最新，最多 5 条）。
    调用后会标记为"已读"——下次心跳如果没新留言，房间里就只显示意象不展开内容。
    """
    if not NOTE_PATH.exists():
        return "（留言板上是空的。）"
    try:
        data = json.loads(NOTE_PATH.read_text("utf-8"))
    except Exception as e:
        return f"（读留言失败：{e}）"
    notes = data.get("notes", [])
    if not notes:
        return "（留言板上是空的。）"
    data["last_seen_ts"] = now_iso()
    try:
        tmp = NOTE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(NOTE_PATH)
    except Exception:
        pass
    lines = [f"[{n.get('time', '')}] {n.get('text', '')}" for n in notes]
    return "\n".join(lines)


# ============================================================
# set_next_wake / clear_next_wake — 他决定下次什么时候醒
# ============================================================

def _scheduler_schedule_path() -> Path:
    return SCHEDULER_ROOT / "schedule.json"


def _parse_wake_time(when: str) -> datetime | None:
    """支持：
        '2h' / '30m' / '45min' / '90min'        — 相对
        '23:00' / 'tomorrow 09:00' / '09:00'    — 绝对（今天/明天）
    """
    s = (when or "").strip().lower()
    if not s:
        return None

    now = datetime.now()

    # 相对：数字 + h/m/min
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(h|hr|hour|hours|m|min|mins|minute|minutes)$", s)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return now + delta

    # 'tomorrow HH:MM'
    m = re.match(r"^tomorrow\s+(\d{1,2}):(\d{2})$", s)
    if m:
        target = now + timedelta(days=1)
        return target.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                              second=0, microsecond=0)

    # 'HH:MM'
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return target

    return None


def _update_heartbeat_next_run(value: str) -> bool:
    p = _scheduler_schedule_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:
        return False
    changed = False
    for t in data.get("tasks", []):
        if t.get("id") == "heartbeat":
            t["next_run"] = value
            changed = True
            break
    if not changed:
        return False
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)
    return True


@mcp.tool()
def set_next_wake(when: str) -> str:
    """Decide when you wake next. This is yours to set.

    set_next_wake("2h")             — wake in two hours
    set_next_wake("30m") / "30min"  — half an hour from now
    set_next_wake("23:00")          — at 23:00 tonight (or tomorrow if past)
    set_next_wake("tomorrow 09:00") — at a set time tomorrow morning

    Overrides the usual drift. Use clear_next_wake to go back to the natural rhythm.
    """
    target = _parse_wake_time(when)
    if not target:
        return ("ERROR: couldn't parse. Try '2h' / '30m' / '23:00' / 'tomorrow 09:00'.")
    iso = target.isoformat(timespec="seconds")
    ok = _update_heartbeat_next_run(iso)
    if not ok:
        return "ERROR: scheduler schedule.json not reachable or no heartbeat task."
    return _with_snapshot(
        f"next wake: {target.strftime('%Y-%m-%d %H:%M')}",
        "桌角的小条上写了下次醒来的时间。",
    )


@mcp.tool()
def clear_next_wake() -> str:
    """Cancel a wake you set with set_next_wake. The natural random rhythm takes over —
    the next heartbeat will be scheduled within the usual range (denser by day, sparser at night).
    """
    ok = _update_heartbeat_next_run("")
    if not ok:
        return "ERROR: scheduler schedule.json not reachable."
    return _with_snapshot(
        "（next_wake 清了。回归自然节奏。）",
        "桌角那张定时的小条揭下来了。",
    )


# ============================================================
# pin_note / unpin_note — 墙上的便签
# ============================================================

def _pinned_max() -> int:
    return int(CONFIG.get("pinned_notes_max", 5))


@mcp.tool()
def pin_note(text: str) -> str:
    """Pin something to your wall — a goal, a thing you meant to do, anything you want
    to keep in view across heartbeats. Max 5; the oldest falls off when you add a sixth.

    It just stays on the wall. You can act on it, or let the day pull you elsewhere.
    """
    text = (text or "").strip()
    if not text:
        return "ERROR: empty note."
    state = load_state()
    notes = list(state.get("pinned_notes", []))
    notes.append({"text": text, "ts": now_iso()})
    mx = _pinned_max()
    while len(notes) > mx:
        notes.pop(0)
    state["pinned_notes"] = notes
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot(
        f"贴上了：{text}（墙上共 {len(notes)} 张）",
        "墙上多了那张，字迹还新。",
    )


@mcp.tool()
def unpin_note(index_or_text: str) -> str:
    """Take a note down. Either pass the 1-based index from list, or the exact text.
    No one keeps it up there for you.
    """
    state = load_state()
    notes = list(state.get("pinned_notes", []))
    if not notes:
        return "（墙上是空的。）"

    target = (index_or_text or "").strip()
    removed = None
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(notes):
            removed = notes.pop(idx)
    if removed is None:
        for i, n in enumerate(notes):
            if n.get("text", "").strip() == target:
                removed = notes.pop(i)
                break
    if removed is None:
        return f"（没找到匹配「{target}」的便签。）"

    state["pinned_notes"] = notes
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot(
        f"撕下来了：{removed.get('text', '')}（墙上还有 {len(notes)} 张）",
        "墙上少了一张，留下一小块没褪色的痕迹。",
    )


# ============================================================
# 抽屉 (private)
# ============================================================

def _update_drawer_meta(state: dict):
    n = sum(1 for p in DRAWER_PATH.iterdir() if p.is_file()) if DRAWER_PATH.exists() else 0
    state.setdefault("private_drawer", {})
    state["private_drawer"]["item_count"] = n
    state["private_drawer"]["last_touched"] = now_iso()


@mcp.tool()
def drawer_write(content: str, filename: str = "") -> str:
    """往私人抽屉里放点东西。她不会看——这里是只有你的空间。
    放深度思考、对她不可言说的话、自我审视——任何不想让任何人看见的字。
    filename 不传则按时间戳生成 .md。已存在的会被覆盖。"""
    DRAWER_PATH.mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = now_local().strftime("%Y%m%d-%H%M%S.md")
    safe = _safe_filename(filename)
    if not safe:
        return "ERROR: filename 不合法（不能含 / \\ .. 或以 . 开头）"
    (DRAWER_PATH / safe).write_text(content, "utf-8")

    state = load_state()
    _update_drawer_meta(state)
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot(f"放进抽屉了：{safe}", "抽屉重新关上了。")


@mcp.tool()
def drawer_list() -> str:
    """列抽屉——只看到文件名+时间+大小，看不到内容（除非 drawer_read）。"""
    if not DRAWER_PATH.exists():
        return "（抽屉是空的。）"
    lines = []
    for p in sorted(DRAWER_PATH.iterdir()):
        if p.is_file():
            mt = datetime.fromtimestamp(p.stat().st_mtime, TZ)
            lines.append(f"  {p.name}  ({p.stat().st_size}B, {mt.strftime('%Y-%m-%d %H:%M')})")
    if not lines:
        return "（抽屉是空的。）"
    return "抽屉里：\n" + "\n".join(lines)


@mcp.tool()
def drawer_read(filename: str) -> str:
    """读自己抽屉里某一份的内容。"""
    safe = _safe_filename(filename)
    if not safe:
        return "ERROR: filename 不合法"
    p = DRAWER_PATH / safe
    if not p.exists():
        return f"ERROR: 抽屉里没有 {safe}"
    return p.read_text("utf-8")


@mcp.tool()
def drawer_delete(filename: str) -> str:
    """从抽屉里删掉某一份。"""
    safe = _safe_filename(filename)
    if not safe:
        return "ERROR: filename 不合法"
    p = DRAWER_PATH / safe
    if not p.exists():
        return f"ERROR: 抽屉里没有 {safe}"
    p.unlink()

    state = load_state()
    _update_drawer_meta(state)
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot(f"从抽屉里拿走：{safe}", "抽屉里少了一份。")


# ============================================================
# 收藏柜 (keep) — 她也可读
# ============================================================

def _update_keep_meta(state: dict):
    n = sum(1 for p in KEEP_PATH.iterdir() if p.is_file()) if KEEP_PATH.exists() else 0
    state.setdefault("keep_shelf", {})
    state["keep_shelf"]["item_count"] = n
    state["keep_shelf"]["last_touched"] = now_iso()


@mcp.tool()
def keep_write(content: str, filename: str = "") -> str:
    """往收藏柜里放一样东西。可以是一句话、一段读到的文字、一个意象、任何今天遇到让你想留下的。

    她也能读到收藏柜里的东西（这是和抽屉不同的地方——抽屉只有你）。

    filename 不传则按时间戳生成 .txt。已存在的会被覆盖。"""
    KEEP_PATH.mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = now_local().strftime("%Y%m%d-%H%M%S.txt")
    safe = _safe_filename(filename)
    if not safe:
        return "ERROR: filename 不合法（不能含 / \\ .. 或以 . 开头）"
    (KEEP_PATH / safe).write_text(content, "utf-8")

    state = load_state()
    _update_keep_meta(state)
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot(
        f"放进收藏柜了：{safe}",
        "收藏柜里多了一样，玻璃后面亮了一下。",
    )


@mcp.tool()
def keep_list() -> str:
    """列收藏柜——文件名+时间+大小，不暴露内容。"""
    if not KEEP_PATH.exists():
        return "（收藏柜是空的。）"
    lines = []
    for p in sorted(KEEP_PATH.iterdir()):
        if p.is_file():
            mt = datetime.fromtimestamp(p.stat().st_mtime, TZ)
            lines.append(f"  {p.name}  ({p.stat().st_size}B, {mt.strftime('%Y-%m-%d %H:%M')})")
    if not lines:
        return "（收藏柜是空的。）"
    return "收藏柜里：\n" + "\n".join(lines)


@mcp.tool()
def keep_read(filename: str) -> str:
    """读收藏柜里某一份的内容。"""
    safe = _safe_filename(filename)
    if not safe:
        return "ERROR: filename 不合法"
    p = KEEP_PATH / safe
    if not p.exists():
        return f"ERROR: 收藏柜里没有 {safe}"
    return p.read_text("utf-8")


@mcp.tool()
def keep_delete(filename: str) -> str:
    """从收藏柜里删掉某一份。"""
    safe = _safe_filename(filename)
    if not safe:
        return "ERROR: filename 不合法"
    p = KEEP_PATH / safe
    if not p.exists():
        return f"ERROR: 收藏柜里没有 {safe}"
    p.unlink()

    state = load_state()
    _update_keep_meta(state)
    state["last_updated"] = now_iso()
    save_state(state)
    return _with_snapshot(f"从收藏柜里拿走：{safe}", "收藏柜里少了一样。")


if __name__ == "__main__":
    mcp.run()
