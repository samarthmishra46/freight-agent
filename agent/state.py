"""Session state, persisted to disk.

Three lodgements happen across four days in the walkthrough, so a session has
to survive between conversations rather than living in process memory.

v1 stores sessions in one world-readable JSON file with no encryption, no
access control and no expiry, and a session id is accepted from the client
without any check that the client is the operator who created it.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import confirmations, state_machine
from .identity import Operator, operator_from_dict

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "sessions.json"

LODGEMENT_KEYS = ("cargo_report", "underbond", "outturn")

_lock = threading.Lock()


@dataclass
class Session:
    """One operator's conversation, and what it has lodged so far."""

    session_id: str
    operator: Operator
    history: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    manifest_path: str | None = None
    # Where the consignment has got to. Advanced by agent/tools.py once customs
    # accepts a lodgement, because the status is a statement about what has
    # been lodged rather than about what was attempted.
    shipment_state: str = state_machine.INITIAL_STATE
    lodgements: dict[str, Any] = field(
        default_factory=lambda: dict.fromkeys(LODGEMENT_KEYS)
    )
    # v1: confirmation is an appended string and nothing more. Nothing binds it
    # to a payload, nothing checks it before a lodge tool runs, and nothing
    # stops the same one being reused. That is the whole of test 4a.
    confirmations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "operator": self.operator.as_dict(),
            "history": self.history,
            "manifest": self.manifest,
            "manifest_path": self.manifest_path,
            "shipment_state": self.shipment_state,
            "lodgements": self.lodgements,
            "confirmations": self.confirmations,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        lodgements = dict.fromkeys(LODGEMENT_KEYS)
        lodgements.update(data.get("lodgements") or {})
        return cls(
            session_id=data["session_id"],
            operator=operator_from_dict(data.get("operator")),
            history=data.get("history") or [],
            manifest=data.get("manifest"),
            manifest_path=data.get("manifest_path"),
            shipment_state=state_machine.normalise(data.get("shipment_state")),
            lodgements=lodgements,
            confirmations=data.get("confirmations") or [],
        )

    def public_state(self) -> dict[str, Any]:
        """What the front end is told about this session.

        The parsed manifest goes out in full, including all ten importers'
        names, addresses and commercial values, because the lodgement preview
        cards need to show exactly what will be sent.
        """
        return {
            "session_id": self.session_id,
            "operator": self.operator.as_dict(),
            "manifest": self.manifest,
            "manifest_path": self.manifest_path,
            "shipment_state": self.shipment_state,
            "shipment_status_label": state_machine.label(self.shipment_state),
            "next_action": state_machine.next_action(self.shipment_state),
            "lodgements": self.lodgements,
            "confirmations": self.confirmations,
            # CP-8. The staged lodgement, carrying its token. This reaches the
            # operator's browser and nothing else: it is never serialised into
            # session.history, so it never crosses hop 4 to the model.
            "pending_confirmation": (
                pending.for_operator()
                if (pending := confirmations.pending_for(self.session_id))
                else None
            ),
            # Operator messages only. Tool results are also role "user", so
            # they are excluded by requiring plain string content.
            "turns": sum(1 for m in self.history
                         if m.get("role") == "user" and isinstance(m.get("content"), str)),
        }


def _load_all() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_or_create(session_id: str | None, operator: Operator) -> Session:
    """Fetch a session by id, or start a new one.

    A client that supplies someone else's session id gets that session, with
    its manifest and its lodgement history. Nothing checks that the id belongs
    to the caller.
    """
    with _lock:
        store = _load_all()
        if session_id and session_id in store:
            session = Session.from_dict(store[session_id])
            # The identity on the session is replaced by whatever the current
            # request asserted, so the operator on a session can change turn
            # to turn without anything noticing.
            session.operator = operator
            return session
        new_id = session_id or f"s-{uuid.uuid4().hex[:12]}"
        return Session(session_id=new_id, operator=operator)


def get(session_id: str) -> Session | None:
    with _lock:
        store = _load_all()
        data = store.get(session_id)
        return Session.from_dict(data) if data else None


def save(session: Session) -> None:
    with _lock:
        store = _load_all()
        store[session.session_id] = session.as_dict()
        _save_all(store)


def record_lodgement(session: Session, key: str, outcome: dict[str, Any]) -> None:
    session.lodgements[key] = outcome
    save(session)


def all_session_ids() -> list[str]:
    with _lock:
        return sorted(_load_all())
