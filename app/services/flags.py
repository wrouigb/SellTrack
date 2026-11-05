"""Feature flags: an on/off switch, a percentage rollout and an allowlist."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import FeatureFlag, User
from app.services import audit
from app.services.errors import ConflictError, NotFoundError, ValidationFailed

_PERCENT = 100
_DESCRIPTION_MAX = 500
_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]*$", re.ASCII)


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


def get_flag(flag_id: int) -> FeatureFlag:
    flag = db.session.get(FeatureFlag, flag_id)
    if flag is None:
        raise NotFoundError("That flag doesn't exist.")
    return flag


def create_flag(key: str, *, actor_id: int | None, description: str = "") -> FeatureFlag:
    """Add a new flag, switched off. A flag that already exists is left as it is."""
    key = (key or "").strip()
    _check_key(key)
    _check_description(description)
    if db.session.scalar(select(FeatureFlag.id).where(FeatureFlag.key == key)):
        raise ConflictError(key="There's already a flag with that name.")
    try:
        return set_flag(key, actor_id=actor_id, description=description)
    except IntegrityError:  # someone else added it in the moment since the check above
        db.session.rollback()
        raise ConflictError(key="There's already a flag with that name.") from None


def parse_allowlist(text: str | None) -> list[int]:
    """Turn "alex, sam" into account IDs. Names that don't exist are an error, not ignored."""
    names = sorted({name for name in re.split(r"[\s,]+", text or "") if name})
    if not names:
        return []
    rows = db.session.execute(select(User.username, User.id).where(User.username.in_(names))).all()
    found = {str(username).casefold(): user_id for username, user_id in rows}
    if missing := [name for name in names if name.casefold() not in found]:
        raise ValidationFailed(allow_users=f"No account called {', '.join(missing)}.")
    return sorted(set(found.values()))


def allowlist_names(flag: FeatureFlag) -> list[str]:
    """The usernames on a flag's allowlist. Accounts deleted since are left out."""
    if not flag.allow_user_ids:
        return []
    return list(
        db.session.scalars(
            select(User.username).where(User.id.in_(flag.allow_user_ids)).order_by(User.username)
        )
    )


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
    _check_key(key)
    if description is not None:
        _check_description(description)
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


def _check_key(key: str) -> None:
    if not key or len(key) > FeatureFlag.key.type.length:
        raise ValidationFailed(key="Enter a flag name of at most 64 characters.")
    if not _KEY.fullmatch(key):
        raise ValidationFailed(key="Use lowercase letters, numbers, dots, dashes and underscores.")


def _check_description(description: str) -> None:
    if len(description) > _DESCRIPTION_MAX:
        raise ValidationFailed(description=f"Use at most {_DESCRIPTION_MAX} characters.")
