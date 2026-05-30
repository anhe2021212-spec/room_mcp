"""Self-driven scheduler — dual-mode scheduling + event triggers + perturbations.

Task model:
- Each task supports both `cron` (recurring) and `next_run` (one-shot ISO timestamp)
- `next_run` wins; after firing it is consumed (cleared)
- The "heartbeat" task abandons cron entirely. After each fire it auto-generates
  the next random `next_run` (denser by day, sparser at night — configurable)

Event sources (besides cron / next_run):
- `manual_wake.flag` exists  → fire "manual" (keeper-knock)
- weather detector            → fire "event:weather" when weather kind changes
- calendar detector           → fire "event:calendar" or "event:{custom}" at 00:30
                                each day (custom comes from a festival entry with
                                a `special` field — e.g. set `special: "my_birthday"`
                                to fire "event:my_birthday" instead of "event:calendar")
- morning_news detector       → fire "morning_news" once in the daily window
- mail detector               → fire "event:mail" on matching unseen mail (off by default)

Priority (same-tick yield rule):
    manual > weather > calendar > morning_news > mail > scheduled
  morning_news yields if manual or weather fired this tick (gives up the day)
  calendar yields if manual fired this tick (gives up the day)

Each fire writes a perturbation.json record that the room renderer consumes
once (writing consumed=true on read):
  {
    "trigger": "scheduled" | "manual" | "event:mail" | "event:weather"
             | "morning_news" | "event:calendar" | "event:<custom>",
    "kind": "manual" | "mail" | "weather:rain" | "morning_news" | "calendar" | ... | null,
    "hint": "..." | null,         # null for "scheduled"
    "fired_at": "<iso>",
    "consumed": false
  }

Delivery is via the pluggable Notifier interface (see notifiers/). The default
notifier formats the heartbeat as:
    [♥{task_id}:{trigger}] {prompt}
"""
import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from croniter import croniter

from notifiers import load_notifier

BASE_DIR = Path(__file__).parent
SCHEDULE_FILE = BASE_DIR / "schedule.json"
LAST_RUN_FILE = BASE_DIR / "last_run.json"
CONFIG_FILE = BASE_DIR / "scheduler_config.json"
LOG_FILE = BASE_DIR / "scheduler.log"
PID_FILE = BASE_DIR / "scheduler.pid"

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
log = logging.getLogger("scheduler")
log.setLevel(logging.INFO)
log.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(console)
telethon_log = logging.getLogger("telethon")
telethon_log.setLevel(logging.WARNING)
telethon_log.addHandler(handler)


# ------------------------------------------------------------------
# IO helpers (atomic)
# ------------------------------------------------------------------

def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"load_json {path.name} failed: {e}")
    return default if default is not None else {}


