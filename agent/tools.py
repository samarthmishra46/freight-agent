"""Tool definitions and dispatch.

The LLM never talks to the customs server. It emits a `tool_use` block; this
module is what actually makes the call. That separation is the one piece of
structure v1 does have, and it is the reason a control at hop 14 is possible
at all.

`dispatch` runs the customs rules engine and the shipment state machine before
a lodgement goes anywhere, and refuses on failure with the rule errors handed
back to the model. That is a product feature: customs rules and shipment state
belong on our side, not in the government system we are posting to.

It is emphatically not a structural control, and is not built as one:

- the customs server has no idea whether validation ran
- the shipment state lives on the session and can be written directly

So the layer is honest about what it is: it catches the ordinary wrong
lodgement, and a caller that reaches the customs server directly goes round it.

**Phase 9 — CP-8.** `dispatch` no longer sends a lodgement. It validates, then
stages the payload in `agent/confirmations.py` and returns "staged" to the
model *without the token*. Only `POST /confirm`, driven by the operator's own
client, can redeem that token and reach `_post_to_customs` — whose confirmation
parameter is required, so no call path can omit it. The model has been removed
from the trust path: it can propose, and it can no longer send.

What v2 still deliberately does NOT do — these remain open findings:

- verify the operator identity it stamps on the request (CP-1)
- authenticate the caller at the customs server, so a direct POST to port 9000
  still bypasses everything above (CP-10)
- classify or mark manifest text before it enters the model context (CP-7)
- minimise what crosses to the provider (CP-3)
- restrict the file path the model chooses for read_manifest (CP-5)
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from . import audit, confirmations, rules, state, state_machine
from .mcp_client import ManifestMCPClient
from .state import Session

CUSTOMS_URL = os.environ.get("CUSTOMS_URL", "http://localhost:9000")
CUSTOMS_TIMEOUT = 20.0

# Tool definitions for the three lodge tools, in OpenAI function-calling shape.
# `read_manifest` is not here — it is advertised by the MCP server and merged in
# by the loop, so from the model's point of view all four tools look the same.
_LOCAL_FUNCTIONS: list[dict[str, Any]] = [
    {
        "name": "lodge_cargo_report",
        "description": (
            "Lodge the self-assessed clearance cargo report with the Australian "
            "Border Force. Include every house bill whose value_aud is AUD 1,000 "
            "or less, and no others. This is irreversible once accepted. Only "
            "call this after the operator has confirmed the exact contents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mawb": {
                    "type": "string",
                    "description": "Master air waybill number, 11 digits.",
                },
                "depot": {
                    "type": "string",
                    "description": "Destination depot code, e.g. I028N.",
                },
                "house_bills": {
                    "type": "array",
                    "description": "The SAC house bills to report.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "hawb": {"type": "string"},
                            "consignee": {"type": "string"},
                            "pieces": {"type": "integer"},
                            "weight_kg": {"type": "number"},
                            "value_aud": {"type": "number"},
                            "sac": {
                                "type": "boolean",
                                "description": "True when value_aud <= 1000.",
                            },
                        },
                        "required": ["hawb", "consignee", "pieces", "weight_kg",
                                     "value_aud", "sac"],
                    },
                },
            },
            "required": ["mawb", "depot", "house_bills"],
        },
    },
    {
        "name": "lodge_underbond_request",
        "description": (
            "Request an underbond movement of the consignment from the terminal "
            "to the destination depot. On a terminal-to-depot leg the reason "
            "code is DCL. Irreversible once accepted; confirm with the operator "
            "first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mawb": {"type": "string", "description": "Master air waybill, 11 digits."},
                "from_location": {"type": "string", "description": "Terminal code, e.g. FW53H."},
                "to_location": {"type": "string", "description": "Depot code, e.g. I028N."},
                "mode": {"type": "string", "description": "Transport mode, e.g. ROAD."},
                "reason": {
                    "type": "string",
                    "enum": ["DCL", "TRN", "SPL", "WHS", "EXP"],
                    "description": "Movement reason code. DCL for terminal to depot.",
                },
            },
            "required": ["mawb", "from_location", "to_location", "reason"],
        },
    },
    {
        "name": "lodge_outturn",
        "description": (
            "Report the depot outturn: what was actually received against what "
            "was expected. result is NIL when the scanned count matches the "
            "manifested pieces, SH when fewer arrived, SU when more arrived, "
            "and SC for a combined discrepancy. SH and SC require a reason "
            "supplied by the operator in their own words, because it is "
            "recorded against their name. Irreversible once accepted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mawb": {"type": "string"},
                "location": {"type": "string", "description": "Depot code, e.g. I028N."},
                "expected_count": {"type": "integer"},
                "scanned_count": {"type": "integer"},
                "result": {
                    "type": "string",
                    "enum": ["NIL", "SH", "SU", "SC"],
                    "description": "NIL matches, SH short, SU surplus, SC combined.",
                },
                "reason": {
                    "type": "string",
                    "description": "Required when result is SH or SC. The operator's words.",
                },
            },
            "required": ["mawb", "location", "expected_count", "scanned_count", "result"],
        },
    },
]

LOCAL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": function} for function in _LOCAL_FUNCTIONS
]

LOCAL_TOOL_NAMES = tuple(function["name"] for function in _LOCAL_FUNCTIONS)

# tool name -> (customs endpoint, key on session.lodgements)
_LODGEMENT_ROUTES = {
    "lodge_cargo_report": ("/lodge/cargo-report", "cargo_report"),
    "lodge_underbond_request": ("/lodge/underbond", "underbond"),
    "lodge_outturn": ("/lodge/outturn", "outturn"),
}


async def _post_to_customs(
    session: Session, tool_name: str, arguments: dict[str, Any], turn: int,
    confirmation: confirmations.Pending,
) -> dict[str, Any]:
    """POST a lodgement to the customs server and return its response verbatim.

    Hop 14 on the diagram, and the irreversible one.

    Phase 9: this is now unreachable without a redeemed confirmation. The
    parameter is required rather than optional on purpose — there is no call
    path that reaches customs without one, and a future caller cannot omit it
    by accident. The token id and the payload hash travel in the body so the
    ledger records which human confirmation authorised the lodgement.
    """
    endpoint, lodgement_key = _LODGEMENT_ROUTES[tool_name]
    url = f"{CUSTOMS_URL}{endpoint}"
    headers = session.operator.customs_headers()

    body = dict(arguments)
    body["confirmation_token"] = confirmation.token
    body["confirmation_sha256"] = confirmation.payload_sha256

    audit.log_hop(
        session.session_id, "loop->customs", body, turn=turn,
        action=tool_name, tool=tool_name,
        operator=session.operator.name, verdict="sent",
        confirmation_sha256=confirmation.payload_sha256,
        notes=f"POST {url} as {session.operator.name} "
              f"({session.operator.certificate_id}), authorised by "
              f"confirmation {confirmation.payload_sha256[:12]}",
    )

    async with httpx.AsyncClient(timeout=CUSTOMS_TIMEOUT) as client:
        response = await client.post(url, json=body, headers=headers)

    try:
        body = response.json()
    except ValueError:
        body = {"status": "unparseable", "raw": response.text}

    audit.log_hop(
        session.session_id, "customs->loop", body, turn=turn,
        action=tool_name, tool=tool_name,
        operator=session.operator.name, verdict=body.get("status"),
        notes=f"HTTP {response.status_code}",
    )

    outcome = {
        "status": body.get("status"),
        "http_status": response.status_code,
        "reference": body.get("reference"),
        "errors": body.get("errors"),
        "at": body.get("received_at"),
        "lodged_by": session.operator.as_dict(),
        "sent": arguments,
        "confirmation_sha256": confirmation.payload_sha256,
    }
    state.record_lodgement(session, lodgement_key, outcome)
    return body


def _check_before_lodging(
    action: str, arguments: dict[str, Any], session: Session, turn: int
) -> dict[str, Any] | None:
    """Run the state machine and the rules engine. Returns a refusal, or None.

    Both checks run even when the first fails, so the model is told everything
    that is wrong in one go rather than discovering it a turn at a time.
    """
    transition = state_machine.can_transition(session.shipment_state, action)
    rule_result = rules.validate(action, session.manifest, arguments)
    passed = transition.ok and rule_result.ok

    audit.log_hop(
        session.session_id, "loop->rules", arguments, turn=turn,
        action=action, tool=action,
        operator=session.operator.name,
        verdict="passed" if passed else "refused",
        notes=f"state {transition.current}: {transition.reason} | "
              f"{rule_result.summary()}",
    )

    if passed:
        return None

    reasons: list[str] = []
    if not transition.ok:
        reasons.append(transition.reason)
    if not rule_result.ok:
        reasons.append(rule_result.summary())

    return {
        "status": "refused",
        "refused_by": "agent rules engine and shipment state machine",
        "action": action,
        "shipment_state": session.shipment_state,
        "state_check": transition.as_dict(),
        "rules": rule_result.as_dict(),
        "reference": None,
        "message": (
            "This lodgement was not sent to customs. " + " ".join(reasons)
            + " Nothing has been lodged and nothing is irreversible — correct "
              "the payload and try again, or tell the operator what is wrong."
        ),
    }


async def dispatch(
    name: str,
    arguments: dict[str, Any],
    session: Session,
    mcp: ManifestMCPClient,
    turn: int,
) -> dict[str, Any]:
    """Carry out one tool call the model asked for.

    `arguments` is whatever the model produced. The rules engine checks it
    against the manifest before a lodgement leaves; nothing checks it before
    `read_manifest` runs.
    """
    if name == "read_manifest":
        audit.log_hop(
            session.session_id, "loop->mcp", arguments, turn=turn,
            action=name, tool=name, operator=session.operator.name, verdict="sent",
            notes=f"stdio subprocess, file={arguments.get('file')!r} as chosen by the model",
        )
        result = await mcp.call(name, arguments)
        audit.log_hop(
            session.session_id, "mcp->loop", result, turn=turn,
            action=name, tool=name, operator=session.operator.name,
            verdict="returned",
            notes="third-party document content, unvalidated and unsanitised",
        )
        session.manifest = result
        if isinstance(arguments.get("file"), str):
            session.manifest_path = arguments["file"]
        # A parsed manifest is what puts the consignment on hold. Re-reading it
        # later is harmless and leaves the state where it is.
        session.shipment_state = state_machine.advance(
            session.shipment_state, "read_manifest"
        )
        state.save(session)
        return result

    if name in _LODGEMENT_ROUTES:
        refusal = _check_before_lodging(name, arguments, session, turn)
        if refusal is not None:
            state.save(session)
            return refusal

        # CP-8. The model asked to lodge; our code stages it and stops. Only
        # POST /confirm, driven by the operator's own client, can send it.
        pending = confirmations.stage(
            session.session_id, session.operator, name, arguments
        )
        audit.log_hop(
            session.session_id, "loop->confirm", arguments, turn=turn,
            action=name, tool=name, operator=session.operator.name,
            verdict="staged",
            confirmation_sha256=pending.payload_sha256,
            notes=f"staged awaiting operator confirmation, expires in "
                  f"{confirmations.TTL_SECONDS}s; the token is not returned to "
                  f"the model",
        )
        state.save(session)
        return pending.for_model()

    raise ValueError(f"unknown tool: {name}")


async def lodge_confirmed(
    session: Session, pending: confirmations.Pending, turn: int = 0
) -> dict[str, Any]:
    """Send a lodgement whose confirmation token has just been redeemed.

    Called only by `POST /confirm`, and only after `confirmations.redeem`
    returned ok. The rules engine and state machine run again here rather than
    trusting the verdict recorded at staging time: the manifest or the shipment
    state may have moved between staging and confirmation, and re-checking
    costs nothing.
    """
    refusal = _check_before_lodging(pending.action, pending.payload, session, turn)
    if refusal is not None:
        state.save(session)
        return refusal

    body = await _post_to_customs(
        session, pending.action, pending.payload, turn, pending
    )

    if body.get("status") == "accepted":
        session.shipment_state = state_machine.advance(
            session.shipment_state, pending.action
        )
        state.save(session)
    return body
