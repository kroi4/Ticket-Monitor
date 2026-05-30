"""Background monitoring: check subscriptions and send Telegram alerts."""
import logging
import asyncio
from telegram import Bot
import db
import tm_api

log = logging.getLogger(__name__)


async def run_monitor(bot: Bot):
    log.info("Monitor: running check cycle")
    subs = db.get_all_active_subscriptions()
    if not subs:
        return

    # Group by event_code to minimize API calls
    by_event: dict[str, list] = {}
    for sub in subs:
        by_event.setdefault(sub.event_code, []).append(sub)

    for event_code, event_subs in by_event.items():
        try:
            await _check_event(bot, event_code, event_subs)
        except Exception as exc:
            log.error("Monitor error for event %s: %s", event_code, exc, exc_info=True)


async def _check_event(bot: Bot, event_code: str, subs: list):
    performances = tm_api.get_performances(event_code)
    perf_map = {p["perf_code"]: p for p in performances}

    # Group subs by perf_code (None = all perfs)
    for sub in subs:
        target_perfs = (
            [perf_map[sub.perf_code]]
            if sub.perf_code and sub.perf_code in perf_map
            else list(perf_map.values())
        )
        for perf in target_perfs:
            if perf["is_soldout"]:
                continue
            prices = tm_api.get_prices(event_code, perf["perf_code"])
            matching = _find_matching(prices, sub)
            alert_key = _build_key(matching)

            if alert_key and alert_key != sub.last_alert_key:
                chat_id = sub.effective_chat_id()
                if chat_id:
                    await _send_alert(bot, chat_id, sub, perf, matching)
                    db.update_alert_key(sub.id, alert_key)
            elif not alert_key and sub.last_alert_key:
                # Tickets gone → reset so we can notify again when they return
                db.update_alert_key(sub.id, "")


def _find_matching(prices: list[dict], sub) -> list[dict]:
    """Return prices that satisfy the subscription — filtered by price ceiling only."""
    result = []
    for p in prices:
        if sub.max_price_ils and p["price_ils"] > sub.max_price_ils:
            continue
        result.append(p)
    return result


def _build_key(matching: list[dict]) -> str:
    if not matching:
        return ""
    parts = [f"{m['code']}:{m['price_ils']:.0f}:{m.get('count') or 0}" for m in matching]
    return ",".join(sorted(parts))


async def _send_alert(bot: Bot, chat_id: str, sub, perf: dict, matching: list[dict]):
    event_name = sub.event_name or sub.event_code
    date_str   = perf["date_str"]
    buy_url    = perf["buy_url"]
    emoji, status_label = perf["emoji"], perf["status_label"]

    lines = [
        f"🎟️ <b>{event_name}</b>",
        f"📅 {date_str}  {emoji} {status_label}",
        "",
        f"נמצאו <b>{len(matching)}</b> סוגי כרטיס:",
        "",
    ]
    for m in matching:
        cnt  = f" · {m['count']} מקומות" if m.get("count") else ""
        lines.append(f"• {m['description']} — <b>{m['price_ils']:.0f} ₪</b>{cnt}")

    max_label = f"עד {sub.max_price_ils:.0f} ₪" if sub.max_price_ils else "כל מחיר"
    lines += [
        "",
        f"<i>פילטר: {max_label}</i>",
        "",
        f'🛒 <a href="{buy_url}">לרכישת כרטיסים</a>',
    ]

    try:
        await bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        log.info("Alert sent to %s for %s/%s", chat_id, sub.event_code, perf["perf_code"])
    except Exception as exc:
        log.warning("Failed to send alert to %s: %s", chat_id, exc)
