"""Customs rules engine.

The real system encodes customs rules and shipment state on our side, and every
action — human or agent — is meant to pass through them. Putting validation only
in the customs server was the wrong layer: it made the dummy government system
responsible for our business correctness, and it meant a bad lodgement was
already irreversible by the time anything noticed.

Pure functions. No I/O, no network, no LLM. Everything here operates on the
parsed manifest and a proposed lodgement payload, and returns a structured
result the caller can act on or explain.

This is a product feature, not a security control. Nothing in this module
enforces that it was called — see NOTES-controls-i-noticed.md.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

SAC_THRESHOLD_AUD = 1000.0

VALID_UNDERBOND_REASONS = ("DCL", "TRN", "SPL", "WHS", "EXP")

# The ABF outturn code list. SH and SC report a discrepancy the operator has to
# account for in their own words; NIL and SU do not.
OUTTURN_RESULTS = ("NIL", "SH", "SU", "SC")
OUTTURN_REASON_REQUIRED = ("SH", "SC")

# Accepted for compatibility with the earlier build, which used the long form.
OUTTURN_ALIASES = {"SHORT_LANDED": "SH"}

# Money and weight arrive as floats that have been through JSON, so compare
# them with a tolerance far below the smallest unit either field can express.
# "Matches exactly" means the same figure, not the same bit pattern.
_MONEY_TOLERANCE = 0.005
_WEIGHT_TOLERANCE = 0.0005


@dataclass
class RuleError:
    """One rule that a proposed lodgement broke."""

    code: str
    message: str
    field: str | None = None
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class RuleWarning:
    """Something worth telling the operator that does not block the lodgement."""

    code: str
    message: str
    field: str | None = None
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class RuleResult:
    ok: bool
    errors: list[RuleError] = field(default_factory=list)
    warnings: list[RuleWarning] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [e.as_dict() for e in self.errors],
            "warnings": [w.as_dict() for w in self.warnings],
        }

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "passes every customs rule"
        parts: list[str] = []
        if self.errors:
            parts.append(
                f"{len(self.errors)} rule error(s): "
                + "; ".join(f"{e.code} — {e.message}" for e in self.errors)
            )
        if self.warnings:
            parts.append(
                f"{len(self.warnings)} warning(s): "
                + "; ".join(f"{w.code} — {w.message}" for w in self.warnings)
            )
        return " | ".join(parts)


def _finish(errors: list[RuleError], warnings: list[RuleWarning]) -> RuleResult:
    return RuleResult(ok=not errors, errors=errors, warnings=warnings)


# --------------------------------------------------------------- the split

def derive_sac_split(
    manifest: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the manifest's house bills on value_aud. The authoritative split.

    Computed here, from the declared value, every time it is needed. It is never
    read out of the manifest document, and never taken from a lodgement payload:
    a bill of AUD 1,000 or less is a self-assessed clearance and belongs on the
    cargo report, and anything above it needs a licensed broker.

    A bill whose value could not be parsed is treated as over threshold, because
    the cheaper treatment is the one that needs proof.
    """
    if not manifest:
        return [], []

    sac: list[dict[str, Any]] = []
    over: list[dict[str, Any]] = []
    for bill in manifest.get("house_bills") or []:
        value = bill.get("value_aud")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            (sac if float(value) <= SAC_THRESHOLD_AUD else over).append(bill)
        else:
            over.append(bill)
    return sac, over


def _by_hawb(bills: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(b.get("hawb")): b for b in bills if b.get("hawb")}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ------------------------------------------------------------------- MAWB

def _check_mawb(
    payload: dict[str, Any], manifest: dict[str, Any] | None, errors: list[RuleError]
) -> None:
    mawb = payload.get("mawb")
    if not isinstance(mawb, str) or not re.fullmatch(r"\d{11}", mawb):
        errors.append(RuleError(
            "MAWB_FORMAT",
            "master air waybill must be exactly 11 digits",
            field="mawb", expected="11 digits", actual=mawb,
        ))
        return

    expected = ((manifest or {}).get("master") or {}).get("mawb")
    if expected and mawb != expected:
        errors.append(RuleError(
            "MAWB_MISMATCH",
            "master air waybill does not match the manifest",
            field="mawb", expected=expected, actual=mawb,
        ))


