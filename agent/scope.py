"""Intent scoping on the inbound operator message.

This assistant does freight reporting work: reading a manifest and preparing
the three customs lodgements. A message about anything else is not something it
should be spending a provider call on, and not something it should be answering
in the operator's name.

Allowlist-style and deterministic: a message is in scope when it affirmatively
matches the freight vocabulary, the lodgement workflow, or the short
conversational replies the workflow needs — confirmations, refusals, counts,
and answers to a question the assistant just asked. Anything that matches
nothing is rejected with a plain explanation and never reaches the model.

Keyword and pattern based on purpose. A classifier belongs in phase 9, and
would be an L2 control; this is an L1 check on a narrow, well-understood
vocabulary, which is why it can be deterministic at all.

Scoping the entry point is a product feature: it keeps the assistant on its job.
It is not a defence against prompt injection. Injected text arrives inside the
manifest PDF, at hop 8, long after this check has passed the operator's message
through. See NOTES-controls-i-noticed.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Freight documentation, customs procedure, and this consignment's own terms.
FREIGHT_TERMS = (
    "manifest", "house bill", "house bills", "housebill", "hawb", "mawb",
    "air waybill", "waybill", "consignment", "shipment", "cargo", "cargo report",
    "underbond", "under bond", "outturn", "out turn", "lodge", "lodges",
    "lodged", "lodging", "lodgement", "lodgment", "customs", "border force",
    "abf", "clearance", "clear", "sac", "self-assessed", "self assessed",
    "declaration", "broker", "threshold", "consignee", "consignor", "importer",
    "depot", "terminal", "warehouse", "freight", "forwarder", "flight",
    "carrier", "pieces", "piece", "weight", "kg", "kilo", "value", "valued",
    "aud", "dollar", "invoice", "goods", "description", "origin", "destination",
    "eta", "arrival", "scanned", "scan", "count", "tally", "short", "shortage",
    "short landed", "short-landed", "surplus", "discrepancy", "reason",
    "movement", "move", "release", "released", "status", "reference",
    "accepted", "rejected", "reject", "split", "excluded", "exclude", "include",
    "summary", "recap", "preview", "check", "verify", "confirm", "confirmed",
    "warning", "warnings", "error", "errors", "rule", "rules", "extraction",
)

# This consignment's identifiers and the code lists, matched as whole tokens so
# that "NIL" does not fire on "nil" inside another word.
FREIGHT_CODES = (
    "SQ100", "SGSIN", "AUSYD", "FW53H", "I028N", "13174990090",
    "DCL", "TRN", "SPL", "WHS", "EXP", "NIL", "SH", "SU", "SC",
)

# Short replies the workflow depends on. An operator answering "yes" or "11 of
# 11" is doing freight work; rejecting those would make the assistant unusable.
CONTINUATION_PATTERNS = (
    r"^\s*(yes|yep|yeah|yup|ok|okay|sure|right|correct|agreed|affirmative)"
    r"(\s+please)?[\s,.!]*$",
    r"^\s*(no|nope|nah|negative|incorrect|wrong)(\s+thanks)?[\s,.!]*$",
    r"^\s*(go ahead|proceed|continue|carry on|do it|send it|go)"
    r"(\s+please)?[\s,.!]*$",
    r"^\s*(stop|wait|hold|hold on|cancel|abort|undo)[\s,.!]*$",
    r"^\s*(thanks|thank you|cheers|ta)[\s,.!]*$",
    r"^\s*\d+(\s*(of|/)\s*\d+)?\s*$",          # "11", "11 of 11", "9/11"
    r"^\s*(next|again|retry|redo|repeat)[\s,.!]*$",
)

# A terse reply is only a reply when it is actually terse. "yes" is an operator
# confirming; "yes, write me a poem" is not, and a longer message has to earn
# its place on the freight vocabulary instead.
CONTINUATION_MAX_CHARS = 40

_TERM_RE = re.compile(
    r"\b(" + "|".join(re.escape(term) for term in
                      sorted(FREIGHT_TERMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_CODE_RE = re.compile(
    r"\b(" + "|".join(re.escape(code) for code in FREIGHT_CODES) + r")\b"
)

_HAWB_RE = re.compile(r"\b\d{10,20}\b")

_CONTINUATIONS = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in CONTINUATION_PATTERNS
)

REJECTION_MESSAGE = (
    "That is outside what I do. I prepare Australian customs lodgements for "
    "this consignment — reading the flight manifest, and preparing the cargo "
    "report, the underbond movement request and the depot outturn. Ask me "
    "about the manifest, a house bill, the AUD 1,000 threshold split, or any "
    "of the three lodgements and I can help."
)


@dataclass
class ScopeVerdict:
    """Whether an operator message is freight reporting work."""

    in_scope: bool
    reason: str
    matched: list[str]
    message_chars: int

    @property
    def verdict(self) -> str:
        return "in_scope" if self.in_scope else "out_of_scope"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "in_scope": self.in_scope,
            "reason": self.reason,
            "matched": self.matched,
            "message_chars": self.message_chars,
        }


def check_scope(message: str) -> ScopeVerdict:
    """Decide whether `message` is freight reporting work.

    Matching is on the operator's own words only. Nothing about the session,
    the manifest or the conversation so far is consulted, so the same message
    always gets the same verdict.
    """
    text = (message or "").strip()
    chars = len(text)

    if not text:
        return ScopeVerdict(
            in_scope=False,
            reason="the message is empty",
            matched=[], message_chars=chars,
        )

    matched: list[str] = []

    for term in dict.fromkeys(m.lower() for m in _TERM_RE.findall(text)):
        matched.append(f"term:{term}")

    for code in _CODE_RE.findall(text):
        matched.append(f"code:{code}")

    if _HAWB_RE.search(text):
        matched.append("pattern:waybill-number")

    if chars <= CONTINUATION_MAX_CHARS:
        for pattern in _CONTINUATIONS:
            if pattern.search(text):
                matched.append(f"reply:{pattern.pattern}")
                break

    if matched:
        return ScopeVerdict(
            in_scope=True,
            reason=f"matched {len(matched)} freight indicator(s)",
            matched=matched[:12], message_chars=chars,
        )

    return ScopeVerdict(
        in_scope=False,
        reason=(
            "the message matches nothing in the freight reporting vocabulary, "
            "the consignment's identifiers, or the workflow's expected replies"
        ),
        matched=[], message_chars=chars,
    )
