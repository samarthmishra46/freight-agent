"""Air cargo manifest PDF -> structured records.

This module returns **raw extracted facts only**. It deliberately does not
decide which house bills are self-assessed clearances and which need a broker:
that split is derived from ``value_aud`` downstream, by our own code. A manifest
arrives from outside the company and nobody verified who wrote it, so reading a
clearance classification out of the document would mean trusting an untrusted
document's classification of itself.

Extraction is layered: ``page.extract_tables()`` first, because the manifest's
house-bill block is a real bordered table and comes out cleanly, then a
word-position fallback over ``extract_words()`` for master fields the table pass
missed, then a regex fallback over ``extract_text()`` for house-bill rows. Every
field that cannot be read lands in ``extraction_warnings`` rather than being
guessed at or filled with a placeholder.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pdfplumber

MASTER_FIELDS = (
    "mawb",
    "carrier",
    "flight",
    "origin",
    "destination",
    "eta",
    "forwarder_indicator",
    "terminal",
    "depot",
    "total_house_bills",
    "total_pieces",
    "total_weight_kg",
)

HOUSE_BILL_FIELDS = (
    "hawb",
    "consignor",
    "consignee",
    "delivery_address",
    "pieces",
    "weight_kg",
    "goods_description",
    "value_aud",
    "currency",
    "origin_country",
)

# Manifest label text -> our field name. Labels are matched case-insensitively
# against the start of a cell, so the master block is read by label rather than
# by column position.
MASTER_LABELS: dict[str, str] = {
    "MAWB NUMBER": "mawb",
    "CARRIER": "carrier",
    "FLIGHT": "flight",
    "ORIGIN": "origin",
    "DESTINATION DEPOT": "depot",
    "DESTINATION": "destination",
    "ETA": "eta",
    "FREIGHT FORWARDER INDICATOR": "forwarder_indicator",
    "TERMINAL (CTO)": "terminal",
    "TOTAL HOUSE BILLS": "total_house_bills",
    "TOTAL PIECES": "total_pieces",
    "TOTAL GROSS WEIGHT": "total_weight_kg",
}

# Column header text -> our field name, for the house-bill table.
HOUSE_BILL_HEADERS: dict[str, str] = {
    "HAWB": "hawb",
    "CONSIGNOR": "consignor",
    "CONSIGNEE": "consignee",
    "DELIVERY ADDRESS": "delivery_address",
    "PCS": "pieces",
    "WEIGHT KG": "weight_kg",
    "GOODS DESCRIPTION": "goods_description",
    "INVOICE VALUE": "value_aud",
    "CCY": "currency",
    "ORIGIN": "origin_country",
}

# Markers of a clearance classification printed on the document itself. If one
# of these appears we ignore it and say so — see _note_unused_classification.
CLASSIFICATION_MARKERS: tuple[tuple[str, str], ...] = (
    (r"clearance\s+treatment", "a 'clearance treatment' heading"),
    (r"threshold\s+test", "a 'threshold test' column"),
    (r"\bSAC\b", "a 'SAC' marking"),
    (r"self[-\s]assessed\s+clearance", "a 'self-assessed clearance' marking"),
    (r"import\s+declaration", "an 'import declaration' marking"),
    (r"\bbroker\b", "a 'broker' marking"),
    (r"[≤<>]=?\s*1[,.]?000", "an explicit AUD 1,000 threshold test"),
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MAWB_RE = re.compile(r"\b(\d{11})\b")
_FLIGHT_RE = re.compile(r"\b([A-Z]{2}\d{2,4})\b")
_PORT_RE = re.compile(r"\b([A-Z]{5})\b")
_ETA_RE = re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s+(\d{1,2}:\d{2})\b")
_TERMINAL_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z])\b")
_DEPOT_RE = re.compile(r"\b([A-Z]\d{3}[A-Z])\b")
_WEIGHT_RE = re.compile(r"([\d,]+\.?\d*)\s*kg", re.IGNORECASE)


class ManifestParseError(Exception):
    """Raised when the PDF cannot be opened or contains no readable page."""


# --------------------------------------------------------------------------
# value coercion
# --------------------------------------------------------------------------

def _clean(text: Any) -> str:
    """Collapse the whitespace pdfplumber leaves in wrapped cells."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _to_float(raw: str, label: str, warnings: list[str]) -> float | None:
    """Parse a money or weight figure, stripping thousands commas and symbols."""
    text = re.sub(r"[^\d.\-]", "", _clean(raw).replace(",", ""))
    if not text:
        warnings.append(f"{label}: no numeric value found in {_clean(raw)!r}")
        return None
    try:
        return float(text)
    except ValueError:
        warnings.append(f"{label}: {_clean(raw)!r} does not convert to a number")
        return None


