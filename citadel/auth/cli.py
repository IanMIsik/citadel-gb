"""`citadel user ...`: create and manage logins (there is deliberately no self sign-up)."""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timedelta, timezone

from . import passwords
from .store import ROLES, AuthStore


def add_subparsers(sub) -> None:
    user = sub.add_parser("user", help="Manage logins for the web app")
    actions = user.add_subparsers(dest="user_command", required=True)

    add = actions.add_parser("add", help="Create a user")
    add.add_argument("email")
    add.add_argument("--role", choices=ROLES, default="client")
    add.add_argument("--expires-days", type=int, default=None, help="the login stops working after this many days")
    add.add_argument("--generate", action="store_true", help="generate a random password and print it once")

    actions.add_parser("list", help="List users")
    for name, text in (("disable", "Stop a user logging in (and end their sessions)"), ("enable", "Allow a disabled user to log in"),
                       ("logout", "End all of a user's sessions"), ("set-password", "Set a new password (also unlocks the account)")):
        p = actions.add_parser(name, help=text)
        p.add_argument("email")
        if name == "set-password":
            p.add_argument("--generate", action="store_true")


def _new_password(email: str, generate: bool) -> str:
    if generate:
        return passwords.generate_password()
    first = getpass.getpass("Password (min 12 characters): ")
    if getpass.getpass("Again: ") != first:
        sys.exit("The two entries differ.")
    return first


async def run(args: argparse.Namespace) -> None:
    from ..config import settings
    from ..storage import db

    pool = await db.create_pool(settings.database_url)
    store = AuthStore(pool)
    try:
        email = getattr(args, "email", "").strip().lower() if getattr(args, "email", None) else None
        cmd = args.user_command
        if cmd == "list":
            for u in await store.list_users():
                state = "disabled" if not u["is_active"] else "locked" if u["locked_until"] and u["locked_until"] > datetime.now(timezone.utc) else "active"
                print(f"{u['email']:40s} {u['role']:7s} {state:9s} last login {u['last_login_at'] or '-'}  expires {u['expires_at'] or '-'}")
        elif cmd == "add":
            if await store.get_user_by_email(email):
                sys.exit(f"{email} already exists.")
            password = _new_password(email, args.generate)
            problems = passwords.password_problems(password, email)
            if problems:
                sys.exit("Password " + "; ".join(problems) + ".")
            expires = datetime.now(timezone.utc) + timedelta(days=args.expires_days) if args.expires_days else None
            await store.create_user(email, passwords.hash_password(password), args.role, expires)
            await store.audit("user_created", email=email, detail=f"role={args.role} via cli")
            print(f"created {email} ({args.role})" + (f"\npassword: {password}\n(shown once; it is not stored)" if args.generate else ""))
        elif cmd in ("disable", "enable"):
            if not await store.set_active(email, cmd == "enable"):
                sys.exit(f"no such user {email}")
            if cmd == "disable":
                await store.revoke_user_sessions(email)
            await store.audit(f"user_{cmd}d", email=email, detail="via cli")
            print(f"{cmd}d {email}")
        elif cmd == "logout":
            print(f"ended {await store.revoke_user_sessions(email)} session(s) for {email}")
        elif cmd == "set-password":
            password = _new_password(email, args.generate)
            problems = passwords.password_problems(password, email)
            if problems:
                sys.exit("Password " + "; ".join(problems) + ".")
            if not await store.set_password(email, passwords.hash_password(password)):
                sys.exit(f"no such user {email}")
            await store.revoke_user_sessions(email)
            await store.audit("password_set", email=email, detail="via cli")
            print(f"password set for {email}; their sessions were ended" + (f"\npassword: {password}\n(shown once)" if args.generate else ""))
    finally:
        await pool.close()