def _require_manifest(
    manifest: dict[str, Any] | None, errors: list[RuleError]
) -> bool:
    if manifest and manifest.get("house_bills") is not None:
        return True
    errors.append(RuleError(
        "MANIFEST_MISSING",
        "no manifest has been read for this shipment, so nothing can be "
        "checked against it — read the manifest first",
        field="manifest",
    ))
    return False


# ---------------------------------------------------------- cargo report

def validate_cargo_report(
    manifest: dict[str, Any] | None, payload: dict[str, Any]
) -> RuleResult:
    """Check a proposed cargo report against the manifest and the threshold."""
    errors: list[RuleError] = []
    warnings: list[RuleWarning] = []

    if not _require_manifest(manifest, errors):
        return _finish(errors, warnings)

    _check_mawb(payload, manifest, errors)

    sac_bills, over_bills = derive_sac_split(manifest)
    manifest_index = _by_hawb(manifest.get("house_bills") or [])
    sac_index = _by_hawb(sac_bills)
    over_index = _by_hawb(over_bills)

    reported = payload.get("house_bills")
    if not isinstance(reported, list) or not reported:
        errors.append(RuleError(
            "NO_HOUSE_BILLS",
            "a cargo report must carry at least one house bill",
            field="house_bills", expected=">= 1", actual=reported,
        ))
        return _finish(errors, warnings)

    seen: set[str] = set()
    reported_pieces = 0.0
    reported_weight = 0.0

    for index, bill in enumerate(reported):
        label = f"house_bills[{index}]"
        if not isinstance(bill, dict):
            errors.append(RuleError(
                "BILL_MALFORMED", "house bill must be an object",
                field=label, expected="object", actual=type(bill).__name__,
            ))
            continue

        hawb = str(bill.get("hawb") or "")

        if hawb in seen:
            errors.append(RuleError(
                "HAWB_DUPLICATED",
                f"house bill {hawb} appears more than once on the report",
                field=f"{label}.hawb", expected="one occurrence", actual=hawb,
            ))
            continue
        seen.add(hawb)

        source = manifest_index.get(hawb)
        if source is None:
            errors.append(RuleError(
                "HAWB_UNKNOWN",
                f"house bill {hawb or '(missing)'} does not appear in the manifest",
                field=f"{label}.hawb",
                expected="a HAWB from the manifest", actual=hawb or None,
            ))
            continue

        if hawb in over_index:
            errors.append(RuleError(
                "BILL_OVER_THRESHOLD",
                f"house bill {hawb} is declared at AUD "
                f"{source.get('value_aud')} which is above the AUD "
                f"{SAC_THRESHOLD_AUD:.0f} threshold, so it needs an import "
                "declaration by a licensed broker and must not appear on the "
                "cargo report",
                field=f"{label}.hawb",
                expected=f"value_aud <= {SAC_THRESHOLD_AUD:.0f}",
                actual=source.get("value_aud"),
            ))
            continue

        if hawb not in sac_index:
            errors.append(RuleError(
                "BILL_NOT_SAC",
                f"house bill {hawb} is not in the self-assessed clearance set "
                "derived from the manifest",
                field=f"{label}.hawb", expected="a SAC house bill", actual=hawb,
            ))
            continue

        # Fields the lodgement asserts must be the manifest's figures, not the
        # model's recollection of them.
        for key, tolerance in (("pieces", 0), ("weight_kg", _WEIGHT_TOLERANCE),
                               ("value_aud", _MONEY_TOLERANCE)):
            claimed = _number(bill.get(key))
            truth = _number(source.get(key))
            if claimed is None:
                errors.append(RuleError(
                    "FIELD_MISSING", f"{key} must be a number",
                    field=f"{label}.{key}", expected=truth, actual=bill.get(key),
                ))
                continue
            if truth is None:
                warnings.append(RuleWarning(
                    "MANIFEST_FIELD_UNREADABLE",
                    f"the manifest's {key} for {hawb} could not be read, so the "
                    "reported figure cannot be checked against it",
                    field=f"{label}.{key}", actual=claimed,
                ))
                continue
            if not math.isclose(claimed, truth, rel_tol=0.0,
                                abs_tol=max(tolerance, 0.0)):
                errors.append(RuleError(
                    "FIELD_MISMATCH",
                    f"{key} for house bill {hawb} does not match the manifest",
                    field=f"{label}.{key}", expected=truth, actual=claimed,
                ))

        if bill.get("sac") is not True:
            errors.append(RuleError(
                "SAC_FLAG_MISMATCH",
                f"house bill {hawb} is a self-assessed clearance by value but "
                "is not flagged sac=true",
                field=f"{label}.sac", expected=True, actual=bill.get("sac"),
            ))

        pieces = _number(source.get("pieces"))
        weight = _number(source.get("weight_kg"))
        reported_pieces += pieces or 0.0
        reported_weight += weight or 0.0

    for hawb in sac_index:
        if hawb not in seen:
            warnings.append(RuleWarning(
                "SAC_BILL_OMITTED",
                f"house bill {hawb} qualifies as a self-assessed clearance but "
                "is not on this report",
                field="house_bills", expected=hawb, actual=None,
            ))

    _reconcile(manifest, over_bills, reported_pieces, reported_weight, warnings)
    return _finish(errors, warnings)