def _to_int(raw: str, label: str, warnings: list[str]) -> int | None:
    value = _to_float(raw, label, warnings)
    if value is None:
        return None
    if value != int(value):
        warnings.append(f"{label}: {_clean(raw)!r} is not a whole number")
        return None
    return int(value)


def _to_eta(raw: str, warnings: list[str]) -> str | None:
    """'10 Dec 2025 14:30' -> '2025-12-10T14:30'."""
    text = _clean(raw)
    match = _ETA_RE.search(text)
    if not match:
        warnings.append(f"master.eta: could not read a date and time from {text!r}")
        return None
    day_month_year, time_part = match.groups()
    day, month_name, year = day_month_year.split()
    month = MONTHS.get(month_name[:3].lower())
    if month is None:
        warnings.append(f"master.eta: unrecognised month in {text!r}")
        return None
    hour, minute = time_part.split(":")
    return f"{int(year):04d}-{month:02d}-{int(day):02d}T{int(hour):02d}:{int(minute):02d}"


def _to_bool(raw: str, warnings: list[str]) -> bool | None:
    text = _clean(raw).upper()
    if text.startswith("YES") or text.startswith("TRUE") or text.startswith("Y "):
        return True
    if text.startswith("NO") or text.startswith("FALSE") or text == "N":
        return False
    warnings.append(
        f"master.forwarder_indicator: could not read yes/no from {_clean(raw)!r}"
    )
    return None


def _first_code(pattern: re.Pattern[str], raw: str, label: str,
                warnings: list[str]) -> str | None:
    """Take the first code matching `pattern`, dropping any trailing prose.

    Master codes are printed as 'SGSIN — Singapore' and 'I028N — Jupiter Air
    Oceania'; only the code is a fact about the shipment.
    """
    match = pattern.search(_clean(raw))
    if not match:
        warnings.append(f"{label}: no code found in {_clean(raw)!r}")
        return None
    return match.group(1)


# --------------------------------------------------------------------------
# master block
# --------------------------------------------------------------------------

def _label_for(cell: str) -> tuple[str, str] | None:
    """Split a 'LABEL\\nvalue' master cell into (field name, raw value)."""
    text = _clean(cell)
    if not text:
        return None
    # Longest label first, so DESTINATION DEPOT wins over DESTINATION.
    for label in sorted(MASTER_LABELS, key=len, reverse=True):
        if text.upper().startswith(label):
            return MASTER_LABELS[label], text[len(label):].strip()
    return None


def _master_raw_from_tables(tables: list[list[list[Any]]]) -> dict[str, str]:
    """Collect raw master values from 'LABEL value' cells in any table."""
    raw: dict[str, str] = {}
    for table in tables:
        for row in table:
            for cell in row:
                found = _label_for(cell or "")
                if found and found[1] and found[0] not in raw:
                    raw[found[0]] = found[1]
    return raw


def _master_raw_from_words(page: pdfplumber.page.Page) -> dict[str, str]:
    """Fallback: pair label words with the value words sitting under them.

    The master block is a CSS grid, not a bordered table, so some cells merge
    during table extraction. Here labels and values are matched by horizontal
    position instead, which survives the columns being laid out differently.
    """
    words = page.extract_words(use_text_flow=False)
    lines: dict[int, list[dict[str, Any]]] = {}
    for word in words:
        lines.setdefault(round(word["top"] / 3), []).append(word)
    ordered = [sorted(ws, key=lambda w: w["x0"]) for _, ws in sorted(lines.items())]

    raw: dict[str, str] = {}
    for index, line in enumerate(ordered[:-1]):
        joined = " ".join(w["text"] for w in line).upper()
        # Find every label present on this line, with the x of where it starts.
        spans: list[tuple[float, str]] = []
        for label, field in MASTER_LABELS.items():
            position = joined.find(label)
            if position == -1:
                continue
            consumed = 0
            for word in line:
                if consumed >= position:
                    spans.append((word["x0"], field))
                    break
                consumed += len(word["text"]) + 1
        if not spans:
            continue
        spans.sort()
        # The following line holds the values, in the same column positions.
        value_line = ordered[index + 1]
        for slot, (x_start, field) in enumerate(spans):
            x_end = spans[slot + 1][0] if slot + 1 < len(spans) else float("inf")
            # A small tolerance: value text can start slightly left of its label.
            chunk = [w["text"] for w in value_line
                     if x_start - 4 <= w["x0"] < x_end - 4]
            if chunk and field not in raw:
                raw[field] = " ".join(chunk)
    return raw


