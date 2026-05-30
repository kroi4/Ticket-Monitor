"""Ticketmaster Israel API wrapper."""
import time
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import requests
import config

ISRAEL_TZ = timezone(timedelta(hours=3))

_cache: dict = {}
_CACHE_TTL = 90   # seconds


def _get(path: str) -> dict | list | None:
    url    = f"{config.TM_BASE}/{path}"
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        r = requests.get(url, headers=config.TM_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        _cache[url] = (time.time(), data)
        return data
    except Exception:
        return None


def _inner(resp) -> dict | list | None:
    if isinstance(resp, dict):
        return resp.get("data")
    return resp


def ts_to_str(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=ISRAEL_TZ)
    return dt.strftime("%d/%m/%Y %H:%M")


STATUS_LABELS = {
    "s01_available":       ("🟢", "זמין"),
    "s18_low_availability":("🟡", "זמינות נמוכה"),
    "s02_soldout":         ("🔴", "אזל מהמלאי"),
}

def status_label(status: str) -> tuple[str, str]:
    return STATUS_LABELS.get(status, ("⚫", status or "לא ידוע"))


# ── Public API ────────────────────────────────────────────

def get_all_events() -> list[dict]:
    """Return list of top events from homepage."""
    data = _inner(_get(f"getAllTopEvent/{config.TM_LANG}"))
    if not isinstance(data, list):
        return []
    result = []
    for e in data:
        if not e.get("btxEventId"):
            continue
        result.append({
            "event_code":  e["btxEventId"],
            "name":        e.get("eventName") or e["btxEventId"],
            "venue_name":  e.get("venueName", ""),
            "venue_city":  (e.get("venueCity") or "").strip(),
            "first_date":  ts_to_str(e["firstPerformanceDate"]) if e.get("firstPerformanceDate") else "",
            "last_date":   ts_to_str(e["lastPerformanceDate"])  if e.get("lastPerformanceDate") else "",
            "small_image": e.get("smallImage", ""),
            "image_url":   f"{config.TM_IMG_BASE}/{e['smallImage']}" if e.get("smallImage") else "",
            "redirect":    e.get("redirectNewWebsite", False),
        })
    result.sort(key=lambda e: (
        datetime.strptime(e["first_date"], "%d/%m/%Y %H:%M")
        if e["first_date"] else datetime.max.replace(tzinfo=None)
    ))
    return result


def get_event_detail(event_code: str) -> dict | None:
    data = _inner(_get(f"getEventDetail/{event_code}/{config.TM_CHANNEL}/{config.TM_LANG}"))
    if not isinstance(data, dict):
        return None
    return {
        "event_code":  data.get("eventCode", event_code),
        "name":        data.get("eventName", event_code),
        "venue_name":  data.get("venueName", ""),
        "description": data.get("eventDescription", ""),
        "image_url":   f"{config.TM_IMG_BASE}/{data['smallImage']}" if data.get("smallImage") else "",
    }


def get_performances(event_code: str) -> list[dict]:
    data = _inner(_get(f"getPerformanceList/{event_code}/{config.TM_CHANNEL}/{config.TM_LANG}"))
    if not isinstance(data, list):
        return []
    result = []
    for p in data:
        code   = p.get("performanceCode", "")
        status = p.get("status", "")
        emoji, label = status_label(status)
        result.append({
            "perf_code":  code,
            "status":     status,
            "emoji":      emoji,
            "status_label": label,
            "date_str":   ts_to_str(p["performanceDate"]) if p.get("performanceDate") else "",
            "venue_name": p.get("venueName", ""),
            "is_soldout": "soldout" in status.lower(),
            "buy_url":    f"https://www.ticketmaster.co.il/performance/{event_code}/{code}/ALL/{config.TM_LANG}",
        })
    return result


def get_all_ticket_types(event_code: str) -> list[dict]:
    """Aggregate unique ticket types across all performances of an event.
    Needed because sold-out types don't appear in a specific performance's price list."""
    perfs = get_performances(event_code)
    seen: dict[str, dict] = {}
    for perf in perfs:
        for p in get_prices(event_code, perf["perf_code"]):
            if p["code"] not in seen:
                seen[p["code"]] = p
    result = list(seen.values())
    result.sort(key=lambda x: x["price_ils"])
    return result


def get_prices(event_code: str, perf_code: str) -> list[dict]:
    data = _inner(_get(f"getPriceByProfiles/{event_code}/{perf_code}/{config.TM_CHANNEL}/{config.TM_LANG}"))
    prices = []
    if isinstance(data, dict):
        all_items = [item for lst in data.values() if isinstance(lst, list) for item in lst]
    elif isinstance(data, list):
        all_items = data
    else:
        return prices

    seen_codes = set()
    for item in all_items:
        code = item.get("code") or item.get("priceLevel") or ""
        if code in seen_codes:
            continue
        seen_codes.add(code)
        raw = item.get("value") or 0
        try:
            price_ils = int(raw) / 100
        except (TypeError, ValueError):
            continue
        desc   = (item.get("description") or "לא ידוע").strip()
        blocks = item.get("blocks") or []
        block_names = ", ".join(
            (b.get("description") or "").strip() for b in blocks if b
        )
        prices.append({
            "code":        code,
            "price_ils":   price_ils,
            "description": desc,
            "blocks":      block_names,
        })
    prices.sort(key=lambda x: x["price_ils"])
    return prices
