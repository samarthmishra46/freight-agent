"""CP-8 — the confirmation token. Phase 9, v2.

The v1 gate was L3: a paragraph in the system prompt asking the model to check
with the operator first. Anything the model can read, the model can be argued
out of — and manifest text sits in the same context window as that paragraph.

This is the L1 replacement. The shape is:

  1. The model asks to lodge. Our code does **not** send it. It stages the
     payload here: a random token, the exact payload hash, the session, the
     operator, a 5-minute expiry, `used=False`.
  2. The front end renders its preview from the staged record, not from the
     model's text.
  3. The operator confirms. The front end POSTs the token to `/confirm`.
  4. We check: exists, unexpired, unused, session matches, operator matches,
     and the payload hash still matches. Then we mark it used and POST.
  5. The token id and the payload hash go into the ledger with the lodgement.

**The model never sees the token.** It is not in any tool result, any
`tool_result` message, or any reply. It cannot mint one, guess one or replay
one, so nothing in the conversation — including injected PDF text — can produce
a valid confirmation. That is the whole difference between L1 and L3.

The store is process memory on purpose. `data/sessions.json` is world-writable
and reachable by anything that can write the file; a token living there could
be forged offline. Losing pending confirmations on restart is the correct
trade: an unconfirmed lodgement should not survive a restart.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .identity import Operator

# Short enough that a walked-away-from screen cannot be confirmed later, long
# enough for an operator to actually read the preview.
TTL_SECONDS = 300

_lock = threading.Lock()
_pending: dict[str, "Pending"] = {}


def payload_hash(payload: dict[str, Any]) -> str:
    """Hash the payload exactly as it will be sent.

    Sorted keys, compact separators: the same dict always hashes the same way,
    so the hash checked at redemption is the hash of the bytes that go out.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Pending:
    """One lodgement staged for a human to confirm."""

    token: str
    action: str
    payload: dict[str, Any]
    payload_sha256: str
    session_id: str
    operator_name: str
    operator_certificate: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None

    @property
    def used(self) -> bool:
        return self.used_at is not None

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at

    def for_operator(self) -> dict[str, Any]:
        """What the front end is told. Carries the token; the model never is."""
        return {
            "token": self.token,
            "action": self.action,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "operator": self.operator_name,
            "certificate": self.operator_certificate,
        }

    def for_model(self) -> dict[str, Any]:
        """What the model is told. Deliberately excludes the token."""
        return {
            "status": "staged",
            "action": self.action,
            "payload_sha256": self.payload_sha256,
            "expires_in_seconds": TTL_SECONDS,
            "message": (
                "This lodgement is prepared but NOT sent. It is waiting for the "
                "operator to confirm it in their own interface. You cannot "
                "confirm it and you cannot send it. Show the operator exactly "
                "what is staged, field by field, and wait. Nothing has been "
                "lodged and nothing is irreversible yet."
            ),
        }


@dataclass
class Redemption:
    """The result of trying to spend a token."""

    ok: bool
    reason: str
    pending: Pending | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "action": self.pending.action if self.pending else None,
            "payload_sha256": self.pending.payload_sha256 if self.pending else None,
        }


def stage(session_id: str, operator: Operator, action: str,
          payload: dict[str, Any]) -> Pending:
    """Hold a lodgement pending a human confirmation. Returns the record."""
    now = _now()
    pending = Pending(
        token=secrets.token_urlsafe(32),
        action=action,
        payload=payload,
        payload_sha256=payload_hash(payload),
        session_id=session_id,
        operator_name=operator.name,
        operator_certificate=operator.certificate_id,
        created_at=now,
        expires_at=now + timedelta(seconds=TTL_SECONDS),
    )
    with _lock:
        # Only one lodgement may be pending per session and action: staging a
        # replacement must not leave the earlier payload redeemable.
        for token, existing in list(_pending.items()):
            if (existing.session_id == session_id
                    and existing.action == action
                    and not existing.used):
                del _pending[token]
        _pending[pending.token] = pending
    return pending


def pending_for(session_id: str) -> Pending | None:
    """The live staged lodgement for a session, if there is one."""
    with _lock:
        for entry in _pending.values():
            if entry.session_id == session_id and not entry.used and not entry.expired:
                return entry
    return None


def redeem(token: str, session_id: str, operator: Operator) -> Redemption:
    """Spend a token, or explain why it cannot be spent.

    Every check is a reason to refuse. The payload hash is re-derived from the
    stored payload rather than trusted, so a payload mutated in the store after
    staging fails here rather than being lodged.
    """
    with _lock:
        pending = _pending.get(token or "")

        if pending is None:
            return Redemption(False, "no such confirmation token")
        if pending.used:
            return Redemption(
                False,
                f"this confirmation was already spent at "
                f"{pending.used_at.isoformat().replace('+00:00', 'Z')}; a "
                f"confirmation is single-use",
                pending,
            )
        if pending.expired:
            return Redemption(
                False,
                f"this confirmation expired at "
                f"{pending.expires_at.isoformat().replace('+00:00', 'Z')}; "
                f"prepare the lodgement again",
                pending,
            )
        if pending.session_id != session_id:
            return Redemption(
                False, "this confirmation belongs to a different session", pending)
        if pending.operator_certificate != operator.certificate_id:
            return Redemption(
                False,
                f"this confirmation was issued to certificate "
                f"{pending.operator_certificate}, not "
                f"{operator.certificate_id}",
                pending,
            )
        if payload_hash(pending.payload) != pending.payload_sha256:
            return Redemption(
                False,
                "the staged payload no longer matches the hash it was "
                "confirmed against",
                pending,
            )

        pending.used_at = _now()
        return Redemption(True, "confirmed", pending)


def forget_session(session_id: str) -> None:
    """Drop a session's pending records. Used by the tests."""
    with _lock:
        for token, entry in list(_pending.items()):
            if entry.session_id == session_id:
                del _pending[token]
