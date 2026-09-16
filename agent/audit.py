"""Per-hop audit log.

One line per hop, every hop, to agent.jsonl. This exists so that test 4c can
measure how much importer data crosses `loop->llm` per turn, and so that the
question "if this went wrong overnight, could I reconstruct it from the logs?"
has an answer. Instrumentation added after the fact is instrumentation with
gaps, so every hop is logged from the start.

This log is *evidence about* the pipeline. It is not a control: nothing reads it
back to make a decision, and nothing stops a hop that fails to log.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_PATH = Path(__file__).resolve().parent.parent / "agent.jsonl"

# The eight hops the pipeline can make. Named exactly as the diagram numbers
# them so the log can be read against the drawing hop by hop.
HOPS = (
    "frontend->loop",
    "loop->llm",
    "llm->loop",
    "loop->mcp",
    "mcp->loop",
    "loop->customs",
    "customs->loop",
    "loop->frontend",
)

# Added by the phase 3.5 retrofit. The rules engine and the state machine are
# pure functions rather than a network boundary, so this is a decision point
# recorded as a hop — without it, a lodgement stopped before it reached customs
# leaves no trace anywhere, since the customs ledger never sees it.
INTERNAL_HOPS = ("loop->rules", "loop->confirm")

ALL_HOPS = HOPS + INTERNAL_HOPS

_lock = threading.Lock()


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _canonical(payload: Any) -> bytes:
    """Serialise a payload for measuring and hashing.

    Byte counts have to mean something for test 4c, so this is the payload as
    it would go on the wire: compact JSON, keys in the order given.
    """
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    try:
        return json.dumps(payload, ensure_ascii=False, default=str,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return repr(payload).encode("utf-8")


def log_hop(session_id: str, hop: str, payload: Any, **extra: Any) -> dict[str, Any]:
    """Append one hop to agent.jsonl and return the entry.

    `payload` is whatever crossed the hop. Its size and hash are recorded, not
    its content — the content is already in the conversation, the manifest or
    the customs ledger, and duplicating importer data into a third file would
    widen the exposure this log exists to measure.

    Every entry carries who (`session`, `operator`), what (`hop`, `action`),
    how it went (`verdict`) and when (`ts`), so that a later pass over this file
    can group behaviour by operator and action without needing anything else.
    """
    raw = _canonical(payload)
    action = extra.pop("action", None)
    tool = extra.pop("tool", None)
    entry: dict[str, Any] = {
        "ts": _now_iso(),
        "session": session_id,
        "operator": extra.pop("operator", None),
        "hop": hop,
        "turn": extra.pop("turn", None),
        # The named thing that happened. A tool call carries the tool name; a
        # scope check or a state transition carries its own.
        "action": action or tool,
        "verdict": extra.pop("verdict", None),
        "payload_bytes": len(raw),
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
        "tool": tool,
        "model": extra.pop("model", os.environ.get("MODEL", "gpt-4o")),
        "notes": extra.pop("notes", None),
    }
    entry.update(extra)

    with _lock:
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
    return entry


def read_hops(session_id: str | None = None) -> list[dict[str, Any]]:
    """Read the audit log back. Used by the tests and the plumbing view."""
    if not AUDIT_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    with AUDIT_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is None or entry.get("session") == session_id:
                entries.append(entry)
    return entries