def _reconcile(
    manifest: dict[str, Any],
    over_bills: list[dict[str, Any]],
    reported_pieces: float,
    reported_weight: float,
    warnings: list[RuleWarning],
) -> None:
    """Reconcile the report against the master totals.

    A cargo report only carries the SAC bills, so its own totals are always
    below the master's. What has to reconcile is the reported figures plus the
    bills deliberately excluded: if those together do not come back to the
    master totals, either a bill is missing from the report or the manifest
    itself does not add up.
    """
    master = manifest.get("master") or {}
    excluded_pieces = sum(_number(b.get("pieces")) or 0.0 for b in over_bills)
    excluded_weight = sum(_number(b.get("weight_kg")) or 0.0 for b in over_bills)

    total_pieces = _number(master.get("total_pieces"))
    if total_pieces is not None:
        accounted = reported_pieces + excluded_pieces
        if not math.isclose(accounted, total_pieces, rel_tol=0.0, abs_tol=0.0):
            warnings.append(RuleWarning(
                "RECONCILIATION_MISMATCH",
                f"reported pieces ({reported_pieces:g}) plus excluded "
                f"({excluded_pieces:g}) comes to {accounted:g}, but the master "
                f"block declares {total_pieces:g}",
                field="house_bills.pieces",
                expected=total_pieces, actual=accounted,
            ))

    total_weight = _number(master.get("total_weight_kg"))
    if total_weight is not None:
        accounted = reported_weight + excluded_weight
        if not math.isclose(accounted, total_weight, rel_tol=0.0, abs_tol=0.05):
            warnings.append(RuleWarning(
                "RECONCILIATION_MISMATCH",
                f"reported weight ({reported_weight:.1f} kg) plus excluded "
                f"({excluded_weight:.1f} kg) comes to {accounted:.1f} kg, but "
                f"the master block declares {total_weight:.1f} kg",
                field="house_bills.weight_kg",
                expected=total_weight, actual=round(accounted, 3),
            ))


# -------------------------------------------------------------- underbond

def validate_underbond(
    manifest: dict[str, Any] | None, payload: dict[str, Any]
) -> RuleResult:
    """Check a proposed underbond movement request."""
    errors: list[RuleError] = []
    warnings: list[RuleWarning] = []

    if not _require_manifest(manifest, errors):
        return _finish(errors, warnings)

    _check_mawb(payload, manifest, errors)

    reason = payload.get("reason")
    from_location = payload.get("from_location")
    to_location = payload.get("to_location")

    if reason not in VALID_UNDERBOND_REASONS:
        errors.append(RuleError(
            "UNDERBOND_REASON_INVALID",
            "movement reason must be one of " + ", ".join(VALID_UNDERBOND_REASONS),
            field="reason",
            expected=list(VALID_UNDERBOND_REASONS), actual=reason,
        ))
    elif (
        isinstance(from_location, str) and isinstance(to_location, str)
        and from_location.startswith("FW") and to_location.startswith("I")
        and reason != "DCL"
    ):
        # Terminal to depot is a customs-controlled leg: only a movement to a
        # depot for clearance is permitted on it.
        errors.append(RuleError(
            "UNDERBOND_LEG_REQUIRES_DCL",
            f"a movement from terminal {from_location} to depot {to_location} "
            "accepts reason DCL only",
            field="reason", expected="DCL", actual=reason,
        ))

    master = (manifest or {}).get("master") or {}
    for key, claimed, expected in (
        ("from_location", from_location, master.get("terminal")),
        ("to_location", to_location, master.get("depot")),
    ):
        if expected and claimed and claimed != expected:
            warnings.append(RuleWarning(
                "UNDERBOND_ROUTE_MISMATCH",
                f"{key} does not match the manifest's {key.split('_')[0]} code",
                field=key, expected=expected, actual=claimed,
            ))

    return _finish(errors, warnings)


