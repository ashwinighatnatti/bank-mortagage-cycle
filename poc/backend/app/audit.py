"""Append-only, hash-chained audit trail.

Every row stores sha256(prev_hash || canonical_json(payload)). Altering or
deleting any historical row breaks every hash after it, and `verify_chain()`
reports the first break.

This is cheap to build and it changes the conversation with a compliance
reviewer: "we log everything" becomes something they can check, in front of
you, in one request.

Two rules this module exists to keep:
  · nothing mutates state without an audit line (enforced in the tool decorator)
  · every line is attributed as AI or human, and says which actor
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

GENESIS = "0" * 64

Kind = Literal["ai", "human", "system"]


def canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON. Sorted keys, no incidental whitespace.

    The hash is only meaningful if serialization is stable, so this must be the
    single place any audit payload is turned into bytes.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def row_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


def build_entry(
    prev_hash: str,
    *,
    actor: str,
    role: str,
    kind: Kind,
    action: str,
    case_id: str,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Construct one chained audit entry.

    `at` is injectable so tests are deterministic; production passes None and
    gets UTC now.
    """
    payload = {
        "at": (at or datetime.now(timezone.utc)).isoformat(),
        "actor": actor,
        "role": role,
        "kind": kind,
        "action": action,
        "case_id": case_id,
        "run_id": run_id,
        "detail": detail or {},
    }
    return {**payload, "prev_hash": prev_hash, "hash": row_hash(prev_hash, payload)}


def verify_chain(rows: Iterable[dict[str, Any]]) -> tuple[bool, str | None]:
    """Walk the chain. Returns (ok, first_broken_hash_or_None)."""
    prev = GENESIS
    for row in rows:
        payload = {
            k: row[k]
            for k in ("at", "actor", "role", "kind", "action", "case_id", "run_id", "detail")
        }
        if row["prev_hash"] != prev:
            return False, row["hash"]
        if row_hash(prev, payload) != row["hash"]:
            return False, row["hash"]
        prev = row["hash"]
    return True, None


# ---------------------------------------------------------------------------
# Redaction — the audit trail is read by people, and holds loan data
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = {"ssn", "account_number", "routing_number", "api_key", "token", "password"}


def redact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Strip values we must never persist into an audit payload.

    Tool arguments land in `detail`, and a tool argument can carry anything the
    model put in it. Redact by key name before hashing — once a value is in the
    chain it cannot be removed without breaking every subsequent row.
    """
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if k.lower() in _SENSITIVE_KEYS:
            out[k] = "<redacted>"
        elif isinstance(v, dict):
            out[k] = redact_detail(v)
        elif isinstance(v, str) and len(v) > 2000:
            out[k] = v[:2000] + f"… <truncated {len(v) - 2000} chars>"
        else:
            out[k] = v
    return out
