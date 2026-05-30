"""Telegram bot — interactive ticket monitor."""
import logging
from datetime import datetime
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError
import config
import db
import tm_api
import mailer

log = logging.getLogger(__name__)


# ── Pinned summary ────────────────────────────────────────

def _build_summary_text(subs: list) -> str:
    if not subs:
        return "📋 <b>אין התראות פעילות כרגע.</b>\n\nפתח /start כדי להוסיף מעקב."
    lines = ["📋 <b>ההתראות הפעילות שלך:</b>\n"]
    for sub in subs:
        if sub.max_price_ils and sub.ticket_desc:
            price = f"{sub.ticket_desc} ומטה (עד {sub.max_price_ils:.0f}₪)"
        elif sub.max_price_ils:
            price = f"עד {sub.max_price_ils:.0f}₪"
        else:
            price = "כל המחירים"
        if sub.perf_date:
            dow      = tm_api.dow_he(sub.perf_date)
            date_str = f"{sub.perf_date} ({dow})"
        else:
            date_str = "כל התאריכים"
        lines.append(
            f"🎭 <b>{sub.event_name or sub.event_code}</b>\n"
            f"  📅 {date_str}\n"
            f"  💰 {price}"
        )
    updated = datetime.now().strftime("%d/%m %H:%M")
    lines.append(f"\n<i>עודכן: {updated}</i>")
    return "\n\n".join(lines)


async def update_pinned_summary(bot: Bot, chat_id: str):
    """Send or edit the pinned summary message for this chat."""
    subs   = db.get_subscriptions_for_chat(chat_id)
    text   = _build_summary_text(subs)
    pin_id = db.get_pinned_message_id(chat_id)

    if pin_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=pin_id,
                text=text, parse_mode="HTML",
            )
            return
        except TelegramError:
            pass  # message deleted or inaccessible → send a new one

    msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    db.set_pinned_message_id(chat_id, msg.message_id)
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except TelegramError as e:
        log.warning("Could not pin message in %s: %s", chat_id, e)

# ── Callback data protocol ─────────────────────────────────
# ev:{event_code}                              → show performances
# pf:{event_code}:{perf_code}                  → show ticket types
# sub:{event_code}:{perf_code}:{code}:{price}  → create/update subscription (price=0 for any)
# cancel:{sub_id}:{event_code}:{perf_code}     → delete sub and refresh ticket types
# del:{sub_id}                                 → delete subscription (from alerts screen)
# back:events                                  → go back to event list
# back:ev:{event_code}                         → go back to performances
# alerts                                       → show my subscriptions
# link                                         → show linking instructions


def _chat_id(update: Update) -> str:
    return str(update.effective_chat.id)


def _username(update: Update) -> str | None:
    u = update.effective_user
    return u.username if u else None


async def _edit_or_reply(update: Update, text: str, markup=None, **kwargs):
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode="HTML", **kwargs
        )
    else:
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode="HTML", **kwargs
        )


# ── /start ────────────────────────────────────────────────

# ── Ticket sources registry ───────────────────────────────
# To add a new source: add an entry here and wire up its API call in _get_events().
SOURCES: dict[str, dict] = {
    "ticketmaster": {"label": "Ticketmaster", "emoji": "🎫"},
}


def _get_events(source: str) -> list:
    if source == "ticketmaster":
        return tm_api.get_all_events()
    return []


