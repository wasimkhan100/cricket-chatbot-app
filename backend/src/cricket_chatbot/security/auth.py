"""Anonymous signed session tokens and the admin token check.

A session token is `sid.issued_at.signature` with an HMAC-SHA256 signature, so the server needs no session
table to verify it, and a client cannot forge or extend one. Rate limits are keyed on the session id.
"""

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Header, HTTPException, Request


def _sign(secret: str, payload: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue_token(secret: str) -> tuple[str, str]:
    sid = secrets.token_urlsafe(12)
    payload = f"{sid}.{int(time.time())}"
    return sid, f"{payload}.{_sign(secret, payload)}"


def verify_token(secret: str, token: str, ttl_s: int) -> str | None:
    try:
        sid, issued, sig = token.split(".")
        issued_at = int(issued)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(secret, f"{sid}.{issued}")):
        return None
    if not 0 <= time.time() - issued_at <= ttl_s:
        return None
    return sid


def session_dependency(request: Request, authorization: str = Header(default="")) -> str:
    s = request.app.state.settings
    token = authorization.removeprefix("Bearer ").strip()
    sid = verify_token(s.session_secret, token, s.session_ttl_s) if token else None
    if not sid:
        raise HTTPException(401, "missing or expired session; call POST /api/session")
    return sid


def admin_dependency(request: Request, x_admin_token: str = Header(default="")) -> None:
    expected = request.app.state.settings.admin_token
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(403, "admin token required")


def client_ip(request: Request) -> str:
    # Local app: trust the socket address only. Behind a proxy, read X-Forwarded-For from the trusted proxy.
    return request.client.host if request.client else "unknown"
