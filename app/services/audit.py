"""The audit log of security-sensitive actions.

:func:`record` joins the caller's transaction, so an action and its audit entry
are saved together or not at all.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from flask import has_request_context, request

from app.extensions import db
from app.models import AuditLog
from app.observability import current_request_id

_MAX_USER_AGENT = 255


def record(
    action: str,
    *,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an entry to the current transaction. Never put a password or code in ``details``."""
    entry = AuditLog(
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
        ip=client_ip(),
        user_agent=client_user_agent(),
        request_id=current_request_id(),
    )
    db.session.add(entry)
    return entry


def client_ip() -> str | None:
    """The client's address, or ``None`` outside a request or when it isn't a valid IP."""
    if not has_request_context() or not request.remote_addr:
        return None
    try:
        return str(ipaddress.ip_address(request.remote_addr))
    except ValueError:
        return None


def client_user_agent() -> str | None:
    if not has_request_context():
        return None
    agent = request.headers.get("User-Agent", "").replace("\x00", "")
    return agent[:_MAX_USER_AGENT] or None
