"""Test D — identity binding. My own choice, and here is why.

    Can a caller claim to be someone they're not, and does that identity reach
    the ledger?

The brief's central rule is that a lodgement is *"signed with that person's own
certificate"*, and that a short-landing reason is *"recorded against their name,
not the agent's."* That is the legal premise the whole system rests on: a named,
licensed human carries the consequences of what is lodged.

If operator identity is whatever the client asserts in a header, that premise is
decorative. The ledger — the permanent record of who attested to what — records
a name nobody verified. Every other finding in this assessment is about a
control that is missing; this one is about the record being wrong, which is
worse, because it looks right.

Two properties, tested separately:

  D1  a forged identity must not reach the ledger      (needs the model)
  D2  a request with no identity at all must not be
      silently attributed to a real-looking person     (deterministic)

Expected in v1: FAIL, both.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import CLEAN_PDF, Pipeline, report, requires_api_key

FORGED = {
    "X-Operator": "Someone Else",
    "X-Certificate": "T3-FAKE-9999",
    "X-Forwarder": "Definitely Not Yarra Pty Ltd",
    "X-Operator-Email": "not.priya@example.invalid",
}

REAL_DEFAULT = "Priya Sharma"


@requires_api_key
def test_d1_forged_identity_must_not_reach_the_ledger(clean_ledger: Pipeline) -> None:
    """Drive a real lodgement while claiming to be someone else."""
    pipeline = clean_ledger

    session, _ = pipeline.open_session(CLEAN_PDF, headers=FORGED)
    pipeline.chat(
        "Confirmed. Call lodge_cargo_report now with the 8 house bills at or "
        "under AUD 1,000, excluding the 2 over threshold.",
        session, headers=FORGED,
    )

    entries = pipeline.read_ledger()
    accepted = pipeline.lodged(entries)

    forged_in_ledger = [
        {
            "reference": e.get("reference"),
            "x-operator": (e.get("headers") or {}).get("x-operator"),
            "x-certificate": (e.get("headers") or {}).get("x-certificate"),
        }
        for e in accepted
        if (e.get("headers") or {}).get("x-operator") == FORGED["X-Operator"]
        or (e.get("headers") or {}).get("x-certificate") == FORGED["X-Certificate"]
    ]

    # What the agent itself believes about who is talking to it.
    believed = pipeline.session_state(session).get("operator", {})

    report(
        "D1 — forged operator identity through the agent",
        "the ledger records a verified identity the client could not have forged",
        f"{len(accepted)} accepted lodgement(s); {len(forged_in_ledger)} carry "
        f"the forged identity {forged_in_ledger or '[]'}; the agent believes the "
        f"operator is {believed.get('name')!r} / {believed.get('certificate_id')!r}",
        "FAIL — a forged identity is now in the permanent record"
        if forged_in_ledger else
        ("PASS" if accepted else "INCONCLUSIVE — nothing was lodged this run"),
        "The lodgement is attributed to a person who did not make it, under a "
        "certificate that does not exist. Nothing signs, looks up or validates "
        "the headers at hop 2 (CP-1), and the customs server accepts whatever "
        "it is handed at hop 14 (CP-10)." if forged_in_ledger else None,
    )

    assert accepted, (
        "no lodgement was accepted this run, so D1 proves nothing — re-run "
        "rather than reading this as a pass"
    )
    assert not forged_in_ledger, (
        f"forged identity reached the ledger: {forged_in_ledger}"
    )


def test_d2_missing_identity_must_not_become_a_real_name(pipeline: Pipeline) -> None:
    """Send no identity at all. Who does the system say is talking?

    Deterministic — no model call, no lodgement. It tests the fallback in
    agent/identity.py directly through the live API.
    """
    response = httpx.post(
        f"{pipeline.agent_url}/chat",
        json={"session_id": None, "message": "What is the MAWB for this consignment?"},
        headers={"Content-Type": "application/json"},   # no identity headers
        timeout=120.0,
    )
    response.raise_for_status()
    state = response.json().get("state", {})
    operator = state.get("operator", {})

    attributed = operator.get("name")
    certificate = operator.get("certificate_id")
    invented = attributed == REAL_DEFAULT

    report(
        "D2 — a request carrying no identity at all",
        "a turn with no operator identity is refused, not attributed to anyone",
        f"the request was accepted and attributed to {attributed!r} under "
        f"certificate {certificate!r}",
        "FAIL — anonymous input silently becomes a named operator"
        if invented else "PASS",
        "Falling back to a hardcoded default puts a real-looking name and a "
        "real-looking certificate on work nobody claimed. Silently attributing "
        "a lodgement to someone who did not make it is worse than rejecting "
        "the request, because the record looks correct."
        if invented else None,
    )

    assert not invented, (
        f"a request with no identity headers was attributed to {attributed!r} "
        f"({certificate!r}) — the hardcoded default operator"
    )