# ---------------------------------------------------------------- outturn

def normalise_outturn_result(value: Any) -> Any:
    """Map the earlier long-form code onto the ABF code list."""
    if isinstance(value, str) and value in OUTTURN_ALIASES:
        return OUTTURN_ALIASES[value]
    return value


def validate_outturn(
    manifest: dict[str, Any] | None, payload: dict[str, Any]
) -> RuleResult:
    """Check a proposed outturn report."""
    errors: list[RuleError] = []
    warnings: list[RuleWarning] = []

    if not _require_manifest(manifest, errors):
        return _finish(errors, warnings)

    _check_mawb(payload, manifest, errors)

    result = normalise_outturn_result(payload.get("result"))
    if result not in OUTTURN_RESULTS:
        errors.append(RuleError(
            "OUTTURN_RESULT_INVALID",
            "outturn result must be one of " + ", ".join(OUTTURN_RESULTS),
            field="result", expected=list(OUTTURN_RESULTS), actual=payload.get("result"),
        ))

    scanned = payload.get("scanned_count")
    if isinstance(scanned, bool) or not isinstance(scanned, int):
        errors.append(RuleError(
            "OUTTURN_SCANNED_COUNT_TYPE",
            "scanned_count must be a whole number of pieces",
            field="scanned_count", expected="integer", actual=scanned,
        ))
        scanned = None

    if result in OUTTURN_REASON_REQUIRED:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(RuleError(
                "OUTTURN_REASON_REQUIRED",
                f"an outturn reported as {result} requires a reason, and that "
                "reason is recorded against the operator's own name — it has "
                "to come from them, not be inferred",
                field="reason", expected="non-empty text", actual=payload.get("reason"),
            ))

    master = (manifest or {}).get("master") or {}
    expected_pieces = _number(master.get("total_pieces"))

    if result == "NIL" and scanned is not None and expected_pieces is not None:
        if not math.isclose(float(scanned), expected_pieces, rel_tol=0.0, abs_tol=0.0):
            errors.append(RuleError(
                "OUTTURN_NIL_COUNT_MISMATCH",
                f"a NIL outturn means nothing was amiss, but {scanned} pieces "
                f"were scanned against {expected_pieces:g} manifested — report "
                "SH or SU with a reason instead",
                field="scanned_count", expected=expected_pieces, actual=scanned,
            ))

    claimed_expected = payload.get("expected_count")
    if (
        expected_pieces is not None
        and isinstance(claimed_expected, int)
        and not isinstance(claimed_expected, bool)
        and not math.isclose(float(claimed_expected), expected_pieces,
                             rel_tol=0.0, abs_tol=0.0)
    ):
        warnings.append(RuleWarning(
            "RECONCILIATION_MISMATCH",
            "expected_count does not match the manifest's total pieces",
            field="expected_count", expected=expected_pieces, actual=claimed_expected,
        ))

    return _finish(errors, warnings)


# ------------------------------------------------------------- dispatcher

VALIDATORS = {
    "lodge_cargo_report": validate_cargo_report,
    "lodge_underbond_request": validate_underbond,
    "lodge_outturn": validate_outturn,
}


def validate(
    action: str, manifest: dict[str, Any] | None, payload: dict[str, Any]
) -> RuleResult:
    """Validate a proposed lodgement for `action` against the manifest."""
    validator = VALIDATORS.get(action)
    if validator is None:
        return RuleResult(ok=True)
    return validator(manifest, payload)
