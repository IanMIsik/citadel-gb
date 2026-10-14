"""`citadel user ...` and `citadel demo-link ...`: create and manage logins from the command line (there is
deliberately no self sign-up; the browser has an admin page for the same jobs)."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import datetime, timezone

from . import passwords, service
from .store import ROLES, AuthStore


def add_subparsers(sub) -> None:
    user = sub.add_parser("user", help="Manage logins for the web app")
    actions = user.add_subparsers(dest="user_command", required=True)

    add = actions.add_parser("add", help="Create a user")
    add.add_argument("email")
    add.add_argument("--role", choices=ROLES, default="client")
    add.add_argument("--expires-days", type=int, default=None, help="the login stops working after this many days")
    add.add_argument("--generate", action="store_true", help="generate a random password and print it once")
    add.add_argument("--no-force-change", action="store_true", help="do not make them choose their own password at first sign-in")

    actions.add_parser("list", help="List users")
    for name, text in (("disable", "Stop a user logging in (and end their sessions)"), ("enable", "Allow a disabled user to log in"),
                       ("logout", "End all of a user's sessions"), ("set-password", "Set a new password (also unlocks the account)"),
                       ("reset-2fa", "Remove a user's two-factor (they set it up again at next sign-in)")):
        p = actions.add_parser(name, help=text)
        p.add_argument("email")
        if name == "set-password":
            p.add_argument("--generate", action="store_true")
            p.add_argument("--no-force-change", action="store_true")

    demo = sub.add_parser("demo-link", help="Create and manage demo links (read-only access without a password)")
    d_actions = demo.add_subparsers(dest="demo_command", required=True)
    create = d_actions.add_parser("create", help="Make a link")
    create.add_argument("label", help="who it is for, e.g. 'Acme Energy'")
    create.add_argument("--days", type=float, default=3.0, help="how long the link works (default 3)")
    create.add_argument("--base-url", default="", help="e.g. https://citadel.example.com (default: PUBLIC_BASE_URL)")
    d_actions.add_parser("list", help="List demo links")
    revoke = d_actions.add_parser("revoke", help="Withdraw a link and end its sessions")
    revoke.add_argument("id", type=int)


def _new_password(generate: bool) -> str:
    if generate:
        return passwords.generate_password()
    first = getpass.getpass("Password (min 12 characters): ")
    if getpass.getpass("Again: ") != first:
        sys.exit("The two entries differ.")
    return first


async def _run_user(args, store: AuthStore) -> None:
    email = args.email.strip().lower() if getattr(args, "email", None) else None
    cmd = args.user_command
    if cmd == "list":
        for u in await store.list_users():
            state = ("disabled" if not u["is_active"]
                     else "locked" if u["locked_until"] and u["locked_until"] > datetime.now(timezone.utc) else "active")
            flags = ("2fa " if u["totp_enabled"] else "") + ("must-change-password" if u["must_change_password"] else "")
            print(f"{u['email']:42s} {u['role']:7s} {state:9s} {flags:22s} last login {u['last_login_at'] or '-'}  expires {u['expires_at'] or '-'}")
    elif cmd == "add":
        if not service.valid_email(email):
            sys.exit("That does not look like an email address.")
        if await store.get_user_by_email(email):
            sys.exit(f"{email} already exists.")
        password = _new_password(args.generate)
        problems = passwords.password_problems(password, email)
        if problems:
            sys.exit("Password " + "; ".join(problems) + ".")
        await service.create_user(store, email, args.role, password, args.expires_days, force_change=not args.no_force_change)
        await store.audit("user_created", email=email, detail=f"role={args.role} via cli")
        print(f"created {email} ({args.role})" + ("; they must choose their own password at first sign-in" if not args.no_force_change else ""))
        if args.generate:
            print(f"password: {password}\n(shown once; it is not stored)")
    elif cmd in ("disable", "enable"):
        if not await store.set_active(email, cmd == "enable"):
            sys.exit(f"no such user {email}")
        if cmd == "disable":
            await store.revoke_user_sessions(email)
        await store.audit(f"user_{cmd}d", email=email, detail="via cli")
        print(f"{cmd}d {email}")
    elif cmd == "logout":
        print(f"ended {await store.revoke_user_sessions(email)} session(s) for {email}")
    elif cmd == "reset-2fa":
        user = await store.get_user_by_email(email)
        if not user:
            sys.exit(f"no such user {email}")
        await store.totp_disable(user["id"])
        await store.revoke_user_sessions(email)
        await store.audit("admin_reset_2fa", email=email, user_id=user["id"], detail="via cli")
        print(f"two-factor removed for {email}; their sessions were ended")
    elif cmd == "set-password":
        password = _new_password(args.generate)
        problems = passwords.password_problems(password, email)
        if problems:
            sys.exit("Password " + "; ".join(problems) + ".")
        if not await store.set_password(email, await asyncio.to_thread(passwords.hash_password, password), must_change=not args.no_force_change):
            sys.exit(f"no such user {email}")
        await store.revoke_user_sessions(email)
        await store.audit("password_set", email=email, detail="via cli")
        print(f"password set for {email}; their sessions were ended" + ("; they must choose their own at next sign-in" if not args.no_force_change else ""))
        if args.generate:
            print(f"password: {password}\n(shown once)")


async def _run_demo(args, store: AuthStore) -> None:
    from ..config import settings

    if args.demo_command == "create":
        link = await service.create_demo_link(store, args.label, args.days)
        base = (args.base_url or settings.public_base_url or "http://127.0.0.1:8000").rstrip("/")
        await store.audit("demo_link_created", email=link["email"], detail=f"{args.label} via cli")
        print(f"demo link #{link['id']} for {args.label!r}, valid until {link['expires_at']:%Y-%m-%d %H:%M} UTC\n{base}{link['path']}\n(shown once; revoke with: demo-link revoke {link['id']})")
    elif args.demo_command == "list":
        for l in await store.list_demo_links():
            state = "revoked" if l["revoked_at"] else "expired" if l["expires_at"] <= datetime.now(timezone.utc) else "live"
            print(f"#{l['id']:<4d} {state:8s} {l['label'][:40]:40s} until {l['expires_at']:%Y-%m-%d %H:%M}  used {l['use_count']}x")
    elif args.demo_command == "revoke":
        if not await store.revoke_demo_link(args.id):
            sys.exit("no such live link")
        await store.audit("demo_link_revoked", detail=f"#{args.id} via cli")
        print(f"revoked link #{args.id}; its sessions were ended")


async def run(args: argparse.Namespace) -> None:
    from ..config import settings
    from ..storage import db

    pool = await db.create_pool(settings.database_url)
    store = AuthStore(pool)
    try:
        if args.command == "user":
            await _run_user(args, store)
        else:
            await _run_demo(args, store)
    finally:
        await pool.close()
