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

log = logging.getLogger(__name__)


# ── Pinned summary ────────────────────────────────────────

def _build_summary_text(subs: list) -> str:
    if not subs:
        return "📋 <b>אין התראות פעילות כרגע.</b>\n\nפתח /start כדי להוסיף מעקב."
    lines = ["📋 <b>ההתראות הפעילות שלך:</b>\n"]
    for sub in subs:
        if sub.max_price_ils and sub.ticket_desc:
            price = f"{sub.ticket_desc} ומטה (עד {sub.max_price_ils:.0f} ₪)"
        elif sub.max_price_ils:
            price = f"עד {sub.max_price_ils:.0f} ₪"
        else:
            price = "כל המחירים"
        lines.append(
            f"🎭 <b>{sub.event_name or sub.event_code}</b>\n"
            f"  📅 {sub.perf_date or 'כל התאריכים'}\n"
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
# ev:{event_code}                       → show performances
# pf:{event_code}:{perf_code}           → show ticket types
# tk:{event_code}:{perf_code}:{code}    → ask max price (code=ticket code or ALL)
# sub:{event_code}:{perf_code}:{code}:{price}  → create subscription (price=0 for any)
# del:{sub_id}                          → delete subscription
# back:events                           → go back to event list
# back:ev:{event_code}                  → go back to performances
# alerts                                → show my subscriptions
# link                                  → show linking instructions


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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args and args[0].startswith("link_"):
        await _handle_link(update, context, args[0][5:])
        return
    await _show_events(update, context)


def _event_date_range(e: dict) -> str:
    """Return compact date range string, e.g. '(09/06–11/06 20:15)' or '(09/06 20:15)'."""
    first = e.get("first_date", "")   # "09/06/2026 20:15"
    last  = e.get("last_date",  "")
    if not first:
        return ""
    def _short(d: str):
        parts = d.split(" ")
        day_month = parts[0][:5]   # "09/06"
        time_part = parts[1] if len(parts) > 1 else ""
        return day_month, time_part
    fd, ft = _short(first)
    ld, lt = _short(last) if last else (fd, ft)
    if fd == ld:
        return f"({fd} {ft})"
    return f"({fd}–{ld} {lt})"


async def _show_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    events = tm_api.get_all_events()
    if not events:
        await _edit_or_reply(update, "⚠️ לא ניתן לטעון אירועים כרגע, נסה שוב מאוחר יותר.")
        return

    keyboard = []
    for e in events:
        city  = f" · {e['venue_city']}" if e["venue_city"] else ""
        dates = f" {_event_date_range(e)}" if e.get("first_date") else ""
        label = f"{e['name']}{city}{dates}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ev:{e['event_code']}")])
    keyboard.append([InlineKeyboardButton("📋 ההתראות שלי", callback_data="alerts")])
    keyboard.append([InlineKeyboardButton("🔗 חיבור לחשבון אתר", callback_data="link")])

    await _edit_or_reply(
        update,
        "🎟️ <b>מעקב כרטיסי הופעות</b>\n\nבחר הופעה:",
        InlineKeyboardMarkup(keyboard),
    )


# ── Event → Performances ─────────────────────────────────

