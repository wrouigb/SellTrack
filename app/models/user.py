from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from flask_login import UserMixin
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Model, TimestampMixin, created_at_column, enum_type, primary_key

DEFAULT_TIMEZONE = "Australia/Melbourne"


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Theme(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class User(UserMixin, TimestampMixin, Model):  # type: ignore[misc]
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "char_length(username::text) BETWEEN 3 AND 30 AND username::text ~ '^[A-Za-z0-9._-]+$'",
            name="username_format",
        ),
        CheckConstraint("session_version >= 1", name="session_version_positive"),
    )

    id: Mapped[int] = primary_key()
    username: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole, "user_role"),
        nullable=False,
        default=UserRole.USER,
        server_default=UserRole.USER.value,
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_type(UserStatus, "user_status"),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
    must_change_password: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    #: Part of every login session. Bumping it logs the account out on all devices.
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TIMEZONE, server_default=DEFAULT_TIMEZONE
    )
    theme: Mapped[Theme] = mapped_column(
        enum_type(Theme, "user_theme"),
        nullable=False,
        default=Theme.SYSTEM,
        server_default=Theme.SYSTEM.value,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def get_id(self) -> str:
        """The value kept in the session cookie. See ``app.security.sessions``."""
        return f"{self.id}:{self.session_version}"

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


class RecoveryCode(TimestampMixin, Model):
    """A one-time way back into an account. Only a hash of the code is stored."""

    __tablename__ = "recovery_codes"

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignIn(Model):
    """One successful login, for the "recent sign-ins" list. Append-only."""

    __tablename__ = "sign_ins"

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = created_at_column()
