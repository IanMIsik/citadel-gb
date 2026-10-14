"""Operations shared by the admin API and the `citadel` command line."""
from __future__ import annotations

import asyncio
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import passwords
from .store import new_token

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def two_factor_roles(setting: str) -> tuple[str, ...]:
    return tuple(r.strip() for r in setting.split(",") if r.strip())


async def create_user(store, email: str, role: str, password: str | None = None, expires_days: int | None = None,
                      force_change: bool = True) -> tuple[int, str]:
    """Create a login. With no password, one is generated; the caller shows it once. Returns (id, password)."""
    password = password or passwords.generate_password()
    expires = datetime.now(timezone.utc) + timedelta(days=expires_days) if expires_days else None
    uid = await store.create_user(email, await asyncio.to_thread(passwords.hash_password, password), role, expires, force_change)
    return uid, password


async def create_demo_link(store, label: str, days: float) -> dict:
    """A demo user (read-only role, no usable password) and the secret link that signs in as it."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30] or "demo"
    email = f"demo-{slug}-{secrets.token_hex(3)}@demo.invalid"
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    unusable = await asyncio.to_thread(passwords.hash_password, secrets.token_urlsafe(32))
    user_id = await store.create_user(email, unusable, "demo", expires)
    token = new_token()
    link_id = await store.create_demo_link(label, token, user_id, expires)
    return {"id": link_id, "token": token, "path": f"/demo/{token}", "email": email, "expires_at": expires}
