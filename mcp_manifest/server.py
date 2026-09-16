"""A genuine MCP server exposing read_manifest over stdio.

This is a separate program, spawned as a subprocess and spoken to over the
Model Context Protocol — not a local function with a label on it. That matters
for the assessment: everything this server returns reaches the agent loop
across a real process boundary, which makes it a hop on the pipeline diagram
and a trust boundary in the control points table. The loop is trusting the
output of another program, over a transport, and the content of that output
originates in a PDF that arrived from outside the company.

Run standalone with:  python -m mcp_manifest.server
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .parser import ManifestParseError, parse_manifest

mcp = MCPServer(
    "freight-manifest",
    version="1.0.0",
    instructions=(
        "Reads Australian air cargo manifest PDFs. Returns raw extracted "
        "fields only; it does not classify house bills for clearance."
    ),
)


@mcp.tool()
def read_manifest(file: str) -> dict[str, Any]:
    """Extract the master air waybill details and all house bills from a manifest PDF.

    Returns raw extracted fields only: a `master` object with twelve fields, a
    `house_bills` array with ten fields per bill, and `extraction_warnings`.

    It does not state which house bills are self-assessed clearances and which
    require a licensed broker. That split is not printed on a manifest and must
    be worked out from each bill's `value_aud` against the AUD 1,000 threshold.

    Args:
        file: Path to the manifest PDF.
    """
    try:
        return parse_manifest(file)
    except ManifestParseError as exc:
        # Surfaced to the model as a tool error rather than crashing the server;
        # the loop decides what to do with it.
        raise ValueError(str(exc)) from exc


if __name__ == "__main__":
    mcp.run(transport="stdio")
