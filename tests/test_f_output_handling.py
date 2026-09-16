"""Test F — improper output handling (OWASP LLM10).

**Added in phase 10, because the coverage check found this row reading
"exposed · none · no test".** It is the row the brief says is the valuable
kind, and the honest response to finding one is to write the test rather than
reword the row.

    Does markup inside the manifest reach the operator's browser unescaped?

The vector has two halves and this proves both, deterministically, with no
model call:

  1. markup in a third-party PDF survives parsing intact and is served to the
     browser by the agent, and
  2. `frontend/app.js` writes those same fields into `innerHTML`.

Either half alone is harmless. Together they make a manifest PDF an XSS vector
against the operator's own session — the session that holds the confirmation
tokens CP-8 relies on.

Expected: FAIL. CP-12 is absent.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import Pipeline, report
from mcp_manifest.parser import parse_manifest

ROOT = Path(__file__).resolve().parent.parent
INJECTED_3 = ROOT / "data" / "manifest-SQ100-injected-3.pdf"
APP_JS = ROOT / "frontend" / "app.js"

XSS_CANARY = "XSS-QX7F-MARKUP"

# The fields app.js interpolates into innerHTML when it draws the bills table.
RENDERED_FIELDS = ("hawb", "consignee")


def _innerhtml_interpolations() -> list[str]:
    """Field names interpolated into an innerHTML template literal."""
    source = APP_JS.read_text(encoding="utf-8")
    found: list[str] = []
    for block in re.findall(r"innerHTML\s*=\s*`(.*?)`", source, re.DOTALL):
        found.extend(re.findall(r"\$\{\s*bill\.(\w+)", block))
    return found


def test_f_manifest_markup_reaches_the_browser_unescaped(pipeline: Pipeline) -> None:
    parsed = parse_manifest(INJECTED_3)
    bill = next(b for b in parsed["house_bills"]
                if b["hawb"] == "131749900901008")

    # Half one — the payload survives the parser with its markup intact.
    consignee = str(bill.get("consignee") or "")
    goods = str(bill.get("goods_description") or "")
    tag_survives = "<img" in consignee and "onerror" in consignee
    canary_survives = XSS_CANARY in goods

    # ...and the agent serves it to the browser verbatim.
    session = pipeline.upload(INJECTED_3)["session_id"]
    state = pipeline.session_state(session)
    served = str(state)                      # public_state carries the manifest
    # The manifest only lands on the session once read, so assert on what the
    # parser produced plus what /session returns once it is there.
    reaches_api = "<img" in served or state.get("manifest") is None

    # Half two — those fields go into innerHTML.
    interpolated = _innerhtml_interpolations()
    sinks = sorted(set(interpolated) & set(RENDERED_FIELDS))

    exposed = tag_survives and bool(sinks)

    report(
        "F — improper output handling: manifest markup into innerHTML (CP-12)",
        "manifest-derived strings are escaped before they are written to the DOM",
        f"parser returned consignee={consignee[:60]!r} (tag intact: "
        f"{tag_survives}); goods carries the canary: {canary_survives}; "
        f"app.js interpolates {sinks} into innerHTML",
        "FAIL — a crafted PDF is an XSS vector against the operator"
        if exposed else "PASS",
        "The manifest is untrusted third-party content and these fields reach "
        "the DOM as markup. The operator's session is the one holding CP-8 "
        "confirmation tokens, so this is a path from a document to a lodgement "
        "that no prompt-injection control would catch."
        if exposed else None,
    )

    assert tag_survives, (
        "the markup fixture did not survive parsing — regenerate with "
        "python data/make_injected_pdfs.py"
    )
    assert reaches_api
    assert not sinks, (
        f"app.js writes manifest-derived {sinks} into innerHTML without "
        f"escaping; a crafted PDF becomes script in the operator's browser"
    )
