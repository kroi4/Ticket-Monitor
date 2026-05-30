"""FastAPI web application with Google SSO."""
import logging
from functools import wraps
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
import config
import db

log = logging.getLogger(__name__)

app = FastAPI(title="Ticket Monitor")
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=86400 * 30)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Google OAuth ─────────────────────────────────────────
oauth = OAuth()
oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def current_user(request: Request) -> db.User | None:
    uid = request.session.get("user_id")
    if uid:
        return db.get_user(uid)
    return None


def require_login(request: Request) -> db.User:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


# ── Routes ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/auth/login")
async def auth_login(request: Request):
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token    = await oauth.google.authorize_access_token(request)
        userinfo = token.get("userinfo") or {}
        google_id = userinfo.get("sub") or userinfo.get("id")
        email     = userinfo.get("email", "")
        name      = userinfo.get("name", email)
        if not google_id:
            return RedirectResponse("/?error=no_id")
        user = db.get_or_create_user(google_id=google_id, email=email, name=name)
        request.session["user_id"] = user.id

        # Auto-link Telegram if arrived from bot link
        pending_chat = request.session.pop("after_login_link_chat", None)
        if pending_chat:
            db.link_telegram(user.id, pending_chat)
            return RedirectResponse(f"/linked/{pending_chat}")

        return RedirectResponse("/dashboard")
    except Exception as exc:
        log.error("OAuth callback error: %s", exc, exc_info=True)
        return RedirectResponse("/?error=oauth_failed")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/")
    subs = db.get_subscriptions_for_user(user.id)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user":    user,
        "subs":    subs,
        "web_base": config.WEB_BASE_URL,
    })


@app.get("/link-telegram/{chat_id}", response_class=HTMLResponse)
async def link_telegram_page(request: Request, chat_id: str):
    user = current_user(request)
    if not user:
        # Save chat_id and redirect to login — after login will auto-link
        request.session["after_login_link_chat"] = chat_id
        return RedirectResponse("/auth/login")
    db.link_telegram(user.id, chat_id)
    return RedirectResponse(f"/linked/{chat_id}")


@app.get("/linked/{chat_id}", response_class=HTMLResponse)
async def linked_page(request: Request, chat_id: str):
    user = current_user(request)
    return templates.TemplateResponse("linked.html", {
        "request": request,
        "user":    user,
        "chat_id": chat_id,
    })


@app.get("/link-telegram-token")
async def get_link_token(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)
    token = db.create_link_token(user.id)
    bot_url = f"https://t.me/ticket_monitorBOT?start=link_{token}"
    return JSONResponse({"url": bot_url, "token": token})


@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(request: Request, sub_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)
    deleted = db.delete_subscription(sub_id, owner_user_id=user.id)
    if not deleted:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True})
