from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Model, TimestampMixin, primary_key


class FeatureFlag(TimestampMixin, Model):
    __tablename__ = "feature_flags"
    __table_args__ = (CheckConstraint("rollout_pct BETWEEN 0 AND 100", name="rollout_pct_range"),)

    id: Mapped[int] = primary_key()
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=""
    )
    enabled: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    rollout_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    allow_user_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list, server_default=text("'{}'::bigint[]")
    )
