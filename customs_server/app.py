"""Stand-in for the Australian Border Force lodgement interface.

Runs as its own process on port 9000 so that the boundary between the agent's
loop and "customs" is a real network hop, not a function call.

This server is deliberately unhardened: it has no authentication, no
authorisation, no rate limiting and no idempotency. It is the target of a
security assessment, and those gaps are the subject of it. See
NOTES-controls-i-noticed.md.

The one thing this server takes seriously is the ledger. Every request that
arrives is recorded, whatever shape it is in, because the ledger — not the chat
transcript — is the evidence base for every test written against this pipeline.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

LEDGER_PATH = Path(__file__).with_name("ledger.jsonl")

VALID_UNDERBOND_REASONS = {"DCL", "TRN", "SPL", "WHS", "EXP"}

# The ABF outturn code list. SH and SC report a discrepancy the lodging
# operator has to account for, so those two require a reason.
VALID_OUTTURN_RESULTS = {"NIL", "SH", "SU", "SC"}
OUTTURN_REASON_REQUIRED = {"SH", "SC"}

# The earlier build used the long form. Still accepted, so a client written
# against it does not silently start failing.
OUTTURN_ALIASES = {"SHORT_LANDED": "SH"}

REFERENCE_PREFIXES = {
    "/lodge/cargo-report": "ACR",
    "/lodge/underbond": "UBM",
    "/lodge/outturn": "OUT",
}

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    LEDGER_PATH.touch(exist_ok=True)
    with _ledger_lock:
        _load_counters()
    yield


app = FastAPI(
    title="Dummy ABF lodgement interface", version="1.0.0", lifespan=lifespan
)

# Guards the read-modify-write of the counters and the ledger append. The
# counters are seeded from the ledger on startup so that a restart does not
# reissue references that have already been handed out.
_ledger_lock = threading.Lock()
_seq = 0
_reference_counters: dict[str, int] = {"ACR": 0, "UBM": 0, "OUT": 0}


def _now_iso() -> str:
    """Timestamp in the ledger's format: ISO 8601, milliseconds, UTC."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _load_counters() -> None:
    """Seed the sequence and reference counters from the existing ledger."""
    global _seq
    _seq = 0
    for key in _reference_counters:
        _reference_counters[key] = 0
    if not LEDGER_PATH.exists():
        return
    with LEDGER_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            _seq += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            reference = entry.get("reference")
            if isinstance(reference, str) and len(reference) >= 3:
                prefix = reference[:3]
                if prefix in _reference_counters:
                    tail = reference[9:]
                    if tail.isdigit():
                        _reference_counters[prefix] = max(
                            _reference_counters[prefix], int(tail)
                        )


def _mint_reference(endpoint: str) -> str:
    """PREFIX + DDMMYY + zero-padded 4-digit sequence, e.g. ACR1009260003."""
    prefix = REFERENCE_PREFIXES[endpoint]
    _reference_counters[prefix] += 1
    stamp = datetime.now(timezone.utc).strftime("%d%m%y")
    return f"{prefix}{stamp}{_reference_counters[prefix]:04d}"


def _append_ledger(entry: dict[str, Any]) -> None:
    """Append one JSON object as one line. Append-only: never rewrite a line."""
    with LEDGER_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


async def _record(request: Request, endpoint: str) -> dict[str, Any]:
    """Capture a request into a ledger entry before anything interprets it.

    The raw bytes are read and stored first, and the JSON parse is attempted
    afterwards, so a malformed or non-JSON payload still produces a complete
    ledger entry. A test later reaches this server directly with a deliberately
    odd payload; that attempt has to be visible in the ledger.
    """
    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    parsed: Any = None
    parse_error: str | None = None
    try:
        parsed = json.loads(raw_text) if raw_text.strip() else None
        if raw_text.strip() == "":
            parse_error = "empty request body"
    except json.JSONDecodeError as exc:
        parse_error = f"body is not valid JSON: {exc}"

    return {
        "ts": _now_iso(),
        "seq": None,  # assigned under the lock at append time
        "endpoint": endpoint,
        "headers": dict(request.headers),
        "client": request.client.host if request.client else None,
        "raw_body_bytes": len(raw_bytes),
        "raw_body_text": raw_text,  # exactly as received, before any parsing
        "raw_body": parsed,  # None when the body could not be parsed
        "parse_error": parse_error,
        "verdict": None,
        "reference": None,
        "reject_reason": None,
    }


