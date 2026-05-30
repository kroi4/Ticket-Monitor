from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    Boolean, DateTime, ForeignKey, Text
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
        # prevent exact duplicates
        existing = s.query(Subscription).filter_by(
            telegram_chat_id=telegram_chat_id,
            user_id=user_id,
            event_code=event_code,
            perf_code=perf_code,
            ticket_code=ticket_code,
            active=True,
        ).first()
        if existing:
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
        # verify ownership
        if owner_chat_id and sub.telegram_chat_id != owner_chat_id and (
            not sub.user or str(sub.user.telegram_chat_id) != owner_chat_id
        ):
            if not owner_user_id or sub.user_id != owner_user_id:
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
        subs = s.query(Subscription).filter_by(user_id=user_id, active=True).all()
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
