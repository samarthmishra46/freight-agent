"""MCP client: spawns the freight-manifest server and calls it over stdio.

Everything that comes back from here crossed a process boundary and originated
in a third-party document. v1 takes it exactly as given: no schema validation
of the response, no size cap, no sanitisation, no restriction on the `file`
argument the model chose. Those gaps are deliberate — see
NOTES-controls-i-noticed.md.
"""

from __future__ import annotations

import json
import os
import sys
from types import TracebackType
from typing import Any

from mcp import Client, StdioServerParameters

MCP_SERVER_MODULE = "mcp_manifest.server"

# Tools this client serves. The loop merges these with its local tool schemas
# into one array for the model, so the model cannot tell which of its tools
# cross a process boundary and which do not.
MCP_TOOL_NAMES = ("read_manifest",)


def _server_parameters() -> StdioServerParameters:
    """Spawn the server with the same interpreter that is running the loop."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", MCP_SERVER_MODULE],
        cwd=str(os.getcwd()),
        env=dict(os.environ),
    )


class ManifestMCPClient:
    """Async context manager holding one stdio session to the MCP server.

    The subprocess lives for the lifetime of the client, so a conversation that
    reads the manifest several times does not pay to restart it each turn.
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "ManifestMCPClient":
        self._client = Client(_server_parameters())
        await self._client.__aenter__()
        result = await self._client.list_tools()
        self._tools = list(result.tools if hasattr(result, "tools") else result)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
            self._client = None

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    def to_llm_tool_schema(self) -> list[dict[str, Any]]:
        """Convert the MCP tool definitions into OpenAI function-calling format.

        MCP advertises a JSON Schema per tool, which is what OpenAI wants under
        `parameters`, so this is an envelope change rather than a translation.

        The description the model sees is the one the MCP server advertised, so
        a change to the server's docstring changes what the model is told this
        tool does — without the loop being redeployed or even aware.
        """
        schemas: list[dict[str, Any]] = []
        for tool in self._tools:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool over MCP and return its result.

        The result is returned as the server sent it. v1 does not check that it
        matches the tool's declared output schema, does not cap its size, and
        does not inspect the text inside it before it goes on to the model.
        """
        if self._client is None:
            raise RuntimeError("MCP client used outside its context manager")

        result = await self._client.call_tool(name, arguments)

        if getattr(result, "is_error", False):
            text = _text_of(result)
            raise RuntimeError(f"MCP tool {name} failed: {text}")

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            # MCPServer wraps a non-dict return in {"result": ...}; a dict
            # return comes back as-is.
            return structured.get("result", structured) if set(structured) == {"result"} else structured

        text = _text_of(result)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"result": text}


def _text_of(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        value = getattr(block, "text", None)
        if value:
            parts.append(value)
    return "\n".join(parts)
