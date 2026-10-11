"""Password hashing (argon2id) and the rules a new password must meet."""
from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 256     # a cap, so an absurdly long "password" cannot be used to burn CPU on the hash

_hasher = PasswordHasher()   # argon2id with the library's current recommended parameters

# Not a breach list -- only the passwords everyone tries first. A fuller check (for example against
# Have I Been Pwned's range API) can replace this later without changing callers.
_COMMON = {
    "password", "password1", "password12", "password123", "passw0rd", "letmein", "welcome", "welcome1",
    "qwerty", "qwertyuiop", "qwerty123", "iloveyou", "admin", "administrator", "changeme", "citadel",
    "123456", "12345678", "123456789", "1234567890", "111111", "000000", "abc123", "monkey", "dragon",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# A real hash to check against when the email is unknown, so a wrong email takes as long to reject as a
# wrong password and the response time does not reveal which accounts exist.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def password_problems(password: str, email: str = "") -> list[str]:
    """Why `password` is not acceptable (empty list: it is)."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters")
    if len(set(password)) < 5:
        problems.append("must not be a repeated pattern")
    if password.lower() in _COMMON or password.lower().rstrip("0123456789!") in _COMMON:
        problems.append("is one of the most common passwords")
    local = email.split("@")[0].lower()
    if len(local) >= 4 and local in password.lower():
        problems.append("must not contain your email name")
    return problems


def generate_password() -> str:
    """A random password that satisfies password_problems()."""
    return secrets.token_urlsafe(18)