async def _show_performances(update: Update, context: ContextTypes.DEFAULT_TYPE, event_code: str):
    q = update.callback_query
    detail = tm_api.get_event_detail(event_code)
    perfs  = tm_api.get_performances(event_code)
    name   = detail["name"] if detail else event_code

    keyboard = []
    for p in perfs:
        label = f"{p['emoji']} {p['date_str']} — {p['status_label']}"
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
    prices = tm_api.get_prices(event_code, perf_code)

    name   = detail["name"] if detail else event_code
    date   = perf["date_str"] if perf else perf_code
    emoji  = perf["emoji"] if perf else "🎟️"
    slabel = perf["status_label"] if perf else ""

    keyboard = []
    for p in prices:
        # Choosing a tier = get alerts for that price AND anything cheaper
        label = f"{p['description']} ({p['price_ils']:.0f} ₪)"
        keyboard.append([InlineKeyboardButton(
            label,
            callback_data=f"sub:{event_code}:{perf_code}:{p['code']}:{int(p['price_ils'])}"
        )])
    keyboard.append([InlineKeyboardButton(
        "📊 כל המחירים",
        callback_data=f"sub:{event_code}:{perf_code}:ALL:0"
    )])
    keyboard.append([InlineKeyboardButton("🔙 חזרה", callback_data=f"back:ev:{event_code}")])

    status_line = f"{emoji} {slabel}" if slabel else ""
    hint = "\n\n💡 <i>בחירת כרטיס יקר כוללת גם כרטיסים זולים ממנו</i>" if prices else ""

    await q.edit_message_text(
        f"🎟️ <b>{name}</b>\n📅 {date}  {status_line}{hint}\n\n"
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
    prices    = tm_api.get_prices(event_code, perf_code)
    ticket    = next((p for p in prices if p["code"] == ticket_code), None)

    event_name  = detail["name"] if detail else event_code
    perf_date   = perf["date_str"] if perf else perf_code
    # ticket_desc = display label for the selected ceiling tier
    ticket_desc = ticket["description"] if ticket and ticket_code != "ALL" else None

    sub = db.create_subscription(
        telegram_chat_id=chat_id,
        event_code=event_code,
        event_name=event_name,
        perf_code=perf_code,
        perf_date=perf_date,
        ticket_desc=ticket_desc,
        ticket_code=None,          # filtering is by price only, not by specific code
        max_price_ils=max_price,
    )

    if max_price:
        if ticket_desc:
            price_label = f"{ticket_desc} ומטה (עד {max_price:.0f} ₪)"
        else:
            price_label = f"עד {max_price:.0f} ₪"
    else:
        price_label = "כל המחירים"

    keyboard = [
        [InlineKeyboardButton("📋 כל ההתראות שלי", callback_data="alerts")],
        [InlineKeyboardButton("🏠 ראשי", callback_data="back:events")],
    ]

    text = (
        f"✅ <b>מעקב הוגדר!</b>\n\n"
        f"🎭 {event_name}\n"
        f"📅 {perf_date}\n"
        f"💰 {price_label}\n\n"
        f"תקבל התראה כשיהיו כרטיסים מתאימים."
    )

    if q:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    context.user_data.pop("pending", None)
    context.user_data.pop("awaiting_price", None)

    # Update / create the pinned summary message
    await update_pinned_summary(context.bot, chat_id)


# ── My alerts ────────────────────────────────────────────

async def _show_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = _chat_id(update)
    subs = db.get_subscriptions_for_chat(chat_id)

    keyboard = []
    if subs:
        lines = ["📋 <b>ההתראות הפעילות שלך:</b>\n"]
        for sub in subs:
            price_label  = f"עד {sub.max_price_ils:.0f} ₪" if sub.max_price_ils else "כל מחיר"
            ticket_label = sub.ticket_desc or "כל הכרטיסים"
            date_label   = sub.perf_date or "כל התאריכים"
            lines.append(f"• <b>{sub.event_name}</b> | {date_label} | {ticket_label} | {price_label}")
            keyboard.append([InlineKeyboardButton(
                f"🗑️ {sub.event_name} {date_label}",
                callback_data=f"del:{sub.id}"
            )])
    else:
        lines = ["📋 <b>אין לך התראות פעילות.</b>\n\nהתחל בבחירת הופעה מהרשימה הראשית."]

    keyboard.append([InlineKeyboardButton("🏠 ראשי", callback_data="back:events")])

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
        keyboard = [[InlineKeyboardButton("🏠 ראשי", callback_data="back:events")]]
    else:
        web_url = f"{config.WEB_BASE_URL}/link-telegram/{chat_id}"
        text = (
            "🔗 <b>חיבור לחשבון האתר</b>\n\n"
            "לחץ על הכפתור — תועבר להתחברות עם Google "
            "והחיבור יושלם <b>אוטומטית</b>."
        )
        keyboard = [
            [InlineKeyboardButton("🌐 חיבור עם Google →", url=web_url)],
            [InlineKeyboardButton("🏠 ראשי", callback_data="back:events")],
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

    if data == "back:events":
        await _show_events(update, context)

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