async def _show_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = _chat_id(update)
    user    = db.get_user_by_chat_id(chat_id)

    keyboard = [
        [InlineKeyboardButton(f"{s['emoji']} {s['label']}", callback_data=f"src:{key}")]
        for key, s in SOURCES.items()
    ]
    keyboard.append([InlineKeyboardButton("📋 ההתראות שלי", callback_data="alerts")])

    if user and user.email:
        link_label = f"✅ {user.email}"
    else:
        link_label = "🔗 חיבור לחשבון אתר"
    keyboard.append([InlineKeyboardButton(link_label, callback_data="link")])

    await _edit_or_reply(
        update,
        f"🎟️ <b>מעקב כרטיסי הופעות</b>\n\nבחר מקור כרטיסים:\n\n"
        f"🌐 {config.WEB_BASE_URL}",
        InlineKeyboardMarkup(keyboard),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args and args[0].startswith("link_"):
        await _handle_link(update, context, args[0][5:])
        return
    await _show_sources(update, context)


_DAYS_HE = ["ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "ש׳", "א׳"]  # weekday() 0=Mon … 6=Sun


def _dow_he(date_str: str) -> str:
    """Hebrew day-of-week abbreviation from 'DD/MM/YYYY HH:MM'."""
    try:
        d = datetime.strptime(date_str.split()[0], "%d/%m/%Y")
        return _DAYS_HE[d.weekday()]
    except Exception:
        return ""


def _event_label(e: dict) -> str:
    """Single-line button: 'Name · City (DD/MM(א) – DD/MM(ב) HH:MM)'."""
    city  = f" · {e['venue_city']}" if e.get("venue_city") else ""

    first = e.get("first_date", "")
    last  = e.get("last_date",  "")
    if not first:
        return f"{e['name']}{city}"

    def _parts(d: str):
        p = d.split(" ")
        return p[0][:5], (p[1] if len(p) > 1 else ""), _dow_he(d)

    fd, ft, fdow = _parts(first)
    ld, lt, ldow = _parts(last) if last else (fd, ft, fdow)

    date_part = f"{fd} ({fdow}) – {ld} ({ldow})" if fd != ld else f"{fd} ({fdow})"
    time_part = f"  {ft}" if ft else ""
    return f"{e['name']}{city}  ·  {date_part}{time_part}"


async def _show_events(update: Update, context: ContextTypes.DEFAULT_TYPE, source: str = "ticketmaster"):
    context.user_data["source"] = source
    events = _get_events(source)
    if not events:
        await _edit_or_reply(update, "⚠️ לא ניתן לטעון אירועים כרגע, נסה שוב מאוחר יותר.")
        return

    src_label = SOURCES.get(source, {}).get("label", source)
    now = datetime.now().strftime("%d/%m %H:%M")

    chat_id = _chat_id(update)
    user_subs = db.get_subscriptions_for_chat(chat_id)
    subscribed_events = {sub.event_code for sub in user_subs}

    keyboard = []
    for e in events:
        prefix = "🔔 " if e["event_code"] in subscribed_events else ""
        keyboard.append([InlineKeyboardButton(prefix + _event_label(e), callback_data=f"ev:{e['event_code']}")])
    keyboard.append([InlineKeyboardButton("🔙 חזרה", callback_data="back:sources")])

    header = (
        f"🎫 <b>{src_label}</b>\n"
        f"<i>⏱ עודכן: {now}</i>\n"
        f"<i>תאריכים(יום) שעת-הופעה</i>\n\n"
        f"בחר הופעה:"
    )
    await _edit_or_reply(update, header, InlineKeyboardMarkup(keyboard))


# ── Event → Performances ─────────────────────────────────

async def _show_performances(update: Update, context: ContextTypes.DEFAULT_TYPE, event_code: str):
    q = update.callback_query
    detail = tm_api.get_event_detail(event_code)
    perfs  = tm_api.get_performances(event_code)
    name   = detail["name"] if detail else event_code

    chat_id = _chat_id(update)
    linked_user = db.get_user_by_chat_id(chat_id)
    user_id = linked_user.id if linked_user else None

    keyboard = []
    for p in perfs:
        dow    = tm_api.dow_he(p["date_str"])
        sub    = db.get_sub_for_perf(chat_id, user_id, event_code, p["perf_code"])
        prefix = "🔔 " if sub else ""
        label  = f"{prefix}{p['emoji']} {p['date_str']} ({dow}) — {p['status_label']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"pf:{event_code}:{p['perf_code']}")])
    keyboard.append([InlineKeyboardButton("🔙 חזרה", callback_data="back:events")])

    await q.edit_message_text(
        f"🎟️ <b>{name}</b>\nבחר תאריך:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


# ── Performance → Ticket types (= price ceiling selection) ──

async def _show_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        event_code: str, perf_code: str):
    q      = update.callback_query
    detail = tm_api.get_event_detail(event_code)
    perfs  = tm_api.get_performances(event_code)
    perf   = next((p for p in perfs if p["perf_code"] == perf_code), None)

    # Current performance prices (only available types)
    current_prices  = tm_api.get_prices(event_code, perf_code)
    current_codes   = {p["code"] for p in current_prices}
    # All ticket types known for this event (including sold-out in this performance)
    all_types       = tm_api.get_all_ticket_types(event_code)

    name   = detail["name"] if detail else event_code
    date   = perf["date_str"] if perf else perf_code
    emoji  = perf["emoji"] if perf else "🎟️"
    slabel = perf["status_label"] if perf else ""

    # Check for existing subscription on this specific performance
    chat_id     = _chat_id(update)
    linked_user = db.get_user_by_chat_id(chat_id)
    user_id     = linked_user.id if linked_user else None
    existing_sub = db.get_sub_for_perf(chat_id, user_id, event_code, perf_code, include_all_perfs=False)

    keyboard = []
    for p in all_types:
        available   = p["code"] in current_codes
        is_selected = (
            existing_sub is not None
            and existing_sub.max_price_ils is not None
            and abs(p["price_ils"] - existing_sub.max_price_ils) < 0.5
        )
        if available:
            label = f"{p['description']} ({p['price_ils']:.0f}₪)"
        else:
            label = f"{p['description']} 🔴 אזל ({p['price_ils']:.0f}₪)"
        if is_selected:
            label = "✅ " + label
        keyboard.append([InlineKeyboardButton(
            label,
            callback_data=f"sub:{event_code}:{perf_code}:{p['code']}:{int(p['price_ils'])}"
        )])
    is_all_selected = existing_sub is not None and existing_sub.max_price_ils is None
    all_label = "✅ 📊 כל המחירים" if is_all_selected else "📊 כל המחירים"
    keyboard.append([InlineKeyboardButton(
        all_label,
        callback_data=f"sub:{event_code}:{perf_code}:ALL:0"
    )])
    if existing_sub:
        keyboard.append([InlineKeyboardButton(
            "🗑️ בטל מעקב",
            callback_data=f"cancel:{existing_sub.id}:{event_code}:{perf_code}"
        )])
    if perf and perf.get("buy_url"):
        keyboard.append([InlineKeyboardButton("🔗 דף ההופעה", url=perf["buy_url"])])
    keyboard.append([InlineKeyboardButton("🔙 חזרה", callback_data=f"back:ev:{event_code}")])

    status_line  = f"{emoji} {slabel}" if slabel else ""
    hint         = "\n\n💡 <i>בחירת כרטיס יקר כוללת גם כרטיסים זולים ממנו</i>" if all_types else ""
    soldout_note = "\n<i>🔴 = אזל בתאריך זה — ניתן עדיין להגדיר מעקב</i>" if any(p["code"] not in current_codes for p in all_types) else ""
    sub_note     = "\n\n🔔 <i>יש לך מעקב פעיל להופעה זו</i>" if existing_sub else ""

    await q.edit_message_text(
        f"🎟️ <b>{name}</b>\n📅 {date}  {status_line}{sub_note}{hint}{soldout_note}\n\n"
        f"בחר את <b>תקרת המחיר</b> שמעניינת אותך:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


# ── Create subscription ──────────────────────────────────

async def _create_sub(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      event_code: str, perf_code: str, ticket_code: str,
                      max_price_raw: str):
    q = update.callback_query
    chat_id = _chat_id(update)

    max_price = float(max_price_raw) if max_price_raw not in ("0", "", "0.0") else None
    perfs     = tm_api.get_performances(event_code)
    perf      = next((p for p in perfs if p["perf_code"] == perf_code), None)
    detail    = tm_api.get_event_detail(event_code)
    all_types = tm_api.get_all_ticket_types(event_code)
    ticket    = next((p for p in all_types if p["code"] == ticket_code), None)

    event_name  = detail["name"] if detail else event_code
    perf_date   = perf["date_str"] if perf else perf_code
    # ticket_desc = display label for the selected ceiling tier
    ticket_desc = ticket["description"] if ticket and ticket_code != "ALL" else None

    linked_user = db.get_user_by_chat_id(chat_id)
    sub = db.create_subscription(
        telegram_chat_id=chat_id,
        event_code=event_code,
        event_name=event_name,
        perf_code=perf_code,
        perf_date=perf_date,
        ticket_desc=ticket_desc,
        ticket_code=None,          # filtering is by price only, not by specific code
        max_price_ils=max_price,
        user_id=linked_user.id if linked_user else None,
    )

    if max_price:
        if ticket_desc:
            price_label = f"{ticket_desc} ומטה (עד {max_price:.0f}₪)"
        else:
            price_label = f"עד {max_price:.0f}₪"
    else:
        price_label = "כל המחירים"

    if perf_date:
        dow = tm_api.dow_he(perf_date)
        perf_date_display = f"{perf_date} ({dow})"
    else:
        perf_date_display = perf_date

    keyboard = [
        [InlineKeyboardButton("📋 ההתראות שלי", callback_data="alerts")],
        [InlineKeyboardButton("🔙 חזרה להופעות", callback_data=f"back:ev:{event_code}")],
        [InlineKeyboardButton("🏠 ראשי", callback_data="back:sources")],
    ]

    text = (
        f"✅ <b>מעקב הוגדר!</b>\n\n"
        f"🎭 {event_name}\n"
        f"📅 {perf_date_display}\n"
        f"💰 {price_label}\n\n"
        f"תקבל התראה כשיהיו כרטיסים מתאימים."
    )

    if q:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    context.user_data.pop("pending", None)
    context.user_data.pop("awaiting_price", None)

    await update_pinned_summary(context.bot, chat_id)

    # Email confirmation if user has a linked Google account
    email = db.get_email_for_sub(sub)
    if email:
        all_subs = db.get_subscriptions_for_chat(chat_id)
        await mailer.send_subscription_confirmed(email, sub, all_subs)


# ── My alerts ────────────────────────────────────────────

async def _show_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = _chat_id(update)
    subs = db.get_subscriptions_for_chat(chat_id)

    keyboard = []
    if subs:
        lines = ["📋 <b>ההתראות הפעילות שלך:</b>\n"]
        for sub in subs:
            price_label  = f"עד {sub.max_price_ils:.0f}₪" if sub.max_price_ils else "כל מחיר"
            ticket_label = sub.ticket_desc or "כל הכרטיסים"
            if sub.perf_date:
                dow        = tm_api.dow_he(sub.perf_date)
                date_label = f"{sub.perf_date} ({dow})"
            else:
                date_label = "כל התאריכים"
            lines.append(f"• <b>{sub.event_name}</b> | {date_label} | {ticket_label} | {price_label}")
            keyboard.append([InlineKeyboardButton(
                f"🗑️ {sub.event_name} {date_label}",
                callback_data=f"del:{sub.id}"
            )])
    else:
        lines = ["📋 <b>אין לך התראות פעילות.</b>\n\nהתחל בבחירת הופעה מהרשימה הראשית."]

    keyboard.append([InlineKeyboardButton("🏠 ראשי", callback_data="back:sources")])

    if q:
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )


# ── Link web account ─────────────────────────────────────

async def _show_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = _chat_id(update)
    user = db.get_user_by_chat_id(chat_id)

    if user:
        text = (
            f"✅ <b>החשבון שלך מקושר!</b>\n\n"
            f"👤 {user.name or user.email}\n"
            f"📧 {user.email}\n\n"
            f"ההתראות שלך מסונכרנות עם לוח הבקרה באתר."
        )
        keyboard = [[InlineKeyboardButton("🏠 ראשי", callback_data="back:sources")]]
    else:
        web_url = f"{config.WEB_BASE_URL}/link-telegram/{chat_id}"
        text = (
            "🔗 <b>חיבור לחשבון האתר</b>\n\n"
            "לחץ על הקישור, התחבר עם Google והחיבור יושלם <b>אוטומטית</b>:\n\n"
            f"{web_url}"
        )
        keyboard = [
            [InlineKeyboardButton("🏠 ראשי", callback_data="back:sources")],
        ]

    if q:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def _handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    user = db.use_link_token(token)
    if not user:
        await update.message.reply_text("❌ הקישור לא תקין או פג תוקף. נסה שוב מהאתר.")
        return
    chat_id  = _chat_id(update)
    username = _username(update)
    db.link_telegram(user.id, chat_id, username)
    await update.message.reply_text(
        f"✅ <b>החשבון קושר בהצלחה!</b>\n\n"
        f"👤 {user.name or user.email}\n\n"
        f"עכשיו תוכל לנהל את ההתראות שלך גם מהאתר וגם מהבוט.",
        parse_mode="HTML",
    )
    await _show_events(update, context)


# ── Handlers ─────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "back:sources":
        await _show_sources(update, context)

    elif data == "back:events":
        source = context.user_data.get("source", "ticketmaster")
        await _show_events(update, context, source)

    elif data.startswith("src:"):
        await _show_events(update, context, source=data[4:])

    elif data.startswith("back:ev:"):
        event_code = data[8:]
        await _show_performances(update, context, event_code)

    elif data == "alerts":
        await _show_alerts(update, context)

    elif data == "link":
        await _show_link(update, context)

    elif data.startswith("ev:"):
        await _show_performances(update, context, data[3:])

    elif data.startswith("pf:"):
        _, event_code, perf_code = data.split(":", 2)
        await _show_tickets(update, context, event_code, perf_code)

    elif data.startswith("sub:"):
        parts = data.split(":")
        _, event_code, perf_code, ticket_code, max_price = parts[0], parts[1], parts[2], parts[3], parts[4]
        await _create_sub(update, context, event_code, perf_code, ticket_code, max_price)

    elif data.startswith("del:"):
        sub_id  = int(data[4:])
        chat_id = _chat_id(update)
        deleted = db.delete_subscription(sub_id, owner_chat_id=chat_id)
        if deleted:
            await q.answer("✅ ההתראה נמחקה", show_alert=False)
            await update_pinned_summary(context.bot, chat_id)
        await _show_alerts(update, context)

    elif data.startswith("cancel:"):
        parts      = data.split(":")
        sub_id     = int(parts[1])
        event_code = parts[2]
        perf_code  = parts[3]
        chat_id    = _chat_id(update)
        deleted    = db.delete_subscription(sub_id, owner_chat_id=chat_id)
        if deleted:
            await q.answer("✅ המעקב בוטל", show_alert=False)
            await update_pinned_summary(context.bot, chat_id)
        await _show_tickets(update, context, event_code, perf_code)

    else:
        await q.answer("פעולה לא מוכרת")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text when awaiting max price."""
    if not context.user_data.get("awaiting_price"):
        return
    text = (update.message.text or "").strip()
    try:
        price = float(text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ נא להכניס מספר בלבד (למשל: 350)")
        return

    pending = context.user_data.get("pending", {})
    await _create_sub(
        update, context,
        pending.get("event_code", ""),
        pending.get("perf_code", ""),
        pending.get("ticket_code", "ALL"),
        str(price),
    )


async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_alerts(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎟️ <b>מעקב כרטיסי הופעות</b>\n\n"
        "/start — תפריט ראשי\n"
        "/myalerts — ההתראות שלי\n"
        "/help — עזרה\n\n"
        f"🌐 אתר: {config.WEB_BASE_URL}",
        parse_mode="HTML",
    )


# ── Build application ────────────────────────────────────

def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("myalerts",  cmd_myalerts))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    return app
