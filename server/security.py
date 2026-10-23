"""Security helpers: token hashing, auth, origin validation, role gates."""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any

from server.db import fetch_one


def hash_token(token: str) -> str:
    """SHA-256 hash of a bearer token (hex digest)."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    """Generate a random 32-byte hex token."""
    return secrets.token_hex(32)


def authenticate(token: str | None) -> dict[str, Any] | None:
    """Look up a person by bearer token. Returns the person row or None."""
    if not token:
        return None
    h = hash_token(token)
    return fetch_one("SELECT * FROM people WHERE token_hash = ?", (h,))


def check_origin(origin: str | None) -> bool:
    """Validate the Origin header against the allowlist.

    Rules (from the MCP Streamable HTTP spec):
    - If Origin is present and not in the allowlist → reject (403).
    - If Origin is absent → allow (same-origin or non-browser client).
    """
    if origin is None:
        return True  # no Origin header = not a cross-origin browser request
    allowed_raw = os.environ.get("ALLOWED_ORIGINS", "")
    if not allowed_raw:
        return True  # no allowlist configured = accept all
    allowed = {o.strip().rstrip("/") for o in allowed_raw.split(",") if o.strip()}
    return origin.rstrip("/") in allowed


def require_role(person: dict[str, Any], *allowed_roles: str) -> str | None:
    """Return an error message if the person's role is not in *allowed_roles*.

    Returns None if the person has one of the allowed roles.
    """
    if person["role"] not in allowed_roles:
        friendly = ", ".join(allowed_roles)
        return f"Only {friendly}s can do that."
    return None


def get_caller_from_headers(headers: Any) -> dict[str, Any]:
    """Extract and validate bearer token from HTTP headers."""
    auth = ""
    if hasattr(headers, "get"):
        auth = headers.get("authorization", "")
    elif isinstance(headers, dict):
        auth = headers.get("authorization", "")

    if not auth:
        raise PermissionError("Missing Authorization header.")
    if not auth.lower().startswith("bearer "):
        raise PermissionError("Authorization header must be Bearer token.")

    token = auth[7:].strip()
    person = authenticate(token)
    if not person:
        raise PermissionError("Invalid or unknown bearer token.")
    return person


def get_caller(ctx: Any) -> dict[str, Any]:
    """Retrieve the authenticated person from an MCP tool Context."""
    if ctx is None or not hasattr(ctx, "headers"):
        raise PermissionError("Missing request context for authentication.")
    return get_caller_from_headers(ctx.headers)

