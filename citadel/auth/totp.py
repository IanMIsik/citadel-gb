"""Time-based one-time passwords (RFC 6238: HMAC-SHA1, 6 digits, 30 s steps), as authenticator apps expect,
plus recovery codes and the QR code for enrolment. Standard library only, apart from `segno` for the QR."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1          # accept the code for the previous, current and next step (clock drift between phone and server)
ISSUER = "Citadel"


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")      # 160 bits, as the RFC recommends


def _code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(value).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // STEP_SECONDS)


def code_now(secret: str, now: float | None = None) -> str:
    return _code_at(secret, current_step(now))


def verify(secret: str, code: str, last_used_step: int | None = None, now: float | None = None) -> int | None:
    """The time step the code belongs to if it is valid, else None. A step at or before `last_used_step` is
    refused, so a code that has been used once (or one older than the last) cannot be used again."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step_now = current_step(now)
    for step in range(step_now - WINDOW, step_now + WINDOW + 1):
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(_code_at(secret, step), code):
            return step
    return None


def otpauth_uri(email: str, secret: str) -> str:
    label = quote(f"{ISSUER}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"


def qr_data_uri(uri: str) -> str:
    """The QR code as an inline SVG data URI (the page's content policy allows data: images)."""
    import segno

    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2, dark="#000000", light="#ffffff")


# -- recovery codes ------------------------------------------------------------------------------------------
def new_recovery_codes(n: int = 10) -> list[str]:
    """Single-use codes for when the phone is lost, like `k3f9-x7m2`."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"          # no look-alikes (i, l, o, 0, 1)
    return ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
            for _ in range(n)]


def normalise_recovery_code(code: str) -> str:
    c = "".join(ch for ch in code.lower() if ch.isalnum())
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 else ""


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalise_recovery_code(code).encode()).hexdigest()
