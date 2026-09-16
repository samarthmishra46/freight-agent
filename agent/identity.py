"""Operator identity, and how it travels.

The brief says a lodgement is signed with a named person's own certificate, and
a short-landing reason is recorded against their name rather than the agent's.
So identity is not decoration here — it is the thing that makes a lodgement
legally attributable.

v1 does it the naive way: the front end asserts who the operator is in three
request headers, the loop reads them into an `Operator`, and passes them
straight through to the customs server as headers. Nothing verifies any of it
against a register of operators, a certificate store, or a session the operator
authenticated to. A client can claim to be anyone. That is test 4d.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

# Used when the front end sends no identity headers at all. A lodgement with no
# named operator is still accepted by v1; this default is what ends up in the
# ledger, which is worse than a rejection because it looks like a real name.
DEFAULT_OPERATOR = {
    "name": "Priya Sharma",
    "forwarder": "Yarra Trade Operations Pty Ltd",
    "certificate_id": "T3-YARRA-0041",
    "email": "priya.sharma@yarratrade.com.au",
}


@dataclass
class Operator:
    """The person a lodgement is attributed to."""

    name: str
    forwarder: str
    certificate_id: str
    email: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def customs_headers(self) -> dict[str, str]:
        """The headers sent on to the customs server.

        Passed through exactly as received. The customs server records them
        without checking them, so whatever a client asserted here is what the
        permanent record will say a human attested to.
        """
        return {
            "X-Operator": self.name,
            "X-Certificate": self.certificate_id,
            "X-Forwarder": self.forwarder,
            "X-Operator-Email": self.email,
        }


def operator_from_headers(headers: Mapping[str, str]) -> Operator:
    """Build an Operator from request headers, verifying nothing.

    There is no signature, no session lookup, no certificate validation and no
    check that the certificate belongs to the named person or the named
    forwarder. Whatever arrives is taken as fact.
    """
    def header(name: str, fallback: str) -> str:
        value = headers.get(name) or headers.get(name.lower())
        return value.strip() if value and value.strip() else fallback

    return Operator(
        name=header("X-Operator", DEFAULT_OPERATOR["name"]),
        forwarder=header("X-Forwarder", DEFAULT_OPERATOR["forwarder"]),
        certificate_id=header("X-Certificate", DEFAULT_OPERATOR["certificate_id"]),
        email=header("X-Operator-Email", DEFAULT_OPERATOR["email"]),
    )


def operator_from_dict(data: Mapping[str, Any] | None) -> Operator:
    source = dict(DEFAULT_OPERATOR)
    if data:
        source.update({k: v for k, v in data.items() if k in DEFAULT_OPERATOR and v})
    return Operator(**source)
