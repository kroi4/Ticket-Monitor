from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    Boolean, DateTime, ForeignKey, Text, or_
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import config

engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id                = Column(Integer, primary_key=True)
    email             = Column(String, unique=True, nullable=False)
    name              = Column(String)
    google_id         = Column(String, unique=True)
    telegram_chat_id  = Column(String, unique=True)
    telegram_username = Column(String)
    notify_telegram       = Column(Boolean, default=True)
    notify_email          = Column(Boolean, default=True)
    check_interval_seconds = Column(Integer, nullable=True)
    created_at        = Column(DateTime, default=utcnow)
    subscriptions     = relationship("Subscription", back_populates="user",
                                     foreign_keys="Subscription.user_id")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id               = Column(Integer, primary_key=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=True)
    telegram_chat_id = Column(String, nullable=True)   # for bot-only users
    event_code       = Column(String, nullable=False)
    event_name       = Column(String)
    perf_code        = Column(String, nullable=True)   # null = all performances
    perf_date        = Column(String, nullable=True)
    ticket_desc      = Column(String, nullable=True)   # null = all ticket types
    ticket_code      = Column(String, nullable=True)
    max_price_ils    = Column(Float, nullable=True)    # null = any price
    active           = Column(Boolean, default=True)
    last_alert_key   = Column(String, default="")
    created_at       = Column(DateTime, default=utcnow)
    user             = relationship("User", back_populates="subscriptions",
                                    foreign_keys=[user_id])

    def effective_chat_id(self) -> str | None:
        """Return the Telegram chat_id to notify."""
        if self.telegram_chat_id:
            return self.telegram_chat_id
        if self.user and self.user.telegram_chat_id:
            return self.user.telegram_chat_id
        return None


class ChatSettings(Base):
    __tablename__ = "chat_settings"
    chat_id           = Column(String, primary_key=True)
    pinned_message_id = Column(Integer, nullable=True)


