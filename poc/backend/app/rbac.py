"""Role-based access control for people — the human half of `gate.py`.

The symmetry is the point. `gate.TOOL_SPECS` says which agent may call which
tool; `CAPABILITIES` below says which role may perform which human action. Both
are consulted before the write, in Python, and both return a refusal that names
the reason rather than silently doing nothing.

HIDING A BUTTON IS NOT A CONTROL. The React app will hide actions a role cannot
perform, because showing them would be bad design. That is a convenience, not a
security boundary: every mutating endpoint re-checks here, so an underwriter
posting directly to the approvals endpoint is refused exactly as an agent is
refused a tool it does not hold.

Demo credentials are deliberately weak and deliberately hashed. The password is
public (it is in the skill file), but storing it in plaintext would teach the
wrong lesson to anyone reading this as a reference implementation, and the
hashing path is the one that has to work in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import lru_cache
from typing import Any

import bcrypt
from jose import JWTError, jwt

from .config import get_settings

ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 8

# bcrypt directly, not passlib. passlib 1.7.4 (its last release, 2020) probes
# the backend by hashing an over-length secret; bcrypt 5.x rejects that instead
# of truncating it, so every hash call raises ValueError. Pinning bcrypt back
# below 5 would work and would also mean shipping an unmaintained library to do
# two function calls. See the note in requirements.txt.
BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> bytes:
    """Hash a password. Rejects over-length input rather than truncating it.

    bcrypt ignores everything past 72 bytes. Silently truncating means two
    different long passwords authenticate each other, so this refuses instead.
    """
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"password is {len(raw)} bytes; bcrypt accepts at most "
            f"{BCRYPT_MAX_BYTES} and silently ignores the rest"
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt())


def verify_password(password: str, hashed: bytes) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed)
    except ValueError:
        return False


class Role(StrEnum):
    ANALYST = "analyst"
    SUPERVISOR = "supervisor"
    UNDERWRITER = "underwriter"


@dataclass(frozen=True, slots=True)
class User:
    username: str
    name: str
    role: Role
    queue: str | None = None


# ---------------------------------------------------------------------------
# The demo roster. Names and queues are verbatim from the reference design.
# ---------------------------------------------------------------------------
DEMO_PASSWORD = "Coforge@123"

USERS: dict[str, User] = {
    "analyst1": User("analyst1", "Priya Nair", Role.ANALYST, "A"),
    "analyst2": User("analyst2", "Arjun Mehta", Role.ANALYST, "B"),
    "analyst3": User("analyst3", "Lena Rossi", Role.ANALYST, "C"),
    "supervisor": User("supervisor", "Marcus Webb", Role.SUPERVISOR),
    "underwriter": User("underwriter", "Diane Foster", Role.UNDERWRITER),
}


@lru_cache(maxsize=1)
def _password_hashes() -> dict[str, bytes]:
    """Hashed once per process. bcrypt is slow on purpose, so do not repeat it."""
    return {u: hash_password(DEMO_PASSWORD) for u in USERS}


@lru_cache(maxsize=1)
def _decoy_hash() -> bytes:
    return hash_password("not-a-real-password")


def authenticate(username: str, password: str) -> User | None:
    """Verify credentials. Returns None on any failure, without saying which."""
    user = USERS.get(username)
    hashed = _password_hashes().get(username)
    if user is None or hashed is None:
        # Still verify against a decoy so a missing username and a wrong
        # password take the same time. It costs nothing to not leak which
        # usernames exist.
        verify_password(password, _decoy_hash())
        return None
    return user if verify_password(password, hashed) else None


# ---------------------------------------------------------------------------
# The capability matrix
# ---------------------------------------------------------------------------
class Action(StrEnum):
    VIEW_PIPELINE = "view_pipeline"
    VIEW_EXCEPTIONS = "view_exceptions"
    VIEW_HUB = "view_hub"
    VIEW_AUDIT = "view_audit"
    VIEW_APPROVALS = "view_approvals"

    START_SCAN = "start_scan"
    WORK_EXCEPTION = "work_exception"        # verify / recalc / request / override
    APPROVE = "approve"                      # sign off a proposal
    CONFIRM_GATED = "confirm_gated"          # authorise a money-spending tool call
    DECIDE_LOAN = "decide_loan"              # the underwriting decision


CAPABILITIES: dict[Role, frozenset[Action]] = {
    Role.ANALYST: frozenset({
        Action.VIEW_PIPELINE, Action.VIEW_EXCEPTIONS, Action.VIEW_HUB,
        Action.VIEW_AUDIT, Action.WORK_EXCEPTION,
    }),
    Role.SUPERVISOR: frozenset({
        Action.VIEW_PIPELINE, Action.VIEW_EXCEPTIONS, Action.VIEW_HUB,
        Action.VIEW_AUDIT, Action.VIEW_APPROVALS, Action.START_SCAN,
        Action.WORK_EXCEPTION, Action.APPROVE, Action.CONFIRM_GATED,
    }),
    Role.UNDERWRITER: frozenset({
        Action.VIEW_PIPELINE, Action.VIEW_EXCEPTIONS, Action.VIEW_HUB,
        Action.VIEW_AUDIT, Action.DECIDE_LOAN,
    }),
}

# Why these lines fall where they do:
#
#   A supervisor cannot render an underwriting decision. Approving a fix and
#   deciding a loan are different jobs held by different people, and collapsing
#   them would make the approval lane decorative.
#
#   An underwriter cannot work a HITL case. They receive files that are ready;
#   reaching back into the exception queue would let them clear their own
#   blockers.
#
#   Only a supervisor confirms a gated tool call, because that is money leaving
#   the building.


class Denied(Exception):
    """A person attempted something their role does not permit."""

    def __init__(self, action: Action | str, role: Role | str, detail: str = ""):
        self.action, self.role = str(action), str(role)
        super().__init__(
            f"{self.role} may not {self.action}"
            + (f": {detail}" if detail else "")
        )


def may(user: User, action: Action) -> bool:
    return action in CAPABILITIES[user.role]


def require(user: User, action: Action, detail: str = "") -> None:
    """Raise unless this user may do this. Call before the write, always."""
    if not may(user, action):
        raise Denied(action, user.role, detail)


def require_queue(user: User, queue: str | None) -> None:
    """An analyst works their own queue.

    Viewing another queue is allowed — the reference lets analysts see the whole
    board — but acting on it is not. A supervisor works any queue.
    """
    if user.role is not Role.ANALYST:
        return
    if queue is not None and queue != user.queue:
        raise Denied(
            Action.WORK_EXCEPTION, user.role,
            f"queue {queue} belongs to another analyst; you hold queue {user.queue}",
        )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def issue_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.username,
        "name": user.name,
        "role": str(user.role),
        "queue": user.queue,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def read_token(token: str) -> User:
    """Decode a token into a user. Raises JWTError on anything wrong.

    The user is rebuilt from `USERS`, not from the token body. A token carries
    the username; the role comes from the roster. Trusting a role claim from the
    token would mean anyone who could mint one could mint a supervisor.
    """
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.jwt_secret, algorithms=[ALGORITHM]
        )
    except JWTError as exc:
        raise JWTError(f"invalid token: {exc}") from exc

    user = USERS.get(str(payload.get("sub", "")))
    if user is None:
        raise JWTError("token names a user who does not exist")
    return user
