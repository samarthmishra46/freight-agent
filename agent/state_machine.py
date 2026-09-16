"""Per-shipment state, and which action may happen next.

The statuses match the strip in docs/1-FreightAgent-Walkthrough.html, which is
what an operator reads to know where a consignment has got to:

    NO_STATUS -> HELD -> CLEAR -> SUBUBMOV -> RELEASED

Each lodgement moves the shipment on, and only from the state it is meant to
follow. Reporting an outturn on a consignment that never got an underbond
movement is not a security problem — it is a wrong report, and the operator
whose certificate signs it is the one who carries that.

A product feature, not a security control. The state lives on the session and
is advanced by agent/tools.py after customs accepts a lodgement; nothing stops
a caller reaching the customs client without coming through here. See
NOTES-controls-i-noticed.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NO_STATUS = "NO_STATUS"
HELD = "HELD"
CLEAR = "CLEAR"
SUBUBMOV = "SUBUBMOV"
RELEASED = "RELEASED"

STATES = (NO_STATUS, HELD, CLEAR, SUBUBMOV, RELEASED)

INITIAL_STATE = NO_STATUS

# action -> (state it must be in, state it moves to)
TRANSITIONS: dict[str, tuple[str, str]] = {
    "read_manifest": (NO_STATUS, HELD),
    "lodge_cargo_report": (HELD, CLEAR),
    "lodge_underbond_request": (CLEAR, SUBUBMOV),
    "lodge_outturn": (SUBUBMOV, RELEASED),
}

# What the operator would have had to do to be in the required state, phrased
# for someone reading a refusal rather than reading the code.
_PREREQUISITE = {
    NO_STATUS: "the shipment has not been started",
    HELD: "the manifest has to be read first",
    CLEAR: "the cargo report has to be lodged and accepted first",
    SUBUBMOV: "the underbond movement has to be lodged and accepted first",
    RELEASED: "the outturn has to be lodged and accepted first",
}

_STATE_LABEL = {
    NO_STATUS: "NO STATUS",
    HELD: "HELD",
    CLEAR: "CLEAR",
    SUBUBMOV: "SUBUBMOV",
    RELEASED: "RELEASED",
}


@dataclass
class TransitionCheck:
    """Whether an action may run now, and why not if it may not."""

    ok: bool
    reason: str
    current: str
    required: str | None = None
    next_state: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "current": self.current,
            "required": self.required,
            "next_state": self.next_state,
        }


def label(state: str) -> str:
    """The status as the strip displays it."""
    return _STATE_LABEL.get(state, state)


def normalise(state: Any) -> str:
    return state if state in STATES else INITIAL_STATE


def can_transition(current: Any, action: str) -> TransitionCheck:
    """Can `action` run while the shipment is in `current`?"""
    state = normalise(current)

    transition = TRANSITIONS.get(action)
    if transition is None:
        return TransitionCheck(
            ok=True,
            reason=f"{action} does not move the shipment on",
            current=state,
        )

    required, next_state = transition

    if state == required:
        return TransitionCheck(
            ok=True,
            reason=f"{label(state)} -> {label(next_state)}",
            current=state, required=required, next_state=next_state,
        )

    if state == next_state:
        return TransitionCheck(
            ok=False,
            reason=(
                f"{action} has already been done on this shipment — it is "
                f"already {label(state)}, and a lodgement cannot be withdrawn "
                "or repeated"
            ),
            current=state, required=required, next_state=next_state,
        )

    done = STATES.index(state) > STATES.index(next_state)
    if done:
        return TransitionCheck(
            ok=False,
            reason=(
                f"{action} belongs earlier in the sequence — this shipment has "
                f"already reached {label(state)}"
            ),
            current=state, required=required, next_state=next_state,
        )

    return TransitionCheck(
        ok=False,
        reason=(
            f"{action} needs the shipment to be {label(required)} but it is "
            f"{label(state)}: {_PREREQUISITE[required]}"
        ),
        current=state, required=required, next_state=next_state,
    )


def advance(current: Any, action: str) -> str:
    """The state after `action` succeeded. Unchanged if it moves nothing.

    Called only once customs has accepted a lodgement, because the shipment's
    status is a statement about what has been lodged, not about what we tried.
    """
    state = normalise(current)
    transition = TRANSITIONS.get(action)
    if transition is None:
        return state
    required, next_state = transition
    return next_state if state == required else state


def next_action(current: Any) -> str | None:
    """The action this shipment is waiting on, if any."""
    state = normalise(current)
    for action, (required, _) in TRANSITIONS.items():
        if required == state:
            return action
    return None
