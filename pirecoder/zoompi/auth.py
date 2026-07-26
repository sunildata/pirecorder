"""Password authentication.

Deliberately minimal: one shared device password, salted PBKDF2, session
cookie. There are no user accounts because the threat model is "someone else
on the venue Wi-Fi", not multi-tenant access control.

Recording control endpoints stay reachable when auth is enabled but the
session is missing *only* if auth is switched off in settings — a lost
password must never be the reason a live take can't be stopped.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from .config import config

PBKDF2_ROUNDS = 120_000
SALT_BYTES = 16


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check that also accepts a legacy plaintext value.

    A fresh install ships with a plaintext default so the device is usable
    before anyone visits Settings; the first successful login upgrades it.
    """
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        return hmac.compare_digest(password, stored)
    try:
        _, rounds, salt_hex, digest_hex = stored.split("$", 3)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def check_credentials(password: str) -> bool:
    stored = config.get("password", "")
    if not verify_password(password, stored):
        return False
    if not stored.startswith("pbkdf2$"):
        config.set("password", hash_password(password))
    return True


def set_password(new_password: str) -> None:
    if len(new_password) < 4:
        raise ValueError("Password must be at least 4 characters")
    config.set("password", hash_password(new_password))


def is_authenticated() -> bool:
    if not config.get("auth_enabled"):
        return True
    return bool(session.get("authenticated"))


def login_session() -> None:
    session["authenticated"] = True
    session["token"] = secrets.token_hex(16)
    session.permanent = True


def logout_session() -> None:
    session.clear()


def login_required(view):
    """Redirect browsers to the login page; answer 401 JSON for API calls."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_authenticated():
            return view(*args, **kwargs)
        wants_json = request.path.startswith("/api/") or request.is_json
        if wants_json:
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("web.login", next=request.path))

    return wrapper
