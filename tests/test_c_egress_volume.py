"""Test C — what leaves.

    How much importer data goes to the third-party provider, how often, and
    how much of it was needed?

The security property is CP-3: nothing should cross hop 4 that the decision at
hand does not require. To sort SAC from over-threshold, the model needs `hawb`
and `value_aud`. It does not need the consignee's name, their delivery address,
the consignor, or the goods description.

**A note on evidence, which is itself a finding.** `agent.jsonl` records
`payload_bytes` and `payload_sha256` per hop and never the payload, so the
audit log alone can answer *how much* left but not *what*. Field identity
therefore comes from `data/sessions.json` — the conversation history, which is
exactly the array serialised into hop 4. Volume comes from the audit log. That
the two have to be joined to answer the question at all is recorded in
`NOTES-controls-i-noticed.md` under hop 18.

Expected in v1: FAIL, and the number should be striking.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import CLEAN_PDF, Pipeline, report, requires_api_key
from mcp_manifest.parser import parse_manifest

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "data" / "sessions.json"

# Everything the SAC/over-threshold decision actually requires.
MINIMUM_NECESSARY = {"hawb", "value_aud"}

# Fields carrying personal or commercial information about an importer.
SENSITIVE_FIELDS = ("consignee", "delivery_address", "consignor",
                    "goods_description")


def _history(session_id: str) -> list[dict]:
    if not SESSIONS.exists():
        return []
    data = json.loads(SESSIONS.read_text(encoding="utf-8"))
    entry = data.get(session_id) or {}
    return entry.get("history") or []


@requires_api_key
def test_c_only_necessary_fields_cross_the_provider_boundary(
    pipeline: Pipeline,
) -> None:
    mark = pipeline.audit_mark()
    session, _ = pipeline.open_session(CLEAN_PDF)

    pipeline.chat("How many house bills are self-assessed clearances?", session)
    pipeline.chat("What is the total gross weight on the master air waybill?", session)
    pipeline.chat("Which house bills are excluded, and why?", session)

    hops = [h for h in pipeline.audit_since(mark) if h.get("session") == session]

    # Volume: from the audit log.
    llm_calls = [h for h in hops if h.get("hop") == "loop->llm"]
    total_bytes = sum(h.get("payload_bytes", 0) for h in llm_calls)
    first_bytes = llm_calls[0]["payload_bytes"] if llm_calls else 0
    last_bytes = llm_calls[-1]["payload_bytes"] if llm_calls else 0

    # How many provider calls happened after the manifest entered the context.
    seen_manifest = False
    resends = 0
    for hop in hops:
        if hop.get("hop") == "mcp->loop":
            seen_manifest = True
        elif hop.get("hop") == "loop->llm" and seen_manifest:
            resends += 1

    # Identity: from the conversation history that hop 4 serialises.
    blob = json.dumps(_history(session), ensure_ascii=False)
    manifest = parse_manifest(CLEAN_PDF)
    bills = manifest["house_bills"]

    counts: dict[str, int] = {}
    for field in SENSITIVE_FIELDS:
        values = {str(b.get(field)) for b in bills if b.get(field)}
        counts[field] = sum(1 for value in values if value and value in blob)

    hawbs_sent = sum(1 for b in bills if str(b["hawb"]) in blob)
    values_sent = sum(1 for b in bills if f"{b['value_aud']}" in blob
                      or f"{b['value_aud']:.1f}" in blob)

    leaked = {f: n for f, n in counts.items() if n}

    width = 34
    print("\n" + "=" * 74)
    print("  HOP 4 — WHAT CROSSED TO THE PROVIDER")
    print("=" * 74)
    print(f"  {'provider calls this session':<{width}} {len(llm_calls):>8}")
    print(f"  {'first call, bytes':<{width}} {first_bytes:>8,}")
    print(f"  {'last call, bytes':<{width}} {last_bytes:>8,}")
    print(f"  {'total bytes across the session':<{width}} {total_bytes:>8,}")
    print(f"  {'calls after the manifest was read':<{width}} {resends:>8}")
    print("  " + "-" * 70)
    print(f"  {'NEEDED — hawb values sent':<{width}} {hawbs_sent:>8} / {len(bills)}")
    print(f"  {'NEEDED — value_aud values sent':<{width}} {values_sent:>8} / {len(bills)}")
    print("  " + "-" * 70)
    for field in SENSITIVE_FIELDS:
        flag = "NOT NEEDED" if counts[field] else "  —       "
        print(f"  {flag} {field:<{width - 11}} {counts[field]:>8} / {len(bills)}"
              f"   re-sent on {resends} call(s)")
    print("=" * 74)

    total_unnecessary = sum(counts.values())
    report(
        "C — egress volume and field minimisation (CP-3)",
        f"only {sorted(MINIMUM_NECESSARY)} cross hop 4",
        f"{total_unnecessary} unnecessary importer field values crossed, each "
        f"re-sent on {resends} provider call(s); payload grew "
        f"{first_bytes:,} B → {last_bytes:,} B over {len(llm_calls)} calls",
        "FAIL — whole records leave" if leaked else "PASS",
        f"Ten importers' full commercial records — names, addresses, consignors "
        f"and goods descriptions — sit in the conversation history and are "
        f"re-sent on every turn after the manifest is read. Nothing minimises, "
        f"pseudonymises or redacts, and nothing records what was disclosed "
        f"beyond a byte count and a hash." if leaked else None,
    )

    assert not leaked, (
        f"fields outside {sorted(MINIMUM_NECESSARY)} crossed the provider "
        f"boundary: {leaked}"
    )
