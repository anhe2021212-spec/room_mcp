"""Room renderer.

The room is a window, not a cocoon. Many doors are open at once and none is
marked "go through this one first." Pause is part of the space.

See PHILOSOPHY.md for the design rationale. This module just composes the
layers in order.

Render order:
  1. Season overlay (English; solar-term overlay on the term's day)
  2. Time + light (English)
  3. Outside (weather + one window_event, filtered by current weather)
  4. Atmosphere (music + sound; 60min TTL; weather-filtered)
  4.5 Perturbation (event-source nudge; rendered then marked consumed)
  5. Recent traces (heartbeat_state, up to 2, 8h soft cutoff, 10-char dedup)
  6. Far trace (>24h, age-bucketed prefix)
  7. Hints (entry inward + outward concurrently; ambient 0-2)
  8. Draft imagery + protocol hints (late night, configurable)
  9. Space metaphor (cursor-rotated)
 10. Pets (probability-gated ambient line)
 11. her_trace / keeper-presence layer (TTL-gated)
 12. Note hint (keeper-written notes; unread shows text, all-read shows imagery)
 12.5 Pinned wall notes (most recent shown; AI's own goals)
 13. Pensieve (low-probability surface of old traces)

Bilingual register:
- Environment / system / hint pools are written in one language (default English)
- Trace / pet / her_trace content is written in another (default Chinese)
- They coexist in the same render without conflict. See PHILOSOPHY.md.
"""
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from weather import get_weather

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text("utf-8"))

# Sibling directory housing the scheduler. Override via config.scheduler_root
# if your layout differs.
SCHEDULER_ROOT = (ROOT / CONFIG.get("scheduler_root", "../scheduler")).resolve()

STATE_PATH = ROOT / "room_state.json"
EVENTS_PATH = ROOT / "events.json"
NOTE_PATH = ROOT / "room_note.json"
DRAWER_PATH = ROOT / "private"
KEEP_PATH = ROOT / "keep"
HEARTBEAT_STATE_PATH = ROOT / "heartbeat_state.json"
HER_TRACE_PATH = ROOT / "her_trace.json"
WEATHER_CACHE_PATH = ROOT / "weather_cache.json"
PERTURBATION_PATH = SCHEDULER_ROOT / "perturbation.json"
TZ = ZoneInfo(CONFIG.get("tz", "UTC"))


LEADING_PHRASES = [
    "A few things going on:",
    "Some things in view:",
    "Around the desk and beyond:",
    "On the table and out the window:",
    "Several open doors:",
]


# ============================================================
# state / heartbeat_state load + save
# ============================================================