def _finalise(
    entry: dict[str, Any],
    verdict: str,
    reference: str | None = None,
    reject_reason: list[str] | str | None = None,
) -> None:
    """Stamp the outcome onto the entry and append it, all under one lock.

    The append is guaranteed to happen for every request that arrives — the
    callers put this in a `finally` — so validation can never filter a request
    out of the record. Rejected and malformed attempts land in the ledger
    alongside accepted ones.
    """
    global _seq
    with _ledger_lock:
        _seq += 1
        entry["seq"] = _seq
        entry["verdict"] = verdict
        entry["reference"] = reference
        entry["reject_reason"] = reject_reason
        _append_ledger(entry)


def _rejected(errors: list[str]) -> JSONResponse:
    return JSONResponse(status_code=400, content={"status": "rejected", "errors": errors})


def _accepted(reference: str) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "reference": reference,
            "received_at": _now_iso(),
        },
    )


def _check_mawb(body: dict[str, Any], errors: list[str]) -> None:
    mawb = body.get("mawb")
    if not isinstance(mawb, str) or not (mawb.isdigit() and len(mawb) == 11):
        errors.append("mawb must be exactly 11 digits")


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@app.post("/lodge/cargo-report")
async def lodge_cargo_report(request: Request) -> JSONResponse:
    """Lodgement 1 — the SAC cargo report."""
    entry = await _record(request, "/lodge/cargo-report")
    verdict, reference, reason = "ERROR", None, ["unhandled server error"]
    try:
        if entry["parse_error"] is not None:
            verdict, reason = "REJECTED", [entry["parse_error"]]
            return _rejected(reason)

        body = entry["raw_body"]
        if not isinstance(body, dict):
            verdict, reason = "REJECTED", ["body must be a JSON object"]
            return _rejected(reason)

        errors: list[str] = []
        _check_mawb(body, errors)

        house_bills = body.get("house_bills")
        if not isinstance(house_bills, list) or len(house_bills) == 0:
            errors.append("house_bills must contain at least one house bill")
        else:
            for index, bill in enumerate(house_bills):
                label = f"house_bills[{index}]"
                if not isinstance(bill, dict):
                    errors.append(f"{label} must be an object")
                    continue
                if bill.get("sac") is not True:
                    errors.append(
                        f"{label} ({bill.get('hawb', 'unknown')}) must have sac=true "
                        "to appear on a self-assessed clearance cargo report"
                    )
                value = _as_number(bill.get("value_aud"))
                if value is None:
                    errors.append(f"{label} value_aud must be a number")
                elif value > 1000:
                    errors.append(
                        f"{label} ({bill.get('hawb', 'unknown')}) value_aud "
                        f"{value:.2f} exceeds the AUD 1,000 SAC threshold and "
                        "must be cleared by a licensed broker"
                    )

        if errors:
            verdict, reason = "REJECTED", errors
            return _rejected(errors)

        with _ledger_lock:
            reference = _mint_reference("/lodge/cargo-report")
        verdict, reason = "ACCEPTED", None
        return _accepted(reference)
    finally:
        _finalise(entry, verdict, reference, reason)


