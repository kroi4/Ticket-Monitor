"""Background monitoring: check subscriptions and send Telegram alerts."""
import logging
import asyncio
import time
from telegram import Bot
import config
import db
import tm_api
import mailer

log = logging.getLogger(__name__)

_last_checked: dict[int, float] = {}   # sub_id → unix timestamp of last check
_fetch_error_notified: set[str] = set()  # event_codes already notified for fetch failures


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

    if not performances:
        perf_path  = f"getPerformanceList/{event_code}/{config.TM_CHANNEL}/{config.TM_LANG}"
        fail_count = tm_api.get_error_count(perf_path)
        if fail_count >= 3 and event_code not in _fetch_error_notified:
            _fetch_error_notified.add(event_code)
            await _notify_fetch_error(bot, event_code, subs)
            log.warning("Fetch failures for event %s: %d consecutive", event_code, fail_count)
        return

    _fetch_error_notified.discard(event_code)
    perf_map = {p["perf_code"]: p for p in performances}

    for sub in subs:
        # Per-user interval: respect user's chosen check frequency
        user_interval = (
            sub.user.check_interval_seconds
            if sub.user and sub.user.check_interval_seconds
            else config.CHECK_INTERVAL_SECONDS
        )
        last = _last_checked.get(sub.id, 0)
        if time.time() - last < user_interval:
            continue
        _last_checked[sub.id] = time.time()

        target_perfs = (
            [perf_map[sub.perf_code]]
            if sub.perf_code and sub.perf_code in perf_map
            else list(perf_map.values())
        )
        for perf in target_perfs:
            if perf["is_soldout"]:
                continue
            prices    = tm_api.get_prices(event_code, perf["perf_code"])
            matching  = _find_matching(prices, sub)
            alert_key = _build_key(matching)

            if alert_key and alert_key != sub.last_alert_key:
                chat_id      = sub.effective_chat_id()
                event_detail = tm_api.get_event_detail(event_code)

                # Telegram notification
                notify_tg = (not sub.user) or (sub.user.notify_telegram is not False)
                if notify_tg and chat_id:
                    await _send_alert(bot, chat_id, sub, perf, matching)

                # Email notification
                notify_email = sub.user and sub.user.notify_email is not False
                if notify_email:
                    email = db.get_email_for_sub(sub)
                    if email:
                        await mailer.send_ticket_alert(email, sub, perf, matching, event_detail)

                db.log_alert_event(sub, "available", matching, perf)
                db.update_alert_key(sub.id, alert_key)

            elif not alert_key and sub.last_alert_key:
                db.log_alert_event(sub, "unavailable", [], perf)
                db.update_alert_key(sub.id, "")


async def _notify_fetch_error(bot: Bot, event_code: str, subs: list):
    """Notify subscribed users that repeated API calls are failing for an event."""
    event_name = next((s.event_name for s in subs if s.event_name), event_code)
    notified: set[str] = set()
    for sub in subs:
        chat_id = sub.effective_chat_id()
        if chat_id and chat_id not in notified:
            notified.add(chat_id)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚠️ <b>שגיאה בקבלת נתונים</b>\n\n"
                        f"לא ניתן לקבל מידע מ-Ticketmaster עבור:\n"
                        f"🎭 <b>{event_name}</b>\n\n"
                        f"נמשיך לנסות אוטומטית. "
                        f"מומלץ לבדוק גם ישירות באתר Ticketmaster."
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.warning("Failed to send fetch-error notice to %s: %s", chat_id, exc)


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
    parts = [f"{m['code']}:{m['price_ils']:.0f}" for m in matching]
    return ",".join(sorted(parts))


async def _send_alert(bot: Bot, chat_id: str, sub, perf: dict, matching: list[dict]):
    event_name  = sub.event_name or sub.event_code
    date_str    = perf["date_str"]
    buy_url     = perf["buy_url"]
    emoji, status_label = perf["emoji"], perf["status_label"]
    dow         = tm_api.dow_he(date_str)
    date_display = f"{date_str} ({dow})" if dow else date_str

    lines = [
        f"🎟️ <b>{event_name}</b>",
        f"📅 {date_display}  {emoji} {status_label}",
        "",
        f"נמצאו <b>{len(matching)}</b> סוגי כרטיס:",
        "",
    ]
    for m in matching:
        lines.append(f"• {m['description']} — <b>{m['price_ils']:.0f} ₪</b>")

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