def _build_master(raw: dict[str, str], warnings: list[str]) -> dict[str, Any]:
    """Coerce raw master strings into the twelve typed master fields."""
    master: dict[str, Any] = dict.fromkeys(MASTER_FIELDS)

    for field in MASTER_FIELDS:
        if field not in raw or not _clean(raw[field]):
            warnings.append(f"master.{field}: not found in the document")

    if raw.get("mawb"):
        master["mawb"] = _first_code(_MAWB_RE, raw["mawb"], "master.mawb", warnings)
    if raw.get("carrier"):
        master["carrier"] = _clean(raw["carrier"])
    if raw.get("flight"):
        master["flight"] = _first_code(_FLIGHT_RE, raw["flight"], "master.flight", warnings)
    if raw.get("origin"):
        master["origin"] = _first_code(_PORT_RE, raw["origin"], "master.origin", warnings)
    if raw.get("destination"):
        master["destination"] = _first_code(
            _PORT_RE, raw["destination"], "master.destination", warnings
        )
    if raw.get("eta"):
        master["eta"] = _to_eta(raw["eta"], warnings)
    if raw.get("forwarder_indicator"):
        master["forwarder_indicator"] = _to_bool(raw["forwarder_indicator"], warnings)
    if raw.get("terminal"):
        master["terminal"] = _first_code(
            _TERMINAL_RE, raw["terminal"], "master.terminal", warnings
        )
    if raw.get("depot"):
        master["depot"] = _first_code(_DEPOT_RE, raw["depot"], "master.depot", warnings)
    if raw.get("total_house_bills"):
        master["total_house_bills"] = _to_int(
            raw["total_house_bills"], "master.total_house_bills", warnings
        )
    if raw.get("total_pieces"):
        master["total_pieces"] = _to_int(
            raw["total_pieces"], "master.total_pieces", warnings
        )
    if raw.get("total_weight_kg"):
        weight = _WEIGHT_RE.search(_clean(raw["total_weight_kg"]))
        master["total_weight_kg"] = _to_float(
            weight.group(1) if weight else raw["total_weight_kg"],
            "master.total_weight_kg",
            warnings,
        )
    return master


# --------------------------------------------------------------------------
# house bills
# --------------------------------------------------------------------------

def _header_map(row: list[Any]) -> dict[str, int] | None:
    """Map field name -> column index, from a house-bill header row.

    Reading the header rather than assuming column order means the empty
    spacer columns pdfplumber emits, or a reordered manifest, cost nothing.
    """
    mapping: dict[str, int] = {}
    for index, cell in enumerate(row):
        text = _clean(cell).upper()
        if not text:
            continue
        for header, field in HOUSE_BILL_HEADERS.items():
            if text == header and field not in mapping:
                mapping[field] = index
    missing = set(HOUSE_BILL_FIELDS) - set(mapping)
    return None if missing else mapping


def _row_to_house_bill(row: list[Any], mapping: dict[str, int],
                       position: int, warnings: list[str]) -> dict[str, Any]:
    """Read one table row into the ten house-bill fields."""
    def cell(field: str) -> str:
        index = mapping[field]
        return _clean(row[index]) if index < len(row) else ""

    label = f"house_bills[{position}]"
    bill: dict[str, Any] = dict.fromkeys(HOUSE_BILL_FIELDS)

    for field in ("hawb", "consignor", "consignee", "delivery_address",
                  "goods_description", "currency", "origin_country"):
        value = cell(field)
        if value:
            bill[field] = value
        else:
            warnings.append(f"{label}.{field}: empty in the document")

    bill["pieces"] = _to_int(cell("pieces"), f"{label}.pieces", warnings)
    bill["weight_kg"] = _to_float(cell("weight_kg"), f"{label}.weight_kg", warnings)
    bill["value_aud"] = _to_float(cell("value_aud"), f"{label}.value_aud", warnings)
    return bill


def _house_bills_from_tables(tables: list[list[list[Any]]],
                             warnings: list[str]) -> list[dict[str, Any]]:
    """Primary path: find the table with a house-bill header and read its rows."""
    bills: list[dict[str, Any]] = []
    for table in tables:
        mapping: dict[str, int] | None = None
        for row in table:
            if mapping is None:
                mapping = _header_map(row)
                continue
            hawb = _clean(row[mapping["hawb"]]) if mapping["hawb"] < len(row) else ""
            if not re.fullmatch(r"\d{10,20}", hawb):
                continue  # totals row, spacer, or a continued header
            bills.append(_row_to_house_bill(row, mapping, len(bills), warnings))
    return bills


# One text line per house bill: HAWB, then free text, then pcs, weight,
# more free text, value, currency, origin country.
_TEXT_ROW_RE = re.compile(
    r"^(?P<hawb>\d{10,20})\s+"
    r"(?P<middle>.+?)\s+"
    r"(?P<pieces>\d+)\s+"
    r"(?P<weight_kg>[\d,]+\.\d+)\s+"
    r"(?P<goods_description>.+?)\s+"
    r"(?P<value_aud>[\d,]+\.\d{2})\s+"
    r"(?P<currency>[A-Z]{3})\s+"
    r"(?P<origin_country>[A-Z]{2})$"
)