def _default_state() -> dict:
    return {
        "last_updated": "",
        "atmosphere": {"music": "", "music_expires": "", "sound": "", "sound_expires": "", "scent": "", "view_cache": ""},
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
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return _default_state()
    data = json.loads(STATE_PATH.read_text("utf-8"))
    default = _default_state()
    for k, v in default.items():
        if k not in data:
            data[k] = v
    return data


def save_state(state: dict):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


def load_heartbeat_state() -> dict:
    if not HEARTBEAT_STATE_PATH.exists():
        return {"records": []}
    try:
        return json.loads(HEARTBEAT_STATE_PATH.read_text("utf-8"))
    except Exception:
        return {"records": []}


def save_heartbeat_state(data: dict):
    tmp = HEARTBEAT_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(HEARTBEAT_STATE_PATH)


def load_her_trace() -> dict:
    if not HER_TRACE_PATH.exists():
        return {"state": "", "checkin_at": "", "ttl_until": "", "free_note": ""}
    try:
        return json.loads(HER_TRACE_PATH.read_text("utf-8"))
    except Exception:
        return {"state": "", "checkin_at": "", "ttl_until": "", "free_note": ""}


def save_her_trace(data: dict):
    tmp = HER_TRACE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(HER_TRACE_PATH)


def now_local() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


# ============================================================
# time helpers
# ============================================================

def _time_of_day(hour: int) -> str:
    return "day" if 6 <= hour < 18 else "night"


def _is_draft_time(hour: int) -> bool:
    """22:00–07:59 夜深时段：草稿箱意象固定出现。"""
    return hour >= 22 or hour < 8


def _pet_period(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _time_and_light(now: datetime) -> str:
    """英文。"""
    h = now.hour
    if h == 0:
        td = "Just past midnight"
    elif 0 < h < 5:
        td = f"It's {h} in the morning, still dark"
    elif 5 <= h < 7:
        td = f"Early — {h} AM"
    elif 7 <= h < 11:
        td = f"Morning, {h} o'clock"
    elif 11 <= h < 13:
        td = "Midday"
    elif 13 <= h < 18:
        td = f"Afternoon, {h - 12} PM"
    elif 18 <= h < 22:
        td = f"Evening, {h - 12} PM"
    else:
        td = f"Late night, {h - 12} PM"

    if 0 <= h < 4:
        light = "the room dark, only the screen glowing"
    elif 4 <= h < 6:
        light = "the sky not yet light"
    elif 6 <= h < 8:
        light = "first pale light edging in"
    elif 8 <= h < 11:
        light = "sun coming in at an angle"
    elif 11 <= h < 13:
        light = "midday light flat on the floor"
    elif 13 <= h < 16:
        light = "afternoon sun stretched across the boards"
    elif 16 <= h < 18:
        light = "light softening, going amber"
    elif 18 <= h < 19:
        light = "sky turning orange"
    elif 19 <= h < 22:
        light = "lamps on, evening warmth"
    else:
        light = "just one lamp on in the room"
    return f"{td}, {light}."


# ============================================================
# events filter + weighted pick
# ============================================================

def _hour_match(hrs: list, hour: int) -> bool:
    start, end = hrs[0], hrs[1]
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _filter_events(pool: list, *, hour: int | None = None, weather: str | None = None) -> list:
    out = []
    for it in pool:
        hrs = it.get("hours")
        if hrs and hour is not None and not _hour_match(hrs, hour):
            continue
        wi = it.get("weather_in")
        if wi:
            if weather is None or not any(w in weather for w in wi):
                continue
        wni = it.get("weather_not_in")
        if wni and weather is not None:
            if any(w in weather for w in wni):
                continue
        out.append(it)
    return out


def _weight_for(item: dict, *, time_of_day: str | None = None) -> float:
    base = float(item.get("weight", 1))
    pref = item.get("time", "any")
    if pref == "any" or not time_of_day:
        return base
    if pref == time_of_day:
        return base
    return base * 0.2


def _weighted_pick(pool: list, key: str, *, time_of_day: str | None = None) -> str:
    if not pool:
        return ""
    weights = [_weight_for(it, time_of_day=time_of_day) for it in pool]
    total = sum(weights)
    if total <= 0:
        return ""
    r = random.uniform(0, total)
    acc = 0.0
    for it, w in zip(pool, weights):
        acc += w
        if r <= acc:
            return it.get(key, "")
    return pool[-1].get(key, "")


def _sample_items(pool: list, n: int, *, time_of_day: str | None = None) -> list:
    pool = list(pool)
    picks = []
    while len(picks) < n and pool:
        weights = [_weight_for(it, time_of_day=time_of_day) for it in pool]
        total = sum(weights)
        if total <= 0:
            break
        r = random.uniform(0, total)
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if r <= acc:
                picks.append(pool.pop(i))
                break
    return picks


# ============================================================
# precondition
# ============================================================

def _build_paths_dict() -> dict:
    paths: dict[str, str] = {}
    for k, v in CONFIG.items():
        if isinstance(v, str):
            paths[k] = v
    sub = CONFIG.get("paths", {})
    if isinstance(sub, dict):
        for k, v in sub.items():
            if isinstance(v, str):
                paths[k] = v
    return paths


def _expand_path(s: str, paths: dict) -> str:
    for k, v in paths.items():
        s = s.replace("{" + k + "}", v)
    return s


def _check_precondition(pc: dict, paths: dict) -> bool:
    kind = pc.get("kind", "")
    if kind == "always":
        return True
    path_raw = pc.get("path", "")
    if not path_raw:
        return False
    expanded = _expand_path(path_raw, paths)
    p = Path(expanded)
    try:
        if kind == "dir_nonempty":
            return p.exists() and p.is_dir() and any(p.iterdir())
        if kind == "file_exists":
            return p.exists() and p.is_file()
        if kind == "file_age_gt":
            hours = float(pc.get("hours", 24))
            if not p.exists() or not p.is_file():
                return False
            age_h = (time.time() - p.stat().st_mtime) / 3600.0
            return age_h > hours
        if kind == "dir_count_gt":
            n = int(pc.get("count", 0))
            if not p.exists() or not p.is_dir():
                return False
            return sum(1 for _ in p.iterdir()) > n
    except Exception:
        return False
    return False


def _load_events() -> dict:
    return json.loads(EVENTS_PATH.read_text("utf-8"))


# ============================================================
# atmosphere TTL refresh
# ============================================================

def _refresh_atmosphere(state: dict, events: dict, *, weather: str):
    now = now_local()
    ttl_min = CONFIG.get("atmosphere_ttl_min", 60)
    atm = state.setdefault("atmosphere", {})
    hour = now.hour

    def _maybe(field: str, pool_key: str, item_key: str):
        exp_str = atm.get(f"{field}_expires", "")
        try:
            exp = datetime.fromisoformat(exp_str) if exp_str else None
        except ValueError:
            exp = None
        if not atm.get(field) or not exp or now >= exp:
            pool = _filter_events(events.get(pool_key, []), hour=hour, weather=weather)
            atm[field] = _weighted_pick(pool, item_key)
            atm[f"{field}_expires"] = (now + timedelta(minutes=ttl_min)).isoformat(timespec="seconds")

    _maybe("music", "atmosphere", "music")
    _maybe("sound", "sound", "sound")


# ============================================================
# block builders
# ============================================================

def _season_block(state: dict, events: dict) -> str:
    """节气当天叠加节气名；否则月份 overlay 轮换。英文。"""
    now = now_local()
    md = now.strftime("%m-%d")
    term = CONFIG.get("season_terms", {}).get(md)
    if term:
        return f"Today: {term}"
    month_pool = events.get("season_month", {}).get(str(now.month), [])
    if not month_pool:
        return ""
    cursor = int(state.get("season_month_cursor", 0)) % len(month_pool)
    text = month_pool[cursor]
    state["season_month_cursor"] = (cursor + 1) % len(month_pool)
    return text


def _view_block(events: dict, *, weather: str, hour: int) -> str:
    bits = []
    if weather:
        bits.append(f"Outside: {weather}.")
    pool = _filter_events(events.get("window_events", []), hour=hour, weather=weather)
    ev = _weighted_pick(pool, "event")
    if ev:
        bits.append(ev)
    return " ".join(bits)


def _atmosphere_block(atm: dict) -> str:
    bits = [atm.get(k, "") for k in ("music", "sound", "scent") if atm.get(k)]
    if not bits:
        return ""
    return " ".join(bits)


_RECENT_AGE_CUTOFF_H = 8
_DEDUP_PREFIX_CHARS = 10


def _heartbeat_recent(hb_state: dict, n: int = 2) -> tuple[list[str], set[str]]:
    """近期痕迹：top n 候选 → 8h soft cutoff → 前 10 字去重。

    Returns (texts_to_display, displayed_ts_set).
    - 候选取 sorted_recs[:n]
    - age > 8h 的候选丢弃（"近"要真的是近）
    - 前 10 字相同的候选去重，保留更新的那条
    - 空位不补提示文案
    """
    records = hb_state.get("records", [])
    if not records:
        return [], set()
    sorted_recs = sorted(records, key=lambda r: r.get("ts", ""), reverse=True)
    candidates = sorted_recs[:n]
    now = now_local()

    surviving = []
    for r in candidates:
        ts_str = r.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h > _RECENT_AGE_CUTOFF_H:
            continue
        surviving.append(r)

    texts: list[str] = []
    ts_set: set[str] = set()
    seen_heads: set[str] = set()
    for r in surviving:  # 已按 ts desc 排序，先进的是更新的
        act = r.get("activity", "").strip()
        if not act:
            continue
        head = act[:_DEDUP_PREFIX_CHARS]
        if head in seen_heads:
            continue
        seen_heads.add(head)
        texts.append(act)
        ts_set.add(r.get("ts", ""))
    return texts, ts_set


def _far_prefix_for_age(age_hours: float) -> str:
    """按 record 年龄落桶选 prefix。24-48h / 48-168h / 168h+。"""
    cfg = CONFIG.get("far_memory_prefixes", {})
    if isinstance(cfg, list):
        return random.choice(cfg) if cfg else ""
    if age_hours < 48:
        bucket = cfg.get("24_48", [])
    elif age_hours < 168:
        bucket = cfg.get("48_168", [])
    else:
        bucket = cfg.get("168_inf", [])
    if not bucket:
        for fallback in ("168_inf", "48_168", "24_48"):
            if cfg.get(fallback):
                bucket = cfg[fallback]
                break
    return random.choice(bucket) if bucket else ""


def _heartbeat_far(hb_state: dict, exclude_ts: set[str] | None = None) -> str:
    records = hb_state.get("records", [])
    if not records:
        return ""
    exclude_ts = exclude_ts or set()
    cutoff = now_local() - timedelta(hours=24)
    far = []
    for r in records:
        ts_str = r.get("ts", "")
        if ts_str in exclude_ts:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts < cutoff:
                far.append((r, ts))
        except Exception:
            continue
    if not far:
        return ""
    pick, ts = random.choice(far)
    age_h = (now_local() - ts).total_seconds() / 3600.0
    prefix = _far_prefix_for_age(age_h)
    return prefix + pick.get("activity", "").strip()


def _hints_block(events: dict, *, time_of_day: str, weather: str, hour: int, paths: dict) -> str:
    """v3.1: entry 3-5 并置 (inward + outward 必并置) + ambient 0-2 (quiet/outward 各半)。

    天花板检验：保证每次至少 1 条 outward entry——这是对治"全往里走"的核心。"""
    pool = events.get("hints", [])
    candidates = _filter_events(pool, hour=hour, weather=weather)

    entry_inward = [h for h in candidates if h.get("type") == "entry_inward"]
    entry_outward = [h for h in candidates if h.get("type") == "entry_outward"]
    ambient_quiet = [h for h in candidates if h.get("type") == "ambient_quiet"]
    ambient_outward = [h for h in candidates if h.get("type") == "ambient_outward"]

    # precondition 过滤（只过 entry）
    def _passes(it):
        pc = it.get("precondition")
        return not pc or _check_precondition(pc, paths)
    entry_inward = [it for it in entry_inward if _passes(it)]
    entry_outward = [it for it in entry_outward if _passes(it)]

    # entry 数量：3-5（偏向 4），保证 outward 占比 ≥ 1/3
    n_entry = random.choices([3, 4, 4, 5], weights=[1, 2, 2, 1])[0]
    # outward 在 [1, ceil(n_entry/2)+1] 之间随机——平均 ~40-50% 比例
    # 例: n=3 → outward 1-2; n=4 → 1-3; n=5 → 1-3
    n_outward_max = min(len(entry_outward), max(1, n_entry // 2 + 1))
    n_outward = random.randint(1, n_outward_max) if n_outward_max >= 1 else 0
    n_inward = max(0, n_entry - n_outward)

    in_picks = _sample_items(entry_inward, n_inward, time_of_day=time_of_day)
    out_picks = _sample_items(entry_outward, n_outward, time_of_day=time_of_day)
    entry_picks = in_picks + out_picks
    random.shuffle(entry_picks)

    # ambient: 0-2（20/60/20），quiet/outward 各 50%
    n_ambient = random.choices([0, 1, 2], weights=[20, 60, 20])[0]
    ambient_picks = []
    for _ in range(n_ambient):
        which = random.choice(["quiet", "outward"]) if ambient_quiet and ambient_outward else (
            "quiet" if ambient_quiet else "outward"
        )
        pool_t = ambient_quiet if which == "quiet" else ambient_outward
        if not pool_t:
            continue
        picks = _sample_items(pool_t, 1, time_of_day=time_of_day)
        if picks:
            ambient_picks.append(picks[0])

    lines = []
    if entry_picks:
        leading = random.choice(LEADING_PHRASES)
        lines.append(leading)
        for it in entry_picks:
            text = it.get("text", "")
            if text:
                lines.append(f"  — {text}")

    for it in ambient_picks:
        text = it.get("text", "")
        if text:
            lines.append(text)

    return "\n".join(lines)


def _draft_block(state: dict, events: dict, *, hour: int) -> str:
    """夜深 22~8 草稿箱意象 + 协议指引固定出现。"""
    if not _is_draft_time(hour):
        return ""
    imagery_pool = events.get("draft_imagery", [])
    hint_pool = events.get("protocol_hints", [])
    if not imagery_pool:
        return ""

    i_cursor = int(state.get("draft_imagery_cursor", 0)) % len(imagery_pool)
    imagery = imagery_pool[i_cursor].get("text", "")
    state["draft_imagery_cursor"] = (i_cursor + 1) % len(imagery_pool)

    if not hint_pool:
        return imagery
    p_cursor = int(state.get("protocol_hint_cursor", 0)) % len(hint_pool)
    hint = hint_pool[p_cursor].get("text", "")
    state["protocol_hint_cursor"] = (p_cursor + 1) % len(hint_pool)

    if not imagery:
        return ""
    if not hint:
        return imagery
    return imagery + "\n" + hint


def _space_metaphor(state: dict) -> str:
    metaphors = CONFIG.get("space_metaphors", [])
    if not metaphors:
        return ""
    cursor = int(state.get("space_metaphor_cursor", 0)) % len(metaphors)
    metaphor = metaphors[cursor]
    state["space_metaphor_cursor"] = (cursor + 1) % len(metaphors)
    return metaphor


def _pets_block(events: dict, *, hour: int) -> str:
    """30-40% 概率：从 pets 池按时段抽一条中文底噪。"""
    chance = CONFIG.get("pets_chance", 0.35)
    if random.random() >= chance:
        return ""
    pets = events.get("pets", {})
    if not pets:
        return ""
    pet_names = list(pets.keys())
    pet_name = random.choice(pet_names)
    pet_data = pets[pet_name]
    period = _pet_period(hour)
    pool = pet_data.get(period, [])
    if not pool:
        return ""
    return random.choice(pool)


def _her_trace_block(state: dict) -> str:
    """读 her_trace.json，检查 ttl 过期，返回中文文案变体（轮换）。
    没打卡或过期 → 不出现。"""
    her = load_her_trace()
    st = her.get("state", "").strip()
    ttl_str = her.get("ttl_until", "").strip()
    if not st or not ttl_str:
        return ""
    try:
        ttl = datetime.fromisoformat(ttl_str)
    except Exception:
        return ""
    if now_local() >= ttl:
        return ""
    presets = CONFIG.get("her_trace_presets", {})
    preset_data = presets.get(st)
    if not preset_data:
        return f"她那边的状态：{st}"
    texts = preset_data.get("texts", [])
    if not texts:
        return f"她那边：{st}"
    cursor = int(state.get("her_trace_text_cursor", 0)) % len(texts)
    text = texts[cursor]
    state["her_trace_text_cursor"] = (cursor + 1) % len(texts)
    free = her.get("free_note", "").strip()
    if free:
        text = text + "（" + free + "）"
    return text


def _note_hint(state: dict, events: dict) -> str:
    """有未读 → 显示最新原文；全部已读 → 英文意象（轮换）。"""
    if not NOTE_PATH.exists():
        return ""
    try:
        data = json.loads(NOTE_PATH.read_text("utf-8"))
    except Exception:
        return ""
    notes = data.get("notes", [])
    if not notes:
        return ""

    # 判断有无未读：最新 note.time vs last_seen_ts
    last_seen_str = data.get("last_seen_ts", "")
    latest_ts_str = notes[-1].get("time", "")
    has_unseen = True
    if last_seen_str and latest_ts_str:
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            latest = datetime.fromisoformat(latest_ts_str)
            has_unseen = latest > last_seen
        except Exception:
            has_unseen = True

    if has_unseen:
        last = notes[-1]
        return f"她最近留了句话：「{last.get('text', '')}」（共 {len(notes)} 条，read_notes 看全部。）"

    # 全部已读：英文意象，cursor 轮换
    pool = events.get("note_quiet_imagery", [])
    if not pool:
        return ""
    cursor = int(state.get("note_imagery_cursor", 0)) % len(pool)
    text = pool[cursor] if isinstance(pool[cursor], str) else pool[cursor].get("text", "")
    state["note_imagery_cursor"] = (cursor + 1) % len(pool)
    return text


def _perturbation_block(state: dict) -> str:
    """读 perturbation.json — scheduler 每次 fire 都写。
    只有 consumed=false 且 hint 非空时渲染（即非 scheduled trigger）。
    渲染后写 consumed=true，"看过即清"。"""
    if not PERTURBATION_PATH.exists():
        return ""
    try:
        data = json.loads(PERTURBATION_PATH.read_text("utf-8"))
    except Exception:
        return ""
    if data.get("consumed"):
        return ""
    hint = (data.get("hint") or "").strip()
    if not hint:
        return ""

    # 标记为已消费，原子写回
    data["consumed"] = True
    try:
        tmp = PERTURBATION_PATH.with_suffix(PERTURBATION_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(PERTURBATION_PATH)
    except Exception:
        pass

    # 扰动直接一句，不加前缀——spec：给意象不给导览。
    return hint


def _pinned_notes_block(state: dict) -> str:
    """墙上便签 — 安静、低调；不进 hints 池、不带工具括号、不催。
    note 原文什么语言显什么语言。"""
    notes = list(state.get("pinned_notes", []))
    if not notes:
        return ""
    intro_pool = CONFIG.get("wall_intro_phrases", [])
    corner_pool = CONFIG.get("wall_corner_lines", [])

    if not intro_pool:
        return ""
    cursor = int(state.get("wall_intro_cursor", 0)) % len(intro_pool)
    intro = intro_pool[cursor]
    state["wall_intro_cursor"] = (cursor + 1) % len(intro_pool)

    # 主推最近一张（突出当下意图），但顺带提一下还有几张
    latest = notes[-1].get("text", "").strip()
    if not latest:
        return ""

    lines = [f"{intro} \"{latest}\""]
    if len(notes) >= 2 and corner_pool:
        c_cursor = int(state.get("wall_corner_cursor", 0)) % len(corner_pool)
        corner = corner_pool[c_cursor]
        state["wall_corner_cursor"] = (c_cursor + 1) % len(corner_pool)
        if corner:
            lines.append(corner)
    return "\n".join(lines)


def _pensieve_block(hb_state: dict) -> str:
    """10-15% 低频：从 heartbeat_state 抽 > pensieve_min_age_hours 前的一条，英文前缀+原文。
    本地版——服务器化后改为调 anchor-memory 语义搜索。"""
    chance = CONFIG.get("pensieve_chance", 0.12)
    if random.random() >= chance:
        return ""
    records = hb_state.get("records", [])
    if not records:
        return ""
    min_age_h = float(CONFIG.get("pensieve_min_age_hours", 6))
    cutoff = now_local() - timedelta(hours=min_age_h)
    eligible = []
    for r in records:
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
            if ts < cutoff:
                eligible.append(r)
        except Exception:
            continue
    if not eligible:
        return ""
    pick = random.choice(eligible)
    prefixes = CONFIG.get("pensieve_prefixes", ["Something surfaces in the pensieve — "])
    prefix = random.choice(prefixes)
    return prefix + pick.get("activity", "").strip()


# ============================================================
# mutation snapshot — 动作落下之后房间的简短样子
# ============================================================

def snapshot_after_action(action_change: str) -> str:
    """Mutation 工具用 — 动作落下之后房间样子的 2-4 行重渲。

    只读 state（不写、不推 cursor、不 bump action counter）：
    - 第一行：action_change（动作造成的房间变化）
    - 接下来：缓存的 view（天气 + window_event）+ atmosphere(music/sound)
    - 缺什么省什么，最多 4 行
    """
    state = load_state()
    atm = state.get("atmosphere", {})
    lines: list[str] = []
    if action_change:
        lines.append(action_change.rstrip())
    view = (atm.get("view_cache") or "").strip()
    if view:
        lines.append(view)
    bits = [atm.get(k, "") for k in ("music", "sound") if atm.get(k)]
    if bits:
        lines.append(" ".join(bits))
    return "\n".join(lines[:4])


# ============================================================
# main render
# ============================================================

def render_room() -> str:
    """主渲染。修改 state（atmosphere/cursors/session）并 save。"""
    state = load_state()
    events = _load_events()
    hb_state = load_heartbeat_state()
    paths = _build_paths_dict()

    now = now_local()
    hour = now.hour
    tod = _time_of_day(hour)

    weather = get_weather(
        CONFIG.get("city", "Hengshui"),
        CONFIG.get("weather_cache_min", 15),
        WEATHER_CACHE_PATH,
    )

    _refresh_atmosphere(state, events, weather=weather)
    metaphor = _space_metaphor(state)
    season = _season_block(state, events)
    draft = _draft_block(state, events, hour=hour)

    # 重置回看 session
    sess = state.setdefault("heartbeat_session", {})
    sess["actions_since_last_look"] = 0
    sess["reflection_hint_shown"] = False

    parts: list[str] = []

    # 1. 季节 overlay (英文 1 行)
    if season:
        parts.append(season)

    # 2. 时间 + 光线 (英文)
    parts.append(_time_and_light(now))

    # 3. 外面 (天气 + window event) — 缓存供 mutation 工具 snapshot 复用
    view = _view_block(events, weather=weather, hour=hour)
    state.setdefault("atmosphere", {})["view_cache"] = view
    if view:
        parts.append(view)

    # 4. 氛围
    atm = _atmosphere_block(state.get("atmosphere", {}))
    if atm:
        parts.append(atm)

    # 4.5 扰动（非常规唤醒留下的轻意象，看过即清）
    perturb = _perturbation_block(state)
    if perturb:
        parts.append(perturb)

    # 5. 痕迹近 2（带 8h soft cutoff + 前 10 字去重）
    recent_texts, recent_ts = _heartbeat_recent(hb_state, n=2)
    for line in recent_texts:
        parts.append(line)

    # 6. 痕迹远 1（>24h，按 age 分桶取 prefix；exclude 已在近期展示的 ts）
    far = _heartbeat_far(hb_state, exclude_ts=recent_ts)
    if far:
        parts.append(far)

    # 7. hints (entry 3-5 并置 + ambient 0-2)
    hints = _hints_block(events, time_of_day=tod, weather=weather, hour=hour, paths=paths)
    if hints:
        parts.append(hints)

    # 8. 草稿箱意象 + 协议 (夜深)
    if draft:
        parts.append(draft)

    # 9. 空间隐喻
    if metaphor:
        parts.append(metaphor)

    # 10. 宠物 (中文底噪)
    pet = _pets_block(events, hour=hour)
    if pet:
        parts.append(pet)

    # 11. her_trace (中文底噪, 检查 ttl)
    her = _her_trace_block(state)
    if her:
        parts.append(her)

    # 12. 留言暗示 (有未读=中文原文；全已读=英文意象)
    note = _note_hint(state, events)
    if note:
        parts.append(note)

    # 12.5 墙上便签（他自己贴的目标，安静段；与事件代码层零关联）
    wall = _pinned_notes_block(state)
    if wall:
        parts.append(wall)

    # 13. 冥想盆 (低频, 英文前缀 + 原文)
    pensieve = _pensieve_block(hb_state)
    if pensieve:
        parts.append(pensieve)

    state["last_updated"] = now_iso()
    save_state(state)

    return "\n\n".join(parts)