def save_json_atomic(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict:
    return load_json(CONFIG_FILE, {})


def load_schedule() -> dict:
    return load_json(SCHEDULE_FILE, {"tasks": []})


def save_schedule(data: dict):
    save_json_atomic(SCHEDULE_FILE, data)


def load_last_run() -> dict:
    return load_json(LAST_RUN_FILE, {})


def save_last_run(data: dict):
    save_json_atomic(LAST_RUN_FILE, data)


# ------------------------------------------------------------------
# Single-instance lock
# ------------------------------------------------------------------

def check_single_instance():
    if PID_FILE.exists():
        old_pid = PID_FILE.read_text().strip()
        try:
            os.kill(int(old_pid), 0)
            log.error(f"Another scheduler running (PID {old_pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError, OSError):
            pass
    PID_FILE.write_text(str(os.getpid()))


# ------------------------------------------------------------------
# Heartbeat rhythm: 自动生成下次随机 next_run
# ------------------------------------------------------------------

def _is_day(hour: int, day_hours: list) -> bool:
    start, end = int(day_hours[0]), int(day_hours[1])
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def compute_next_heartbeat_run(cfg: dict, now: datetime | None = None) -> str:
    rhythm = cfg.get("heartbeat_rhythm", {})
    day_hours = rhythm.get("day_hours", [8, 22])
    day_range = rhythm.get("day_range_min", [30, 60])
    night_range = rhythm.get("night_range_min", [90, 180])
    now = now or datetime.now()
    if _is_day(now.hour, day_hours):
        lo, hi = float(day_range[0]), float(day_range[1])
    else:
        lo, hi = float(night_range[0]), float(night_range[1])
    delta_min = random.uniform(lo, hi)
    nxt = now + timedelta(minutes=delta_min)
    return nxt.isoformat(timespec="seconds")


# ------------------------------------------------------------------
# should_fire: 双模型
# next_run 优先, fire 即消费 (清空 next_run 并由调用方决定下一次)
# ------------------------------------------------------------------

def should_fire(task: dict, last_run: dict, now: datetime) -> tuple[bool, str]:
    if not task.get("enabled", True):
        return False, "disabled"

    task_id = task["id"]
    next_run_str = task.get("next_run", "").strip()
    if next_run_str:
        try:
            nr = datetime.fromisoformat(next_run_str)
            if now >= nr:
                return True, f"next_run={nr.strftime('%m-%d %H:%M')}"
            return False, f"waiting next_run {nr.strftime('%m-%d %H:%M')}"
        except Exception as e:
            log.warning(f"[{task_id}] next_run parse fail: {e}, falling back to cron")

    cron_expr = task.get("cron", "").strip()
    if not cron_expr:
        return False, "no cron and no next_run"

    last = last_run.get(task_id)
    last_dt = datetime.fromisoformat(last) if last else datetime(2000, 1, 1)
    cron = croniter(cron_expr, last_dt)
    next_fire = cron.get_next(datetime)
    if now >= next_fire:
        return True, f"cron next={next_fire.strftime('%H:%M')}"
    return False, f"waiting cron {next_fire.strftime('%H:%M')}"


# ------------------------------------------------------------------
# Perturbation: 写 perturbation.json (room_mcp 消费)
# ------------------------------------------------------------------

def _imagery_for(cfg: dict, trigger: str, kind: str | None) -> str | None:
    if trigger == "scheduled":
        return None
    pool_map = cfg.get("perturbation_imagery", {})
    if trigger == "event:weather" and kind:
        key = f"event:weather:{kind}"
        pool = pool_map.get(key) or []
        if pool:
            return random.choice(pool)
    pool = pool_map.get(trigger) or []
    if pool:
        return random.choice(pool)
    return None


def write_perturbation(cfg: dict, trigger: str, kind: str | None = None, hint_override: str | None = None):
    """每次 fire 都写一份。room_mcp render 时读 + 写 consumed=true。

    hint_override: 显式指定 hint（用于 morning_news / calendar 这些自行从池里选过的 trigger）。
    传 None 时回退到 _imagery_for（按 trigger 在 perturbation_imagery 里查）。
    """
    paths = cfg.get("paths", {})
    pp = Path(paths.get("perturbation", str(BASE_DIR / "perturbation.json")))
    hint = hint_override if hint_override is not None else _imagery_for(cfg, trigger, kind)
    data = {
        "trigger": trigger,
        "kind": kind,
        "hint": hint,
        "fired_at": datetime.now().isoformat(timespec="seconds"),
        "consumed": False,
    }
    try:
        save_json_atomic(pp, data)
    except Exception as e:
        log.warning(f"write_perturbation failed: {e}")


# ------------------------------------------------------------------
# Fire flow
# ------------------------------------------------------------------

async def do_fire(notifier, task: dict, trigger: str, kind: str | None,
                  cfg: dict, last_run: dict, hint_override: str | None = None):
    """Write perturbation, send via the notifier, update last_run.

    The notifier is responsible for transport-level concerns (delete-after-delay,
    retries, etc.). The scheduler just delegates send_heartbeat.
    """
    task_id = task["id"]
    prompt = task.get("prompt", "heartbeat")

    # 1. Write perturbation FIRST — the AI must see it on look_at_room
    write_perturbation(cfg, trigger, kind, hint_override=hint_override)

    # 2. Delegate delivery
    try:
        await notifier.send_heartbeat(task_id=task_id, trigger=trigger, prompt=prompt)
        log.info(f"Sent [{task_id}:{trigger}] kind={kind}")
    except Exception as e:
        log.warning(f"notifier.send_heartbeat failed [{task_id}:{trigger}]: {e}")

    # 3. Update last_run
    last_run[task_id] = datetime.now().isoformat(timespec="seconds")
    save_last_run(last_run)


def consume_next_run(task: dict, schedule: dict, cfg: dict):
    """fire 后清空 next_run。heartbeat 任务自动生成下一次随机 next_run。"""
    task_id = task["id"]
    for t in schedule.get("tasks", []):
        if t["id"] == task_id:
            t["next_run"] = ""
            if task_id == "heartbeat":
                t["next_run"] = compute_next_heartbeat_run(cfg)
            break
    save_schedule(schedule)


# ------------------------------------------------------------------
# Manual wake flag detection
# ------------------------------------------------------------------

def check_manual_wake(cfg: dict) -> bool:
    paths = cfg.get("paths", {})
    flag = Path(paths.get("manual_wake_flag", str(BASE_DIR / "manual_wake.flag")))
    if flag.exists():
        try:
            flag.unlink()
        except Exception as e:
            log.warning(f"unlink manual_wake.flag failed: {e}")
        return True
    return False


# ------------------------------------------------------------------
# Weather event detector
# ------------------------------------------------------------------

_LAST_WEATHER_TICK = {"ts": 0.0}


def _pick_weather_kind(weather_str: str, keywords: list) -> str | None:
    """从天气字符串里找出第一个匹配的关键词。"""
    if not weather_str:
        return None
    low = weather_str.lower()
    for kw in keywords:
        if kw.lower() in low:
            return kw.lower()
    return None


async def weather_detector_tick(cfg: dict) -> tuple[str | None, str | None]:
    """检查是否到 interval、是否显著变化。返回 (trigger, kind) 或 (None, None)。"""
    wcfg = cfg.get("weather_detector", {})
    if not wcfg.get("enabled", True):
        return None, None
    interval = float(wcfg.get("tick_interval_sec", 600))
    now_ts = datetime.now().timestamp()
    if now_ts - _LAST_WEATHER_TICK["ts"] < interval:
        return None, None
    _LAST_WEATHER_TICK["ts"] = now_ts

    city = wcfg.get("city", "Hengshui")
    keywords = wcfg.get("significant_keywords", [])
    cache_path = Path(cfg.get("paths", {}).get(
        "weather_cache", str(BASE_DIR / "weather_detector_cache.json")
    ))

    # 拉天气（同步 requests，时间短，加 timeout）。复用 wttr.in。
    try:
        import requests
        proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        r = await asyncio.to_thread(
            requests.get, f"https://wttr.in/{city}?format=%C",
            timeout=10, proxies=proxies,
        )
        weather = (r.text or "").strip()
    except Exception as e:
        log.warning(f"weather_detector fetch failed: {e}")
        return None, None

    if not weather:
        return None, None

    cur_kind = _pick_weather_kind(weather, keywords)
    cache = load_json(cache_path, {})
    last_kind = cache.get("kind")
    last_weather = cache.get("weather", "")

    # 写新 cache (无论是否变化)
    save_json_atomic(cache_path, {
        "weather": weather,
        "kind": cur_kind,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })

    # 显著变化：kind 改变（None→X / X→Y / X→None）
    if last_weather and last_kind != cur_kind and cur_kind is not None:
        log.info(f"weather change: '{last_weather}'({last_kind}) -> '{weather}'({cur_kind})")
        return "event:weather", cur_kind
    return None, None


# ------------------------------------------------------------------
# Mail event detector (默认 disabled, IMAP 凭证未配)
# ------------------------------------------------------------------

_LAST_MAIL_TICK = {"ts": 0.0}


async def mail_detector_tick(cfg: dict) -> tuple[str | None, str | None]:
    mcfg = cfg.get("mail_detector", {})
    if not mcfg.get("enabled", False):
        return None, None
    interval = float(mcfg.get("tick_interval_sec", 300))
    now_ts = datetime.now().timestamp()
    if now_ts - _LAST_MAIL_TICK["ts"] < interval:
        return None, None
    _LAST_MAIL_TICK["ts"] = now_ts

    host = mcfg.get("imap_host")
    user = mcfg.get("imap_user")
    pw = mcfg.get("imap_pass")
    if not (host and user and pw):
        return None, None

    seen_path = Path(mcfg.get("seen_uid_path", str(BASE_DIR / "mail_seen_uids.json")))
    seen = set(load_json(seen_path, {"uids": []}).get("uids", []))
    watch_senders = [s.lower() for s in mcfg.get("watch_senders", [])]
    watch_keywords = [k.lower() for k in mcfg.get("watch_keywords", [])]

    def _fetch():
        import imaplib
        import email as emaillib
        m = imaplib.IMAP4_SSL(host, int(mcfg.get("imap_port", 993)))
        m.login(user, pw)
        m.select("INBOX")
        typ, data = m.search(None, "UNSEEN")
        new_uids = []
        if typ == "OK":
            for uid in (data[0] or b"").split():
                uid_s = uid.decode()
                if uid_s in seen:
                    continue
                typ2, mdata = m.fetch(uid, "(RFC822.HEADER)")
                if typ2 != "OK" or not mdata:
                    continue
                raw = mdata[0][1] if mdata[0] else b""
                msg = emaillib.message_from_bytes(raw)
                sender = (msg.get("From", "") or "").lower()
                subject = (msg.get("Subject", "") or "").lower()
                hit = False
                if not watch_senders and not watch_keywords:
                    hit = True
                if any(s in sender for s in watch_senders):
                    hit = True
                if any(k in subject for k in watch_keywords):
                    hit = True
                if hit:
                    new_uids.append(uid_s)
        m.logout()
        return new_uids

    try:
        new_uids = await asyncio.to_thread(_fetch)
    except Exception as e:
        log.warning(f"mail_detector failed: {e}")
        return None, None

    if not new_uids:
        return None, None
    seen.update(new_uids)
    # 简单截断防无限增长
    if len(seen) > 500:
        seen = set(list(seen)[-500:])
    save_json_atomic(seen_path, {"uids": list(seen)})
    return "event:mail", "mail"


# ------------------------------------------------------------------
# Morning news detector — 每日 06-09 内随机一个时间点 fire 一次
# ------------------------------------------------------------------

def _ensure_morning_news_target(cfg: dict, last_run: dict, now: datetime) -> str | None:
    """保证 last_run 里有 morning_news_target_at（今日的目标时间）；跨日重新生成。
    返回当日 target 的 ISO 字符串；morning_news 关闭时返回 None。"""
    nm_cfg = cfg.get("morning_news", {})
    if not nm_cfg.get("enabled", True):
        return None
    today = now.date()
    target_str = last_run.get("morning_news_target_at", "")
    if target_str:
        try:
            target_dt = datetime.fromisoformat(target_str)
            if target_dt.date() == today:
                return target_str
        except Exception:
            pass

    # 跨日 → 新生成。同时清掉 fired_date（如果是昨天的）
    start_h = int(nm_cfg.get("window_start_hour", 6))
    end_h = int(nm_cfg.get("window_end_hour", 9))
    span_min = max(1, (end_h - start_h) * 60)
    offset = random.randint(0, span_min - 1)
    target_dt = datetime.combine(today, datetime.min.time()) + timedelta(hours=start_h, minutes=offset)
    target_iso = target_dt.isoformat(timespec="seconds")
    last_run["morning_news_target_at"] = target_iso
    save_last_run(last_run)
    log.info(f"morning_news target for {today.isoformat()}: {target_iso}")
    return target_iso


def _pick_morning_news_hint(cfg: dict, last_run: dict) -> str | None:
    """日内不重复、优先未抽过的。池抽完后 reset 重抽。"""
    pool = cfg.get("perturbation_imagery", {}).get("morning_news", []) or []
    if not pool:
        return None
    used = list(last_run.get("morning_news_used_hints", []))
    unused = [h for h in pool if h not in used]
    if not unused:
        used = []
        unused = list(pool)
    hint = random.choice(unused)
    used.append(hint)
    if len(used) > len(pool):
        used = used[-len(pool):]
    last_run["morning_news_used_hints"] = used
    return hint


def morning_news_tick(cfg: dict, last_run: dict, now: datetime, blocked_this_tick: bool) -> tuple[str | None, str | None, str | None]:
    """每日 06-09 内随机一个时间点 fire 一次。
    返回 (trigger, kind, hint_override) 或 (None, None, None)。
    blocked_this_tick=True 时（manual/weather 已 fire），让位且当日放弃。
    """
    nm_cfg = cfg.get("morning_news", {})
    if not nm_cfg.get("enabled", True):
        return None, None, None

    today_iso = now.strftime("%Y-%m-%d")
    if last_run.get("morning_news_fired_date") == today_iso:
        return None, None, None

    target_str = _ensure_morning_news_target(cfg, last_run, now)
    if not target_str:
        return None, None, None
    try:
        target_dt = datetime.fromisoformat(target_str)
    except Exception:
        return None, None, None

    if now < target_dt:
        return None, None, None

    # 到点了。如果同 tick 已有 manual/weather → 让位且当日放弃
    if blocked_this_tick:
        last_run["morning_news_fired_date"] = today_iso
        save_last_run(last_run)
        log.info(f"morning_news yielded to higher-priority fire on {today_iso}")
        return None, None, None

    hint = _pick_morning_news_hint(cfg, last_run)
    last_run["morning_news_fired_date"] = today_iso
    save_last_run(last_run)
    return "morning_news", "morning_news", hint


# ------------------------------------------------------------------
# Calendar event detector — 节气/节日，每日 00:30 一次
# ------------------------------------------------------------------

_LUNAR_MONTH_MAP = {
    "正": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "冬": 11, "腊": 12,
}
_LUNAR_DAY_MAP = {
    "初一": 1, "初二": 2, "初三": 3, "初四": 4, "初五": 5,
    "初六": 6, "初七": 7, "初八": 8, "初九": 9, "初十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
    "廿一": 21, "廿二": 22, "廿三": 23, "廿四": 24, "廿五": 25,
    "廿六": 26, "廿七": 27, "廿八": 28, "廿九": 29, "三十": 30,
}


def _parse_lunar_date(s: str) -> tuple[int, int] | None:
    """e.g. '正月初一' → (1, 1) ; '腊月三十' → (12, 30) ;
    解析失败返回 None。"""
    for ch, num in _LUNAR_MONTH_MAP.items():
        prefix = ch + "月"
        if s.startswith(prefix):
            day_part = s[len(prefix):]
            d = _LUNAR_DAY_MAP.get(day_part)
            if d is not None:
                return num, d
    return None


def _lookup_calendar_event(cfg: dict, now: datetime) -> tuple[str, str] | None:
    """查今天命中哪个 calendar event。返回 (trigger, hint) 或 None。

    优先级：solar_terms > yearly_solar > yearly_lunar。"""
    cal_path = Path(cfg.get("paths", {}).get(
        "calendar_events", str(BASE_DIR / "calendar_events.json")
    ))
    if not cal_path.exists():
        return None
    cal = load_json(cal_path, {})

    today_iso = now.strftime("%Y-%m-%d")
    today_md = now.strftime("%m-%d")

    # 1. solar_terms（完整阳历日期）
    st_entry = cal.get("solar_terms", {}).get(today_iso)
    if isinstance(st_entry, dict):
        hint = (st_entry.get("hint_zh") or "").strip()
        return "event:calendar", hint

    # 2. _yearly_solar（每年阳历 MM-DD）
    yf = cal.get("festivals", {}).get("_yearly_solar", {})
    yf_entry = yf.get(today_md)
    if isinstance(yf_entry, dict):
        special = (yf_entry.get("special") or "").strip()
        if special:
            # festival entry routes to a custom trigger + hint pool
            # (e.g. special: "my_birthday" → trigger "event:my_birthday",
            #  pool key "my_birthday_hints" in perturbation_imagery)
            pool = cfg.get("perturbation_imagery", {}).get(f"{special}_hints", [])
            hint = random.choice(pool) if pool else (yf_entry.get("hint_zh") or "")
            return f"event:{special}", hint
        return "event:calendar", (yf_entry.get("hint_zh") or "").strip()

    # 3. _yearly_lunar（阴历日期，zhdate 转换）
    yl = cal.get("festivals", {}).get("_yearly_lunar", {})
    if yl:
        try:
            from zhdate import ZhDate
            zd = ZhDate.from_datetime(now)
            lm, ld = zd.lunar_month, zd.lunar_day
        except Exception as e:
            log.warning(f"zhdate lookup failed: {e}")
            lm = ld = None
        if lm is not None:
            for key, entry in yl.items():
                if key.startswith("_"):
                    continue
                parsed = _parse_lunar_date(key)
                if parsed and parsed == (lm, ld) and isinstance(entry, dict):
                    hint = (entry.get("hint_zh") or "").strip()
                    return "event:calendar", hint

    return None


def calendar_event_tick(cfg: dict, last_run: dict, now: datetime, blocked_this_tick: bool) -> tuple[str | None, str | None, str | None]:
    """每日 00:30 检查节气/节日。同日不重复 fire。
    返回 (trigger, kind, hint_override) 或 (None, None, None)。
    blocked_this_tick（manual fire 过）→ 让位且当日放弃。"""
    ce_cfg = cfg.get("calendar_event", {})
    if not ce_cfg.get("enabled", True):
        return None, None, None

    # 00:30-00:59 时间窗（给 30 分钟容差，避开 0 点其他系统活动）
    if not (now.hour == 0 and now.minute >= 30):
        return None, None, None

    today_iso = now.strftime("%Y-%m-%d")

    # 跨年清空 fired_dates
    fired = list(last_run.get("calendar_fired_dates", []))
    if fired and fired[-1][:4] != now.strftime("%Y"):
        fired = []

    if today_iso in fired:
        return None, None, None

    hit = _lookup_calendar_event(cfg, now)
    if not hit:
        return None, None, None
    trigger, hint = hit

    if blocked_this_tick:
        fired.append(today_iso)
        last_run["calendar_fired_dates"] = fired
        save_last_run(last_run)
        log.info(f"calendar_event yielded to manual on {today_iso} (would have fired {trigger})")
        return None, None, None

    fired.append(today_iso)
    if len(fired) > 400:
        fired = fired[-400:]
    last_run["calendar_fired_dates"] = fired
    save_last_run(last_run)
    return trigger, "calendar", hint


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

async def main():
    check_single_instance()

    cfg = load_config()
    notifier = load_notifier(cfg, logger=log)
    try:
        await notifier.setup()
    except Exception as e:
        log.error(f"Notifier setup failed: {e}", exc_info=True)
        return
    log.info(f"Notifier ready: {type(notifier).__name__}")

    tick_count = 0

    while True:
        tick_count += 1
        try:
            # ----- Keepalive (notifier-specific; many implementations no-op) -----
            try:
                await notifier.keepalive()
            except Exception as e:
                log.warning(f"notifier.keepalive failed: {e}")

            cfg = load_config()
            schedule = load_schedule()
            last_run = load_last_run()
            now = datetime.now()

            # Priority (same-tick yield): manual > weather > calendar
            #                             > morning_news > mail > scheduled
            fired_this_tick: set[str] = set()

            def _hb_task():
                return next(
                    (t for t in schedule.get("tasks", []) if t["id"] == "heartbeat"),
                    None,
                )

            # 1. Manual wake (highest priority)
            if check_manual_wake(cfg):
                hb = _hb_task()
                if hb:
                    log.info("manual_wake.flag detected -> fire manual")
                    await do_fire(notifier, hb, "manual", "manual", cfg, last_run)
                    fired_this_tick.add("manual")
                else:
                    log.warning("manual_wake flagged but no heartbeat task in schedule.json")

            # 2. Weather detector
            try:
                trigger, kind = await weather_detector_tick(cfg)
                if trigger:
                    hb = _hb_task()
                    if hb:
                        await do_fire(notifier, hb, trigger, kind, cfg, last_run)
                        fired_this_tick.add(trigger)
            except Exception as e:
                log.warning(f"weather_detector_tick failed: {e}")

            # 3. Calendar event (yields to manual)
            try:
                cal_blocked = "manual" in fired_this_tick
                trigger, kind, hint_over = calendar_event_tick(cfg, last_run, now, cal_blocked)
                if trigger:
                    hb = _hb_task()
                    if hb:
                        await do_fire(notifier, hb, trigger, kind, cfg, last_run, hint_override=hint_over)
                        fired_this_tick.add(trigger)
            except Exception as e:
                log.warning(f"calendar_event_tick failed: {e}", exc_info=True)

            # 4. Morning news (yields to manual / weather)
            try:
                mn_blocked = ("manual" in fired_this_tick) or any(
                    t.startswith("event:weather") for t in fired_this_tick
                )
                trigger, kind, hint_over = morning_news_tick(cfg, last_run, now, mn_blocked)
                if trigger:
                    hb = _hb_task()
                    if hb:
                        await do_fire(notifier, hb, trigger, kind, cfg, last_run, hint_override=hint_over)
                        fired_this_tick.add(trigger)
            except Exception as e:
                log.warning(f"morning_news_tick failed: {e}", exc_info=True)

            # 5. Mail detector
            try:
                trigger, kind = await mail_detector_tick(cfg)
                if trigger:
                    hb = _hb_task()
                    if hb:
                        await do_fire(notifier, hb, trigger, kind, cfg, last_run)
                        fired_this_tick.add(trigger)
            except Exception as e:
                log.warning(f"mail_detector_tick failed: {e}")

            # 6. Regular scheduled tasks (cron + next_run)
            schedule = load_schedule()  # reread in case earlier fires changed it
            for task in schedule.get("tasks", []):
                fire, reason = should_fire(task, last_run, now)
                if tick_count % 20 == 1:
                    log.info(f"Tick {tick_count} [{task['id']}]: fire={fire} ({reason})")
                if not fire:
                    continue
                await do_fire(notifier, task, "scheduled", None, cfg, last_run)
                schedule = load_schedule()
                consume_next_run(task, schedule, cfg)

            # Heartbeat next_run seeding fallback
            schedule = load_schedule()
            changed = False
            for t in schedule.get("tasks", []):
                if t["id"] == "heartbeat" and not t.get("next_run") and not t.get("cron"):
                    t["next_run"] = compute_next_heartbeat_run(cfg)
                    log.info(f"heartbeat seeded next_run={t['next_run']}")
                    changed = True
            if changed:
                save_schedule(schedule)

        except asyncio.TimeoutError:
            log.error("Send timed out, will retry next tick")
        except Exception as e:
            log.error(f"Tick error: {e}", exc_info=True)

        for h in log.handlers:
            h.flush()

        await asyncio.sleep(30)


if __name__ == "__main__":
    RESTART_DELAY = 30
    while True:
        try:
            asyncio.run(main())
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Scheduler crashed: {e}, restarting in {RESTART_DELAY}s...", exc_info=True)
            import time
            time.sleep(RESTART_DELAY)
        finally:
            if PID_FILE.exists():
                PID_FILE.unlink(missing_ok=True)
