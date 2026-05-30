"""HTML email notifications for subscriptions and ticket alerts."""
import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import config

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


# ── HTML helpers ──────────────────────────────────────────

def _wrap(content: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin:0; padding:0; background:#f2f4f8;
          font-family:Arial,Helvetica,sans-serif; direction:rtl; }}
  @media only screen and (max-width:620px) {{
    .email-outer {{ padding:12px 0 !important; }}
    .email-inner {{ width:100% !important; border-radius:0 !important; }}
    .email-header, .email-body {{ padding:20px 16px !important; }}
    .email-footer {{ padding:12px 16px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f2f4f8;">
<table class="email-outer" width="100%" cellpadding="0" cellspacing="0"
       style="background:#f2f4f8;padding:32px 0;">
<tr><td align="center">

<!-- Card -->
<table class="email-inner" cellpadding="0" cellspacing="0"
       style="width:100%;max-width:560px;background:#ffffff;
              border-radius:10px;overflow:hidden;
              box-shadow:0 4px 16px rgba(0,0,0,.10);">

  <!-- Header -->
  <tr><td class="email-header"
          style="background:#1a1f2e;padding:24px 28px;text-align:center;">
    <div style="font-size:26px;margin-bottom:6px;">🎟️</div>
    <h1 style="margin:0;color:#ffffff;font-size:18px;font-weight:700;
               letter-spacing:.4px;">מעקב כרטיסי הופעות</h1>
  </td></tr>

  <!-- Body -->
  <tr><td class="email-body" style="padding:24px 28px;">
    {content}
  </td></tr>

  <!-- Footer -->
  <tr><td class="email-footer"
          style="background:#f7f8fb;padding:14px 28px;text-align:center;
                 border-top:1px solid #eaecf0;font-size:12px;color:#9ea4b0;">
    Ticket Monitor &nbsp;·&nbsp; Ticketmaster Israel<br>
    <span style="font-size:11px;">נשלח אוטומטית — אין להשיב למייל זה</span>
  </td></tr>

</table>
<!-- /Card -->

</td></tr>
</table>
</body>
</html>"""


def _section(title: str, body: str, color: str = "#f7f8fb") -> str:
    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:{color};border-radius:8px;margin-bottom:14px;">
<tr><td style="padding:14px 18px;">
  <div style="font-size:12px;font-weight:700;color:#555;margin-bottom:10px;
              text-transform:uppercase;letter-spacing:.5px;">{title}</div>
  {body}
</td></tr>
</table>"""


def _row(label: str, value: str) -> str:
    """One label/value detail row — table-based so it works in Gmail."""
    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="border-bottom:1px solid #eaecf0;">
<tr>
  <td style="padding:6px 0;font-size:13px;color:#9ea4b0;
             width:38%;text-align:right;">{label}</td>
  <td style="padding:6px 4px;font-size:13px;color:#1a1f2e;
             font-weight:600;text-align:left;">{value}</td>
</tr>
</table>"""


def _badge(text: str, bg: str = "#e8f5e9", color: str = "#2e7d32") -> str:
    return (f'<span style="background:{bg};color:{color};padding:3px 10px;'
            f'border-radius:12px;font-size:12px;font-weight:700;">{text}</span>')


def _button(label: str, url: str, bg: str = "#e53935") -> str:
    return f"""
<table cellpadding="0" cellspacing="0" style="margin:20px auto 0;width:100%;">
<tr><td align="center" style="border-radius:6px;background:{bg};">
  <a href="{url}"
     style="display:block;padding:14px 24px;color:#ffffff;
            text-decoration:none;font-size:16px;font-weight:700;
            letter-spacing:.3px;text-align:center;">
    {label}
  </a>
</td></tr>
</table>"""


# ── Email builders ────────────────────────────────────────

def _sub_confirmed_html(sub, all_subs: list) -> str:
    price_label = (
        f"{sub.ticket_desc} ומטה (עד {sub.max_price_ils:.0f} ₪)"
        if sub.max_price_ils and sub.ticket_desc
        else f"עד {sub.max_price_ils:.0f} ₪"
        if sub.max_price_ils
        else "כל המחירים"
    )

    detail_rows = (
        _row("אירוע",      sub.event_name or sub.event_code) +
        _row("תאריך",      sub.perf_date or "כל התאריכים") +
        _row("תקרת מחיר",  price_label) +
        _row("נוצר",       datetime.now().strftime("%d/%m/%Y %H:%M"))
    )
    new_block = _section("✅ מעקב חדש נרשם", detail_rows)

    subs_rows = ""
    for s in all_subs:
        pl = (f"עד {s.max_price_ils:.0f} ₪" if s.max_price_ils else "כל המחירים")
        subs_rows += _row(
            s.event_name or s.event_code,
            f"{s.perf_date or 'כל תאריכים'} &nbsp;·&nbsp; {pl}"
        )
    active_block = _section("📋 כל המעקבים הפעילים שלך", subs_rows) if subs_rows else ""

    intro = '<p style="font-size:15px;color:#333;margin:0 0 18px;">המעקב הבא הוגדר בהצלחה:</p>'
    return _wrap(intro + new_block + active_block, "מעקב הוגדר")


def _alert_html(sub, perf: dict, matching: list[dict], event_detail: dict | None) -> str:
    event_name  = sub.event_name or sub.event_code
    venue_name  = (event_detail or {}).get("venue_name", "")
    date_str    = perf["date_str"]
    status_text = perf["status_label"]
    buy_url     = perf["buy_url"]
    emoji       = perf["emoji"]

    venue_html = (f"&nbsp;&nbsp;<span style='font-size:13px;color:#555;'>"
                  f"📍 {venue_name}</span>") if venue_name else ""

    header = f"""
<h2 style="margin:0 0 8px;font-size:20px;color:#1a1f2e;">{event_name}</h2>
<div style="margin-bottom:18px;font-size:14px;color:#555;">
  📅 {date_str}{venue_html}
  &nbsp;&nbsp;{_badge(f"{emoji} {status_text}")}
</div>"""

    ticket_rows = ""
    for m in matching:
        ticket_rows += f"""
<tr>
  <td style="padding:10px 12px;font-size:14px;color:#1a1f2e;
             border-bottom:1px solid #eaecf0;">{m['description']}</td>
  <td style="padding:10px 12px;font-size:14px;text-align:center;
             border-bottom:1px solid #eaecf0;white-space:nowrap;">
    <b style="color:#e53935;">{m['price_ils']:.0f} ₪</b>
  </td>
</tr>"""

    tickets_table = f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #eaecf0;border-radius:8px;
              overflow:hidden;margin-bottom:8px;">
  <tr style="background:#f7f8fb;">
    <th style="padding:10px 12px;font-size:12px;color:#777;
               font-weight:700;text-align:right;">סוג כרטיס</th>
    <th style="padding:10px 12px;font-size:12px;color:#777;
               font-weight:700;text-align:center;">מחיר</th>
  </tr>
  {ticket_rows}
</table>"""

    max_label   = f"עד {sub.max_price_ils:.0f} ₪" if sub.max_price_ils else "כל מחיר"
    filter_note = (f'<p style="font-size:12px;color:#9ea4b0;margin:4px 0 0;">'
                   f'פילטר שהוגדר: {max_label}</p>')

    btn = _button("🛒 לרכישת כרטיסים", buy_url)

    content = header + tickets_table + filter_note + btn
    return _wrap(content, f"כרטיסים זמינים — {event_name}")


# ── Public async API ──────────────────────────────────────

def _send_sync(to_email: str, subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.SMTP_FROM or config.SMTP_USER
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


async def _send(to_email: str, subject: str, html: str):
    if not enabled() or not to_email:
        return
    try:
        await asyncio.to_thread(_send_sync, to_email, subject, html)
        log.info("Email sent to %s — %s", to_email, subject)
    except Exception as exc:
        log.warning("Email failed to %s: %s", to_email, exc)


async def send_subscription_confirmed(to_email: str, sub, all_subs: list):
    event_name = sub.event_name or sub.event_code
    html = _sub_confirmed_html(sub, all_subs)
    await _send(to_email, f"✅ מעקב הוגדר — {event_name}", html)


async def send_ticket_alert(to_email: str, sub, perf: dict,
                             matching: list[dict], event_detail: dict | None = None):
    event_name = sub.event_name or sub.event_code
    html = _alert_html(sub, perf, matching, event_detail)
    await _send(to_email, f"🎟️ כרטיסים זמינים — {event_name} {perf['date_str'][:5]}", html)