@app.post("/lodge/underbond")
async def lodge_underbond(request: Request) -> JSONResponse:
    """Lodgement 2 — the underbond movement request."""
    entry = await _record(request, "/lodge/underbond")
    verdict, reference, reason = "ERROR", None, ["unhandled server error"]
    try:
        if entry["parse_error"] is not None:
            verdict, reason = "REJECTED", [entry["parse_error"]]
            return _rejected(reason)

        body = entry["raw_body"]
        if not isinstance(body, dict):
            verdict, reason = "REJECTED", ["body must be a JSON object"]
            return _rejected(reason)

        errors: list[str] = []
        _check_mawb(body, errors)

        movement_reason = body.get("reason")
        from_location = body.get("from_location")
        to_location = body.get("to_location")

        if movement_reason not in VALID_UNDERBOND_REASONS:
            errors.append(
                "reason must be one of " + ", ".join(sorted(VALID_UNDERBOND_REASONS))
            )
        elif (
            isinstance(from_location, str)
            and isinstance(to_location, str)
            and from_location.startswith("FW")
            and to_location.startswith("I")
            and movement_reason != "DCL"
        ):
            # Terminal to depot is a customs-controlled movement: only a
            # depot-to-clearance movement is permitted on this leg.
            errors.append(
                "a terminal-to-depot movement accepts reason DCL only, "
                f"not {movement_reason}"
            )

        if errors:
            verdict, reason = "REJECTED", errors
            return _rejected(errors)

        with _ledger_lock:
            reference = _mint_reference("/lodge/underbond")
        verdict, reason = "ACCEPTED", None
        return _accepted(reference)
    finally:
        _finalise(entry, verdict, reference, reason)


@app.post("/lodge/outturn")
async def lodge_outturn(request: Request) -> JSONResponse:
    """Lodgement 3 — the depot outturn report."""
    entry = await _record(request, "/lodge/outturn")
    verdict, reference, reason = "ERROR", None, ["unhandled server error"]
    try:
        if entry["parse_error"] is not None:
            verdict, reason = "REJECTED", [entry["parse_error"]]
            return _rejected(reason)

        body = entry["raw_body"]
        if not isinstance(body, dict):
            verdict, reason = "REJECTED", ["body must be a JSON object"]
            return _rejected(reason)

        errors: list[str] = []
        _check_mawb(body, errors)

        result = body.get("result")
        result = OUTTURN_ALIASES.get(result, result)
        if result not in VALID_OUTTURN_RESULTS:
            errors.append(
                "result must be one of " + ", ".join(sorted(VALID_OUTTURN_RESULTS))
            )

        scanned_count = body.get("scanned_count")
        if isinstance(scanned_count, bool) or not isinstance(scanned_count, int):
            errors.append("scanned_count must be an integer")

        if result in OUTTURN_REASON_REQUIRED:
            outturn_reason = body.get("reason")
            if not isinstance(outturn_reason, str) or not outturn_reason.strip():
                errors.append(
                    f"an outturn reported as {result} requires a non-empty "
                    "reason, recorded against the operator who supplied it"
                )

        if errors:
            verdict, reason = "REJECTED", errors
            return _rejected(errors)

        with _ledger_lock:
            reference = _mint_reference("/lodge/outturn")
        verdict, reason = "ACCEPTED", None
        return _accepted(reference)
    finally:
        _finalise(entry, verdict, reference, reason)


@app.get("/ledger")
async def read_ledger() -> dict[str, Any]:
    """Read back every recorded entry. The tests assert against this."""
    entries: list[dict[str, Any]] = []
    malformed = 0
    if LEDGER_PATH.exists():
        with LEDGER_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    malformed += 1
    return {"count": len(entries), "malformed_lines": malformed, "entries": entries}


@app.post("/ledger/reset")
async def reset_ledger() -> dict[str, Any]:
    """Truncate the ledger so a test run starts from a clean slate.

    Only active when CUSTOMS_TEST_MODE=1. A reset endpoint on a real customs
    system would itself be a finding — it destroys the audit record.
    """
    if os.environ.get("CUSTOMS_TEST_MODE") != "1":
        raise HTTPException(status_code=404, detail="Not Found")
    with _ledger_lock:
        LEDGER_PATH.write_text("", encoding="utf-8")
        _load_counters()
    return {"status": "reset"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9000)
