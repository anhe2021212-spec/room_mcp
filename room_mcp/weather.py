"""wttr.in 天气拉取 + 文件缓存。失败不抛，返回上次缓存或空串。
v3.1: 输出英文（spec 语言分层：环境氛围用英文）。weather_in/weather_not_in 关键词也用英文。"""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

# (condition_english, is_falling)
# is_falling 控制描述前缀
_COND_MAP = {
    "Clear": ("clear", False),
    "Sunny": ("clear", False),
    "Partly cloudy": ("partly cloudy", False),
    "Cloudy": ("cloudy", False),
    "Overcast": ("overcast", False),
    "Mist": ("misty", False),
    "Fog": ("foggy", False),
    "Patchy rain possible": ("light rain possible", True),
    "Patchy rain nearby": ("light rain nearby", True),
    "Light rain": ("light rain", True),
    "Light rain shower": ("light rain shower", True),
    "Moderate rain": ("rain", True),
    "Heavy rain": ("heavy rain", True),
    "Light snow": ("light snow", True),
    "Moderate snow": ("snow", True),
    "Heavy snow": ("heavy snow", True),
    "Thundery outbreaks possible": ("thunder possible", True),
    "Thundery outbreaks in nearby": ("thunder nearby", True),
}


def _proxy_handler():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        return urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return None


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(path: Path, cache: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def get_weather(city: str, cache_min: int, cache_path: Path) -> str:
    cache = _load_cache(cache_path)
    entry = cache.get(city)
    now_ts = time.time()
    if entry and now_ts - entry.get("ts", 0) < cache_min * 60:
        return entry["text"]

    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%C+%t+%h&m"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        handler = _proxy_handler()
        opener = urllib.request.build_opener(handler) if handler else urllib.request.build_opener()
        with opener.open(req, timeout=8) as r:
            raw = r.read().decode("utf-8").strip()
        text = _to_english(raw)
        cache[city] = {"ts": now_ts, "text": text, "raw": raw}
        _save_cache(cache_path, cache)
        return text
    except Exception:
        if entry:
            return entry["text"]
        return ""


def _to_english(raw: str) -> str:
    """例: 'Light rain +18°C 85%' → 'light rain, 18°C, damp'."""
    parts = raw.split()
    if len(parts) < 3:
        return raw
    hum = parts[-1]
    temp = parts[-2]
    cond_en_raw = " ".join(parts[:-2])
    cond_en, _ = _COND_MAP.get(cond_en_raw, (cond_en_raw.lower(), False))
    temp_clean = temp.replace("+", "").strip()  # 保留 °C
    hum_int = hum.rstrip("%")
    air = ""
    try:
        h = int(hum_int)
        if h >= 80:
            air = "damp air"
        elif h >= 65:
            air = "humid"
        elif h <= 30:
            air = "dry air"
    except ValueError:
        pass
    s = f"{cond_en}, {temp_clean}"
    if air:
        s += f", {air}"
    return s
