"""Auth data access: users, sessions and the audit trail (Postgres), plus an in-memory twin used by tests."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

ROLES = ("admin", "client", "demo")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Only this hash is stored, so a copy of the database cannot be used to log in."""
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuthStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    # -- users -----------------------------------------------------------------------------------
    async def get_user_by_email(self, email: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM auth_users WHERE email = $1", email)
        return dict(row) if row else None

    async def create_user(self, email: str, password_hash: str, role: str, expires_at: datetime | None = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO auth_users (email, password_hash, role, expires_at) VALUES ($1, $2, $3, $4) RETURNING id",
            email, password_hash, role, expires_at)

    async def list_users(self) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, email, role, is_active, expires_at, failed_attempts, locked_until, last_login_at, created_at "
            "FROM auth_users ORDER BY email")
        return [dict(r) for r in rows]

    async def set_active(self, email: str, active: bool) -> bool:
        return (await self.pool.execute("UPDATE auth_users SET is_active = $2 WHERE email = $1", email, active)) != "UPDATE 0"

    async def set_password(self, email: str, password_hash: str) -> bool:
        res = await self.pool.execute(
            "UPDATE auth_users SET password_hash = $2, failed_attempts = 0, locked_until = NULL WHERE email = $1",
            email, password_hash)
        return res != "UPDATE 0"

    async def update_hash(self, user_id: int, password_hash: str) -> None:
        await self.pool.execute("UPDATE auth_users SET password_hash = $2 WHERE id = $1", user_id, password_hash)

    async def record_failure(self, user_id: int, max_attempts: int, lock_minutes: int) -> bool:
        """Count a wrong password; at `max_attempts` lock the account for `lock_minutes` and start counting again.
        Returns True when this failure locked it."""
        row = await self.pool.fetchrow(
            "UPDATE auth_users SET failed_attempts = failed_attempts + 1 WHERE id = $1 RETURNING failed_attempts", user_id)
        if row and row["failed_attempts"] >= max_attempts:
            await self.pool.execute(
                "UPDATE auth_users SET failed_attempts = 0, locked_until = $2 WHERE id = $1",
                user_id, _now() + timedelta(minutes=lock_minutes))
            return True
        return False

    async def record_success(self, user_id: int) -> None:
        await self.pool.execute(
            "UPDATE auth_users SET failed_attempts = 0, locked_until = NULL, last_login_at = now() WHERE id = $1", user_id)

    # -- sessions --------------------------------------------------------------------------------
    async def create_session(self, user_id: int, token: str, expires_at: datetime, ip: str, user_agent: str) -> None:
        await self.pool.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, expires_at, ip, user_agent) VALUES ($1, $2, $3, $4, $5)",
            token_hash(token), user_id, expires_at, ip, user_agent[:300])

    async def get_session(self, token: str) -> dict | None:
        """The live session and its user, or None (unknown, revoked, expired, user disabled or expired)."""
        row = await self.pool.fetchrow(
            """
            SELECT s.token_hash, s.user_id, s.last_seen_at, s.expires_at, u.email, u.role
            FROM auth_sessions s JOIN auth_users u ON u.id = s.user_id
            WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > now()
              AND u.is_active AND (u.expires_at IS NULL OR u.expires_at > now())
            """, token_hash(token))
        return dict(row) if row else None

    async def touch_session(self, token: str) -> None:
        await self.pool.execute("UPDATE auth_sessions SET last_seen_at = now() WHERE token_hash = $1", token_hash(token))

    async def revoke_session(self, token: str) -> None:
        await self.pool.execute("UPDATE auth_sessions SET revoked_at = now() WHERE token_hash = $1", token_hash(token))

    async def revoke_user_sessions(self, email: str) -> int:
        res = await self.pool.execute(
            "UPDATE auth_sessions SET revoked_at = now() WHERE revoked_at IS NULL "
            "AND user_id = (SELECT id FROM auth_users WHERE email = $1)", email)
        return int(res.split()[-1])

    async def purge_sessions(self) -> None:
        await self.pool.execute(
            "DELETE FROM auth_sessions WHERE expires_at < now() - interval '1 day' OR revoked_at < now() - interval '1 day'")

    async def count_users(self) -> int:
        return await self.pool.fetchval("SELECT count(*) FROM auth_users")

    # -- audit -----------------------------------------------------------------------------------
    async def audit(self, event: str, email: str | None = None, user_id: int | None = None, ip: str | None = None,
                    detail: str | None = None) -> None:
        await self.pool.execute(
            "INSERT INTO auth_audit (event, email, user_id, ip, detail) VALUES ($1, $2, $3, $4, $5)",
            event, email, user_id, ip, detail)


@dataclass
class MemoryAuthStore:
    """Same interface, no database. For tests of the middleware and the routes."""
    users: dict[str, dict] = field(default_factory=dict)
    sessions: dict[str, dict] = field(default_factory=dict)
    events: list[tuple] = field(default_factory=list)
    _next_id: int = 1

    async def get_user_by_email(self, email: str) -> dict | None:
        return self.users.get(email)

    async def create_user(self, email: str, password_hash: str, role: str, expires_at: datetime | None = None) -> int:
        uid, self._next_id = self._next_id, self._next_id + 1
        self.users[email] = {"id": uid, "email": email, "password_hash": password_hash, "role": role, "is_active": True,
                             "expires_at": expires_at, "failed_attempts": 0, "locked_until": None}
        return uid

    async def update_hash(self, user_id: int, password_hash: str) -> None:
        for u in self.users.values():
            if u["id"] == user_id:
                u["password_hash"] = password_hash

    async def record_failure(self, user_id: int, max_attempts: int, lock_minutes: int) -> bool:
        for u in self.users.values():
            if u["id"] == user_id:
                u["failed_attempts"] += 1
                if u["failed_attempts"] >= max_attempts:
                    u["failed_attempts"], u["locked_until"] = 0, _now() + timedelta(minutes=lock_minutes)
                    return True
        return False

    async def record_success(self, user_id: int) -> None:
        for u in self.users.values():
            if u["id"] == user_id:
                u["failed_attempts"], u["locked_until"] = 0, None

    async def create_session(self, user_id: int, token: str, expires_at: datetime, ip: str, user_agent: str) -> None:
        self.sessions[token_hash(token)] = {"user_id": user_id, "expires_at": expires_at, "last_seen_at": _now(),
                                            "revoked": False}

    async def get_session(self, token: str) -> dict | None:
        s = self.sessions.get(token_hash(token))
        if not s or s["revoked"] or s["expires_at"] <= _now():
            return None
        user = next((u for u in self.users.values() if u["id"] == s["user_id"]), None)
        if not user or not user["is_active"] or (user["expires_at"] and user["expires_at"] <= _now()):
            return None
        return {"user_id": user["id"], "email": user["email"], "role": user["role"],
                "last_seen_at": s["last_seen_at"], "expires_at": s["expires_at"]}

    async def touch_session(self, token: str) -> None:
        if (s := self.sessions.get(token_hash(token))):
            s["last_seen_at"] = _now()

    async def revoke_session(self, token: str) -> None:
        if (s := self.sessions.get(token_hash(token))):
            s["revoked"] = True

    async def purge_sessions(self) -> None:
        return None

    async def count_users(self) -> int:
        return len(self.users)

    async def audit(self, event: str, email: str | None = None, user_id: int | None = None, ip: str | None = None,
                    detail: str | None = None) -> None:
        self.events.append((event, email, ip, detail))
