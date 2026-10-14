"""Auth data access: users, sessions, two-factor state, demo links and the audit trail (Postgres), plus an
in-memory twin with the same interface for tests."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ROLES = ("admin", "client", "demo")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Only this hash is stored, so a copy of the database cannot be used to log in."""
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


_USER_LIST_COLUMNS = ("id, email, role, is_active, expires_at, failed_attempts, locked_until, last_login_at, created_at, "
                      "must_change_password, totp_enabled")


class AuthStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    # -- users -----------------------------------------------------------------------------------
    async def get_user_by_email(self, email: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM auth_users WHERE email = $1", email)
        return dict(row) if row else None

    async def get_user_by_id(self, user_id: int) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM auth_users WHERE id = $1", user_id)
        return dict(row) if row else None

    async def create_user(self, email: str, password_hash: str, role: str, expires_at: datetime | None = None,
                          must_change: bool = False) -> int:
        return await self.pool.fetchval(
            "INSERT INTO auth_users (email, password_hash, role, expires_at, must_change_password) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id", email, password_hash, role, expires_at, must_change)

    async def list_users(self) -> list[dict]:
        rows = await self.pool.fetch(f"SELECT {_USER_LIST_COLUMNS} FROM auth_users ORDER BY email")
        return [dict(r) for r in rows]

    async def set_active(self, email: str, active: bool) -> bool:
        return (await self.pool.execute("UPDATE auth_users SET is_active = $2 WHERE email = $1", email, active)) != "UPDATE 0"

    async def set_password(self, email: str, password_hash: str, must_change: bool = False) -> bool:
        res = await self.pool.execute(
            "UPDATE auth_users SET password_hash = $2, must_change_password = $3, failed_attempts = 0, locked_until = NULL "
            "WHERE email = $1", email, password_hash, must_change)
        return res != "UPDATE 0"

    async def set_password_by_id(self, user_id: int, password_hash: str) -> None:
        await self.pool.execute(
            "UPDATE auth_users SET password_hash = $2, must_change_password = FALSE WHERE id = $1", user_id, password_hash)

    async def update_hash(self, user_id: int, password_hash: str) -> None:
        await self.pool.execute("UPDATE auth_users SET password_hash = $2 WHERE id = $1", user_id, password_hash)

    async def record_failure(self, user_id: int, max_attempts: int, lock_minutes: int) -> bool:
        """Count a wrong password or code; at `max_attempts` lock the account for `lock_minutes` and start counting
        again. Returns True when this failure locked it."""
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
            SELECT s.token_hash, s.user_id, s.last_seen_at, s.expires_at, u.email, u.role,
                   u.must_change_password, u.totp_enabled
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

    async def revoke_other_sessions(self, user_id: int, keep_token: str) -> int:
        res = await self.pool.execute(
            "UPDATE auth_sessions SET revoked_at = now() WHERE revoked_at IS NULL AND user_id = $1 AND token_hash <> $2",
            user_id, token_hash(keep_token))
        return int(res.split()[-1])

    async def purge_sessions(self) -> None:
        await self.pool.execute(
            "DELETE FROM auth_sessions WHERE expires_at < now() - interval '1 day' OR revoked_at < now() - interval '1 day'")
        await self.pool.execute("DELETE FROM auth_pending WHERE expires_at < now()")

    async def count_users(self) -> int:
        return await self.pool.fetchval("SELECT count(*) FROM auth_users")

    # -- two-factor ------------------------------------------------------------------------------
    async def totp_begin(self, user_id: int, secret: str) -> None:
        """Remember a new secret that is not yet in force: it only counts once a code from it has been entered."""
        await self.pool.execute(
            "UPDATE auth_users SET totp_secret = $2, totp_enabled = FALSE, totp_last_step = NULL WHERE id = $1", user_id, secret)

    async def totp_enable(self, user_id: int, step: int) -> None:
        await self.pool.execute("UPDATE auth_users SET totp_enabled = TRUE, totp_last_step = $2 WHERE id = $1", user_id, step)

    async def totp_disable(self, user_id: int) -> None:
        await self.pool.execute(
            "UPDATE auth_users SET totp_secret = NULL, totp_enabled = FALSE, totp_last_step = NULL WHERE id = $1", user_id)
        await self.pool.execute("DELETE FROM auth_recovery_codes WHERE user_id = $1", user_id)

    async def totp_set_last_step(self, user_id: int, step: int) -> None:
        await self.pool.execute("UPDATE auth_users SET totp_last_step = $2 WHERE id = $1", user_id, step)

    async def replace_recovery_codes(self, user_id: int, hashes: list[str]) -> None:
        await self.pool.execute("DELETE FROM auth_recovery_codes WHERE user_id = $1", user_id)
        await self.pool.executemany(
            "INSERT INTO auth_recovery_codes (user_id, code_hash) VALUES ($1, $2)", [(user_id, h) for h in hashes])

    async def consume_recovery_code(self, user_id: int, code_hash: str) -> bool:
        res = await self.pool.execute(
            "UPDATE auth_recovery_codes SET used_at = now() WHERE user_id = $1 AND code_hash = $2 AND used_at IS NULL",
            user_id, code_hash)
        return res != "UPDATE 0"

    async def recovery_codes_left(self, user_id: int) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM auth_recovery_codes WHERE user_id = $1 AND used_at IS NULL", user_id)

    # -- half-finished logins (password right, second factor to come) ------------------------------
    async def create_pending(self, user_id: int, token: str, expires_at: datetime) -> None:
        await self.pool.execute(
            "INSERT INTO auth_pending (token_hash, user_id, expires_at) VALUES ($1, $2, $3)", token_hash(token), user_id, expires_at)

    async def get_pending(self, token: str) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT user_id, attempts, expires_at FROM auth_pending WHERE token_hash = $1 AND expires_at > now()", token_hash(token))
        return dict(row) if row else None

    async def bump_pending(self, token: str) -> int:
        return await self.pool.fetchval(
            "UPDATE auth_pending SET attempts = attempts + 1 WHERE token_hash = $1 RETURNING attempts", token_hash(token)) or 99

    async def delete_pending(self, token: str) -> None:
        await self.pool.execute("DELETE FROM auth_pending WHERE token_hash = $1", token_hash(token))

    # -- demo links ------------------------------------------------------------------------------
    async def create_demo_link(self, label: str, token: str, user_id: int, expires_at: datetime) -> int:
        return await self.pool.fetchval(
            "INSERT INTO auth_demo_links (token_hash, label, user_id, expires_at) VALUES ($1, $2, $3, $4) RETURNING id",
            token_hash(token), label, user_id, expires_at)

    async def get_demo_link(self, token: str) -> dict | None:
        """The link and its demo user, if the link is neither revoked nor expired."""
        row = await self.pool.fetchrow(
            """
            SELECT l.id, l.label, l.user_id, l.expires_at, u.email, u.role
            FROM auth_demo_links l JOIN auth_users u ON u.id = l.user_id
            WHERE l.token_hash = $1 AND l.revoked_at IS NULL AND l.expires_at > now() AND u.is_active
            """, token_hash(token))
        return dict(row) if row else None

    async def touch_demo_link(self, link_id: int) -> None:
        await self.pool.execute(
            "UPDATE auth_demo_links SET use_count = use_count + 1, last_used_at = now() WHERE id = $1", link_id)

    async def list_demo_links(self) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, label, created_at, expires_at, revoked_at, use_count, last_used_at FROM auth_demo_links ORDER BY id DESC")
        return [dict(r) for r in rows]

    async def revoke_demo_link(self, link_id: int) -> bool:
        row = await self.pool.fetchrow(
            "UPDATE auth_demo_links SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL RETURNING user_id", link_id)
        if not row:
            return False
        await self.pool.execute("UPDATE auth_users SET is_active = FALSE WHERE id = $1", row["user_id"])
        await self.pool.execute("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", row["user_id"])
        return True

    # -- audit -----------------------------------------------------------------------------------
    async def audit(self, event: str, email: str | None = None, user_id: int | None = None, ip: str | None = None,
                    detail: str | None = None) -> None:
        await self.pool.execute(
            "INSERT INTO auth_audit (event, email, user_id, ip, detail) VALUES ($1, $2, $3, $4, $5)",
            event, email, user_id, ip, detail)

    async def recent_audit(self, limit: int = 100) -> list[dict]:
        rows = await self.pool.fetch("SELECT at, event, email, ip, detail FROM auth_audit ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]


@dataclass
class MemoryAuthStore:
    """Same interface, no database. For tests of the middleware and the routes."""
    users: dict[str, dict] = field(default_factory=dict)
    sessions: dict[str, dict] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    recovery: dict[int, dict[str, bool]] = field(default_factory=dict)     # user_id -> {code_hash: used}
    demo_links: dict[int, dict] = field(default_factory=dict)
    events: list[tuple] = field(default_factory=list)
    _next_id: int = 1
    _next_link: int = 1

    def _user(self, user_id: int) -> dict | None:
        return next((u for u in self.users.values() if u["id"] == user_id), None)

    async def get_user_by_email(self, email: str) -> dict | None:
        return self.users.get(email)

    async def get_user_by_id(self, user_id: int) -> dict | None:
        return self._user(user_id)

    async def create_user(self, email: str, password_hash: str, role: str, expires_at: datetime | None = None,
                          must_change: bool = False) -> int:
        uid, self._next_id = self._next_id, self._next_id + 1
        self.users[email] = {"id": uid, "email": email, "password_hash": password_hash, "role": role, "is_active": True,
                             "expires_at": expires_at, "failed_attempts": 0, "locked_until": None,
                             "must_change_password": must_change, "totp_secret": None, "totp_enabled": False,
                             "totp_last_step": None, "last_login_at": None, "created_at": _now()}
        return uid

    async def list_users(self) -> list[dict]:
        return sorted((dict(u) for u in self.users.values()), key=lambda u: u["email"])

    async def set_active(self, email: str, active: bool) -> bool:
        if email not in self.users:
            return False
        self.users[email]["is_active"] = active
        return True

    async def set_password(self, email: str, password_hash: str, must_change: bool = False) -> bool:
        u = self.users.get(email)
        if not u:
            return False
        u.update(password_hash=password_hash, must_change_password=must_change, failed_attempts=0, locked_until=None)
        return True

    async def set_password_by_id(self, user_id: int, password_hash: str) -> None:
        self._user(user_id).update(password_hash=password_hash, must_change_password=False)

    async def update_hash(self, user_id: int, password_hash: str) -> None:
        self._user(user_id)["password_hash"] = password_hash

    async def record_failure(self, user_id: int, max_attempts: int, lock_minutes: int) -> bool:
        u = self._user(user_id)
        u["failed_attempts"] += 1
        if u["failed_attempts"] >= max_attempts:
            u["failed_attempts"], u["locked_until"] = 0, _now() + timedelta(minutes=lock_minutes)
            return True
        return False

    async def record_success(self, user_id: int) -> None:
        self._user(user_id).update(failed_attempts=0, locked_until=None, last_login_at=_now())

    async def create_session(self, user_id: int, token: str, expires_at: datetime, ip: str, user_agent: str) -> None:
        self.sessions[token_hash(token)] = {"user_id": user_id, "expires_at": expires_at, "last_seen_at": _now(), "revoked": False}

    async def get_session(self, token: str) -> dict | None:
        s = self.sessions.get(token_hash(token))
        if not s or s["revoked"] or s["expires_at"] <= _now():
            return None
        u = self._user(s["user_id"])
        if not u or not u["is_active"] or (u["expires_at"] and u["expires_at"] <= _now()):
            return None
        return {"user_id": u["id"], "email": u["email"], "role": u["role"], "last_seen_at": s["last_seen_at"],
                "expires_at": s["expires_at"], "must_change_password": u["must_change_password"], "totp_enabled": u["totp_enabled"]}

    async def touch_session(self, token: str) -> None:
        if (s := self.sessions.get(token_hash(token))):
            s["last_seen_at"] = _now()

    async def revoke_session(self, token: str) -> None:
        if (s := self.sessions.get(token_hash(token))):
            s["revoked"] = True

    async def revoke_user_sessions(self, email: str) -> int:
        uid = self.users[email]["id"] if email in self.users else None
        n = 0
        for s in self.sessions.values():
            if s["user_id"] == uid and not s["revoked"]:
                s["revoked"], n = True, n + 1
        return n

    async def revoke_other_sessions(self, user_id: int, keep_token: str) -> int:
        keep, n = token_hash(keep_token), 0
        for h, s in self.sessions.items():
            if s["user_id"] == user_id and h != keep and not s["revoked"]:
                s["revoked"], n = True, n + 1
        return n

    async def purge_sessions(self) -> None:
        return None

    async def count_users(self) -> int:
        return len(self.users)

    async def totp_begin(self, user_id: int, secret: str) -> None:
        self._user(user_id).update(totp_secret=secret, totp_enabled=False, totp_last_step=None)

    async def totp_enable(self, user_id: int, step: int) -> None:
        self._user(user_id).update(totp_enabled=True, totp_last_step=step)

    async def totp_disable(self, user_id: int) -> None:
        self._user(user_id).update(totp_secret=None, totp_enabled=False, totp_last_step=None)
        self.recovery.pop(user_id, None)

    async def totp_set_last_step(self, user_id: int, step: int) -> None:
        self._user(user_id)["totp_last_step"] = step

    async def replace_recovery_codes(self, user_id: int, hashes: list[str]) -> None:
        self.recovery[user_id] = {h: False for h in hashes}

    async def consume_recovery_code(self, user_id: int, code_hash: str) -> bool:
        codes = self.recovery.get(user_id, {})
        if code_hash in codes and not codes[code_hash]:
            codes[code_hash] = True
            return True
        return False

    async def recovery_codes_left(self, user_id: int) -> int:
        return sum(1 for used in self.recovery.get(user_id, {}).values() if not used)

    async def create_pending(self, user_id: int, token: str, expires_at: datetime) -> None:
        self.pending[token_hash(token)] = {"user_id": user_id, "expires_at": expires_at, "attempts": 0}

    async def get_pending(self, token: str) -> dict | None:
        p = self.pending.get(token_hash(token))
        return dict(p) if p and p["expires_at"] > _now() else None

    async def bump_pending(self, token: str) -> int:
        p = self.pending.get(token_hash(token))
        if not p:
            return 99
        p["attempts"] += 1
        return p["attempts"]

    async def delete_pending(self, token: str) -> None:
        self.pending.pop(token_hash(token), None)

    async def create_demo_link(self, label: str, token: str, user_id: int, expires_at: datetime) -> int:
        lid, self._next_link = self._next_link, self._next_link + 1
        self.demo_links[lid] = {"id": lid, "token_hash": token_hash(token), "label": label, "user_id": user_id,
                                "created_at": _now(), "expires_at": expires_at, "revoked_at": None, "use_count": 0,
                                "last_used_at": None}
        return lid

    async def get_demo_link(self, token: str) -> dict | None:
        h = token_hash(token)
        for l in self.demo_links.values():
            if l["token_hash"] == h and not l["revoked_at"] and l["expires_at"] > _now():
                u = self._user(l["user_id"])
                if u and u["is_active"]:
                    return {"id": l["id"], "label": l["label"], "user_id": u["id"], "expires_at": l["expires_at"],
                            "email": u["email"], "role": u["role"]}
        return None

    async def touch_demo_link(self, link_id: int) -> None:
        l = self.demo_links[link_id]
        l["use_count"] += 1
        l["last_used_at"] = _now()

    async def list_demo_links(self) -> list[dict]:
        return sorted((dict(l) for l in self.demo_links.values()), key=lambda l: -l["id"])

    async def revoke_demo_link(self, link_id: int) -> bool:
        l = self.demo_links.get(link_id)
        if not l or l["revoked_at"]:
            return False
        l["revoked_at"] = _now()
        self._user(l["user_id"])["is_active"] = False
        for s in self.sessions.values():
            if s["user_id"] == l["user_id"]:
                s["revoked"] = True
        return True

    async def audit(self, event: str, email: str | None = None, user_id: int | None = None, ip: str | None = None,
                    detail: str | None = None) -> None:
        self.events.append((event, email, ip, detail))

    async def recent_audit(self, limit: int = 100) -> list[dict]:
        return [{"at": _now(), "event": e, "email": m, "ip": i, "detail": d} for e, m, i, d in reversed(self.events[-limit:])]
