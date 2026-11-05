"""Feature flags: an on/off switch, a percentage rollout and an allowlist."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from sqlalchemy import select

from app.extensions import db
from app.models import FeatureFlag
from app.services import audit
from app.services.errors import NotFoundError, ValidationFailed

_PERCENT = 100


def rollout_bucket(key: str, user_id: int) -> int:
    """A stable number from 0 to 99 for this user and flag.

    Stable, so raising the rollout only ever adds people. Keyed by flag, so the same people
    aren't always first in line.
    """
    digest = hashlib.sha256(f"{key}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % _PERCENT


def flag_applies(flag: FeatureFlag | None, user_id: int | None) -> bool:
    """``enabled`` is a kill switch: when off, nobody gets the feature, allowlisted or not."""
    if flag is None or not flag.enabled:
        return False
    if flag.rollout_pct >= _PERCENT:
        return True
    if user_id is None:
        return False
    return user_id in flag.allow_user_ids or rollout_bucket(flag.key, user_id) < flag.rollout_pct


def is_enabled(key: str, user_id: int | None = None) -> bool:
    """Is this feature on for this user? A flag that doesn't exist is off."""
    flag = db.session.scalar(select(FeatureFlag).where(FeatureFlag.key == key))
    return flag_applies(flag, user_id)


def list_flags() -> Sequence[FeatureFlag]:
    return db.session.scalars(select(FeatureFlag).order_by(FeatureFlag.key)).all()


def set_flag(
    key: str,
    *,
    actor_id: int | None,
    enabled: bool | None = None,
    rollout_pct: int | None = None,
    allow_user_ids: Iterable[int] | None = None,
    description: str | None = None,
) -> FeatureFlag:
    """Create the flag if needed and change the fields given. Audit-logged."""
    if not key or len(key) > FeatureFlag.key.type.length:
        raise ValidationFailed(key="Enter a flag name of at most 64 characters.")
    if rollout_pct is not None and not 0 <= rollout_pct <= _PERCENT:
        raise ValidationFailed(rollout_pct="Enter a percentage from 0 to 100.")

    flag = db.session.scalar(select(FeatureFlag).where(FeatureFlag.key == key).with_for_update())
    if flag is None:
        flag = FeatureFlag(key=key, enabled=False, rollout_pct=0, allow_user_ids=[], description="")
        db.session.add(flag)
    if enabled is not None:
        flag.enabled = enabled
    if rollout_pct is not None:
        flag.rollout_pct = rollout_pct
    if allow_user_ids is not None:
        flag.allow_user_ids = sorted(set(allow_user_ids))
    if description is not None:
        flag.description = description
    db.session.flush()
    audit.record(
        "flag.changed",
        actor_id=actor_id,
        target_type="feature_flag",
        target_id=flag.id,
        details={
            "key": flag.key,
            "enabled": flag.enabled,
            "rollout_pct": flag.rollout_pct,
            "allow_user_ids": flag.allow_user_ids,
        },
    )
    db.session.commit()
    return flag


def delete_flag(key: str, *, actor_id: int | None) -> None:
    flag = db.session.scalar(select(FeatureFlag).where(FeatureFlag.key == key))
    if flag is None:
        raise NotFoundError("That flag doesn't exist.")
    audit.record(
        "flag.deleted",
        actor_id=actor_id,
        target_type="feature_flag",
        target_id=flag.id,
        details={"key": flag.key},
    )
    db.session.delete(flag)
    db.session.commit()