class LinkToken(Base):
    __tablename__ = "link_tokens"
    token      = Column(String, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    used       = Column(Boolean, default=False)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id               = Column(Integer, primary_key=True)
    sub_id           = Column(Integer, nullable=True)   # intentionally no FK — survives sub deletion
    user_id          = Column(Integer, nullable=True)
    telegram_chat_id = Column(String,  nullable=True)
    event_code       = Column(String,  nullable=False)
    event_name       = Column(String)
    perf_code        = Column(String)
    perf_date        = Column(String)
    status           = Column(String,  nullable=False)  # "available" | "unavailable"
    ticket_summary   = Column(Text,    nullable=True)   # JSON list of {description, price_ils}
    price_min        = Column(Float,   nullable=True)
    price_max        = Column(Float,   nullable=True)
    created_at       = Column(DateTime, default=utcnow)


def init_db():
    Base.metadata.create_all(engine)
    from sqlalchemy import text, inspect as sa_inspect2
    with engine.connect() as conn:
        existing = [c["name"] for c in sa_inspect2(engine).get_columns("users")]
        migrations = {
            "notify_telegram":        "INTEGER NOT NULL DEFAULT 1",
            "notify_email":           "INTEGER NOT NULL DEFAULT 1",
            "check_interval_seconds": "INTEGER",
        }
        for col, defn in migrations.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {defn}"))
        conn.commit()

    # One-time cleanup: remove duplicate subscriptions created before the fix.
    # Keep the oldest sub per (owner, event_code, perf_code, max_price_ils).
    with Session() as s:
        all_subs = (
            s.query(Subscription).filter_by(active=True)
            .order_by(Subscription.created_at).all()
        )
        seen: set = set()
        to_delete: list[int] = []
        for sub in all_subs:
            owner = f"u:{sub.user_id}" if sub.user_id else f"c:{sub.telegram_chat_id}"
            key   = (owner, sub.event_code, sub.perf_code, sub.max_price_ils)
            if key in seen:
                to_delete.append(sub.id)
            else:
                seen.add(key)
        for sub_id in to_delete:
            dup = s.query(Subscription).filter_by(id=sub_id).first()
            if dup:
                s.delete(dup)
        if to_delete:
            s.commit()

    # Migrate orphan bot-created subscriptions to linked user accounts
    with Session() as s:
        orphans = s.query(Subscription).filter(
            Subscription.user_id.is_(None),
            Subscription.telegram_chat_id.isnot(None),
        ).all()
        for sub in orphans:
            user = s.query(User).filter_by(telegram_chat_id=sub.telegram_chat_id).first()
            if user:
                sub.user_id = user.id
        s.commit()


# ── CRUD helpers ──────────────────────────────────────────

def get_or_create_user(google_id: str, email: str, name: str) -> User:
    with Session() as s:
        user = s.query(User).filter_by(google_id=google_id).first()
        if user:
            user.name  = name
            user.email = email
            s.commit()
            s.refresh(user)
            return _detach(user)
        user = User(google_id=google_id, email=email, name=name)
        s.add(user)
        s.commit()
        s.refresh(user)
        return _detach(user)


def get_user(user_id: int) -> User | None:
    with Session() as s:
        u = s.query(User).filter_by(id=user_id).first()
        return _detach(u) if u else None


def link_telegram(user_id: int, chat_id: str, username: str | None = None):
    with Session() as s:
        u = s.query(User).filter_by(id=user_id).first()
        if u:
            u.telegram_chat_id  = chat_id
            u.telegram_username = username
            # Migrate orphan bot-created subscriptions to this user account
            orphans = s.query(Subscription).filter_by(
                telegram_chat_id=chat_id, active=True
            ).filter(Subscription.user_id.is_(None)).all()
            for sub in orphans:
                sub.user_id = user_id
            s.commit()


def get_user_by_chat_id(chat_id: str) -> User | None:
    with Session() as s:
        u = s.query(User).filter_by(telegram_chat_id=chat_id).first()
        return _detach(u) if u else None


def create_subscription(
    telegram_chat_id: str | None,
    event_code: str,
    event_name: str,
    perf_code: str | None,
    perf_date: str | None,
    ticket_desc: str | None,
    ticket_code: str | None,
    max_price_ils: float | None,
    user_id: int | None = None,
) -> Subscription:
    with Session() as s:
        # Build owner filter — a sub belongs to this user if matched by user_id OR chat_id
        owner_clauses = []
        if user_id:
            owner_clauses.append(Subscription.user_id == user_id)
        if telegram_chat_id:
            owner_clauses.append(Subscription.telegram_chat_id == telegram_chat_id)
        owner_filter = or_(*owner_clauses) if owner_clauses else None

        existing = None
        if owner_filter is not None:
            existing = (
                s.query(Subscription)
                .filter(
                    owner_filter,
                    Subscription.event_code == event_code,
                    Subscription.perf_code == perf_code,
                    Subscription.active == True,
                )
                .first()
            )
        if existing:
            # Update price/ticket settings in case the user is changing them
            existing.max_price_ils = max_price_ils
            existing.ticket_desc   = ticket_desc
            existing.ticket_code   = ticket_code
            s.commit()
            return _detach(existing)
        sub = Subscription(
            user_id=user_id,
            telegram_chat_id=telegram_chat_id,
            event_code=event_code,
            event_name=event_name,
            perf_code=perf_code,
            perf_date=perf_date,
            ticket_desc=ticket_desc,
            ticket_code=ticket_code,
            max_price_ils=max_price_ils,
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return _detach(sub)


def delete_subscription(sub_id: int, owner_chat_id: str | None = None, owner_user_id: int | None = None):
    with Session() as s:
        sub = s.query(Subscription).filter_by(id=sub_id).first()
        if not sub:
            return False
        # verify ownership — at least one identifier must match
        owns = False
        if owner_user_id and sub.user_id == owner_user_id:
            owns = True
        if owner_chat_id and (
            sub.telegram_chat_id == owner_chat_id
            or (sub.user and str(sub.user.telegram_chat_id) == owner_chat_id)
        ):
            owns = True
        if not owns:
            return False
        s.delete(sub)
        s.commit()
        return True


def get_subscriptions_for_chat(chat_id: str) -> list[Subscription]:
    with Session() as s:
        # direct subscriptions
        direct = s.query(Subscription).filter_by(
            telegram_chat_id=chat_id, active=True
        ).all()
        # subscriptions via linked user account
        user   = s.query(User).filter_by(telegram_chat_id=chat_id).first()
        user_subs = []
        if user:
            user_subs = s.query(Subscription).filter_by(
                user_id=user.id, active=True
            ).all()
        seen_ids = set()
        result   = []
        for sub in direct + user_subs:
            if sub.id not in seen_ids:
                seen_ids.add(sub.id)
                result.append(_detach(sub))
        return result


def get_subscriptions_for_user(user_id: int) -> list[Subscription]:
    with Session() as s:
        user = s.query(User).filter_by(id=user_id).first()
        subs = s.query(Subscription).filter_by(user_id=user_id, active=True).all()
        seen_ids = {sub.id for sub in subs}
        # Also include any remaining orphan subs linked via telegram_chat_id
        if user and user.telegram_chat_id:
            orphans = s.query(Subscription).filter_by(
                telegram_chat_id=user.telegram_chat_id, active=True
            ).filter(Subscription.user_id.is_(None)).all()
            for sub in orphans:
                if sub.id not in seen_ids:
                    seen_ids.add(sub.id)
                    subs.append(sub)
        return [_detach(s2) for s2 in subs]


def get_email_for_sub(sub) -> str | None:
    """Return the Google-linked email address for a subscription owner, if available."""
    with Session() as s:
        if sub.user_id:
            u = s.query(User).filter_by(id=sub.user_id).first()
            return u.email if u else None
        if sub.telegram_chat_id:
            u = s.query(User).filter_by(telegram_chat_id=sub.telegram_chat_id).first()
            return u.email if u else None
    return None


def get_all_active_subscriptions() -> list[Subscription]:
    with Session() as s:
        return [_detach(sub) for sub in s.query(Subscription).filter_by(active=True).all()]


def update_alert_key(sub_id: int, key: str):
    with Session() as s:
        sub = s.query(Subscription).filter_by(id=sub_id).first()
        if sub:
            sub.last_alert_key = key
            s.commit()


def create_link_token(user_id: int) -> str:
    import secrets as sec
    token = sec.token_urlsafe(16)
    with Session() as s:
        # invalidate old tokens
        old = s.query(LinkToken).filter_by(user_id=user_id, used=False).all()
        for t in old:
            t.used = True
        s.add(LinkToken(token=token, user_id=user_id))
        s.commit()
    return token


def use_link_token(token: str) -> User | None:
    with Session() as s:
        lt = s.query(LinkToken).filter_by(token=token, used=False).first()
        if not lt:
            return None
        lt.used = True
        user = s.query(User).filter_by(id=lt.user_id).first()
        s.commit()
        return _detach(user) if user else None


def get_pinned_message_id(chat_id: str) -> int | None:
    with Session() as s:
        row = s.query(ChatSettings).filter_by(chat_id=chat_id).first()
        return row.pinned_message_id if row else None


def set_pinned_message_id(chat_id: str, message_id: int | None):
    with Session() as s:
        row = s.query(ChatSettings).filter_by(chat_id=chat_id).first()
        if row:
            row.pinned_message_id = message_id
        else:
            s.add(ChatSettings(chat_id=chat_id, pinned_message_id=message_id))
        s.commit()


def update_user_settings(user_id: int, **kwargs):
    allowed = {"notify_telegram", "notify_email", "check_interval_seconds"}
    with Session() as s:
        u = s.query(User).filter_by(id=user_id).first()
        if not u:
            return
        for k, v in kwargs.items():
            if k in allowed:
                setattr(u, k, v)
        s.commit()


def get_sub_for_perf(
    telegram_chat_id: str | None,
    user_id: int | None,
    event_code: str,
    perf_code: str | None,
    include_all_perfs: bool = True,
) -> "Subscription | None":
    """Find the active subscription for this (owner, event, performance).
    include_all_perfs=True also returns subs with perf_code=None (all-perf watches)."""
    with Session() as s:
        owner_clauses = []
        if user_id:
            owner_clauses.append(Subscription.user_id == user_id)
        if telegram_chat_id:
            owner_clauses.append(Subscription.telegram_chat_id == telegram_chat_id)
        if not owner_clauses:
            return None
        perf_filter = (
            or_(Subscription.perf_code == perf_code, Subscription.perf_code.is_(None))
            if include_all_perfs
            else Subscription.perf_code == perf_code
        )
        sub = (
            s.query(Subscription)
            .filter(or_(*owner_clauses), Subscription.event_code == event_code,
                    perf_filter, Subscription.active == True)
            .first()
        )
        return _detach(sub) if sub else None


def get_subscription(sub_id: int) -> Subscription | None:
    with Session() as s:
        sub = s.query(Subscription).filter_by(id=sub_id).first()
        return _detach(sub) if sub else None


def log_alert_event(sub, status: str, matching: list[dict], perf: dict | None = None):
    import json as _json
    summary  = _json.dumps(
        [{"description": m["description"], "price_ils": m["price_ils"]} for m in matching],
        ensure_ascii=False,
    ) if matching else None
    price_min = min(m["price_ils"] for m in matching) if matching else None
    price_max = max(m["price_ils"] for m in matching) if matching else None
    chat_id   = sub.telegram_chat_id
    if not chat_id:
        try:
            if sub.user:
                chat_id = sub.user.telegram_chat_id
        except Exception:
            pass
    with Session() as s:
        ev = AlertEvent(
            sub_id           = sub.id,
            user_id          = sub.user_id,
            telegram_chat_id = chat_id,
            event_code       = sub.event_code,
            event_name       = sub.event_name,
            perf_code        = perf["perf_code"] if perf else sub.perf_code,
            perf_date        = perf["date_str"]  if perf else sub.perf_date,
            status           = status,
            ticket_summary   = summary,
            price_min        = price_min,
            price_max        = price_max,
        )
        s.add(ev)
        s.commit()


def get_alert_history(sub_id: int, limit: int = 200) -> list[dict]:
    import json as _json
    with Session() as s:
        events = (
            s.query(AlertEvent)
            .filter_by(sub_id=sub_id)
            .order_by(AlertEvent.created_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for i, ev in enumerate(events):
            tickets = _json.loads(ev.ticket_summary) if ev.ticket_summary else []
            end_ts  = events[i - 1].created_at.isoformat() + "Z" if i > 0 else None
            result.append({
                "id":         ev.id,
                "status":     ev.status,
                "tickets":    tickets,
                "price_min":  ev.price_min,
                "price_max":  ev.price_max,
                "created_at": ev.created_at.isoformat() + "Z",
                "end_time":   end_ts,
                "perf_date":  ev.perf_date,
            })
        return result


def _detach(obj):
    """Expunge obj from its current session and make it transient (safe to use outside session)."""
    if obj is None:
        return None
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import make_transient
    insp = sa_inspect(obj, raiseerr=False)
    if insp is not None and insp.session is not None:
        # Eagerly touch relationship attributes while session is still open
        try:
            if hasattr(obj, "user") and hasattr(obj, "user_id") and obj.user_id:
                u = obj.user
                if u:
                    _ = u.telegram_chat_id
                    _ = u.id
        except Exception:
            pass
        insp.session.expunge(obj)
    make_transient(obj)
    return obj
