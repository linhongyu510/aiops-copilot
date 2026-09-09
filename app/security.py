"""Optional API-key authentication, role checks and session isolation."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Request

from app.config import config

ROLE_LEVEL = {"viewer": 10, "operator": 20, "admin": 30}


@dataclass(frozen=True)
class Identity:
    actor: str
    role: str
    authenticated: bool


def _request_secret(request: Request) -> str:
    secret = request.headers.get("X-API-Key", "").strip()
    if secret:
        return secret
    authorization = request.headers.get("Authorization", "").strip()
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return value.strip()
    return ""


def resolve_identity(request: Request) -> Identity | None:
    """Resolve a configured key without leaking the key into identity or logs."""
    if not config.auth_enabled:
        return Identity(actor="local-anonymous", role="admin", authenticated=False)

    supplied = _request_secret(request)
    if not supplied:
        return None

    for configured, role in config.api_key_roles.items():
        if hmac.compare_digest(supplied, configured):
            fingerprint = hashlib.sha256(configured.encode("utf-8")).hexdigest()[:12]
            return Identity(actor=f"key-{fingerprint}", role=role, authenticated=True)
    return None


def required_role(path: str, method: str) -> str | None:
    """Return the minimum role for protected API paths."""
    if not path.startswith("/api/"):
        return None
    if path in {"/api/upload", "/api/index_directory"}:
        return "admin"
    if path == "/api/rag/search":
        return "viewer"
    if path == "/api/chat/clear":
        return "operator"
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        return "operator"
    return "viewer"


def role_allows(actual: str, required: str) -> bool:
    return ROLE_LEVEL.get(actual, 0) >= ROLE_LEVEL.get(required, 10_000)


def scoped_session_id(request: Request, session_id: str) -> str:
    """Bind a caller-controlled session id to the authenticated service identity."""
    identity = getattr(request.state, "identity", None)
    if not config.auth_enabled or identity is None:
        return session_id
    return f"{identity.actor}:{session_id}"