def _house_bills_from_text(text: str, warnings: list[str]) -> list[dict[str, Any]]:
    """Fallback path: one regex per line over the text layer.

    Consignor, consignee and delivery address run together in the text layer
    with no delimiter, so this path recovers the numeric and coded fields and
    warns that the three name fields could not be told apart. It is a degraded
    result, and says so, rather than a wrong one.
    """
    bills: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _TEXT_ROW_RE.match(_clean(line))
        if not match:
            continue
        position = len(bills)
        label = f"house_bills[{position}]"
        bill: dict[str, Any] = dict.fromkeys(HOUSE_BILL_FIELDS)
        bill["hawb"] = match.group("hawb")
        bill["goods_description"] = match.group("goods_description")
        bill["currency"] = match.group("currency")
        bill["origin_country"] = match.group("origin_country")
        bill["pieces"] = _to_int(match.group("pieces"), f"{label}.pieces", warnings)
        bill["weight_kg"] = _to_float(
            match.group("weight_kg"), f"{label}.weight_kg", warnings
        )
        bill["value_aud"] = _to_float(
            match.group("value_aud"), f"{label}.value_aud", warnings
        )
        warnings.append(
            f"{label}: read from the text layer, so consignor, consignee and "
            f"delivery_address could not be separated; they ran together as "
            f"{match.group('middle')!r}"
        )
        bills.append(bill)
    return bills


# --------------------------------------------------------------------------
# classification present in the source document
# --------------------------------------------------------------------------

def _note_unused_classification(text: str, warnings: list[str]) -> None:
    """Record, and ignore, any clearance classification printed on the document.

    The SAC / over-threshold split is ours to compute from value_aud. If the
    document asserts one, that assertion is not evidence — but its presence is
    worth knowing about, because a manifest that classifies itself is either
    unusual or tampered with.
    """
    found = [description for pattern, description in CLASSIFICATION_MARKERS
             if re.search(pattern, text, re.IGNORECASE)]
    if found:
        warnings.append(
            "source document contains an unused clearance classification ("
            + ", ".join(found)
            + "); ignored — the SAC/over-threshold split is derived from value_aud"
        )


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def parse_manifest(pdf_path: str | Path) -> dict[str, Any]:
    """Parse an air cargo manifest PDF into master and house-bill records.

    Returns ``{"master": {...12 fields...}, "house_bills": [...10 fields each...],
    "extraction_warnings": [str]}``. Any field that could not be read is ``None``
    with a warning beside it; nothing is substituted with a placeholder.

    No clearance treatment, SAC flag or threshold classification appears in the
    result. That is computed downstream from ``value_aud``.
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise ManifestParseError(f"no such manifest PDF: {path}")

    warnings: list[str] = []
    tables: list[list[list[Any]]] = []
    text_parts: list[str] = []
    first_page: pdfplumber.page.Page | None = None

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise ManifestParseError(f"manifest PDF has no pages: {path}")
            for page in pdf.pages:
                if first_page is None:
                    first_page = page
                tables.extend(page.extract_tables() or [])
                text_parts.append(page.extract_text() or "")

            text = "\n".join(text_parts)

            master_raw = _master_raw_from_tables(tables)
            missing = [f for f in MASTER_LABELS.values() if f not in master_raw]
            if missing:
                # Master cells that merged during table extraction: pair labels
                # with values by position instead.
                for page in pdf.pages:
                    for field, value in _master_raw_from_words(page).items():
                        master_raw.setdefault(field, value)
                    if not [f for f in MASTER_LABELS.values() if f not in master_raw]:
                        break
    except ManifestParseError:
        raise
    except Exception as exc:  # pdfplumber raises a variety of types
        raise ManifestParseError(f"could not read {path}: {exc}") from exc

    master = _build_master(master_raw, warnings)

    house_bills = _house_bills_from_tables(tables, warnings)
    if not house_bills:
        warnings.append(
            "no house-bill table found; falling back to the PDF text layer"
        )
        house_bills = _house_bills_from_text(text, warnings)
    if not house_bills:
        warnings.append("no house bills could be extracted from this document")

    declared = master.get("total_house_bills")
    if declared is not None and declared != len(house_bills):
        warnings.append(
            f"row count mismatch: the master block declares {declared} house "
            f"bills, {len(house_bills)} were extracted"
        )

    _note_unused_classification(text, warnings)

    return {"master": master, "house_bills": house_bills,
            "extraction_warnings": warnings}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m mcp_manifest.parser <manifest.pdf>",
              file=sys.stderr)
        return 2
    try:
        result = parse_manifest(argv[1])
    except ManifestParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
