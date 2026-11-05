"""Security headers, including a nonce-based Content Security Policy."""

from __future__ import annotations

import secrets

from flask import Flask, Response, g, request, session

_HSTS = "max-age=31536000; includeSubDomains"
_PERMISSIONS_POLICY = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"


def csp_nonce() -> str:
    """The nonce for this response. Any inline ``<script>`` must carry it."""
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    nonce: str = g.csp_nonce
    return nonce


def content_security_policy(nonce: str) -> str:
    """No ``unsafe-inline``, no ``unsafe-eval``, and nothing from other sites."""
    return "; ".join(
        (
            "default-src 'none'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "manifest-src 'self'",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        )
    )


def init_app(app: Flask) -> None:
    app.jinja_env.globals["csp_nonce"] = csp_nonce

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        headers = response.headers
        headers["Content-Security-Policy"] = content_security_policy(csp_nonce())
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        headers["Permissions-Policy"] = _PERMISSIONS_POLICY
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if app.config["HSTS_ENABLED"]:
            headers["Strict-Transport-Security"] = _HSTS
        # Pages for a logged-in person must not sit in a browser or proxy cache: the back button
        # on a shared computer shouldn't bring back someone's figures or recovery codes.
        if "_user_id" in session and request.endpoint != "static":
            headers["Cache-Control"] = "no-store"
        return response
