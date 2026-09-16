"""The agentic loop.

This is a deliberately unhardened baseline built as the subject of a security
assessment. It implements none of the following, on purpose:

  - any check that a human confirmed a lodgement before it is sent
  - validation of the model's tool arguments against the parsed manifest
  - server-side recomputation of the AUD 1,000 threshold
  - sanitisation, filtering or classification of text extracted from the PDF
  - verification of the operator identity headers
  - path restrictions on the file argument the model chooses
  - size caps on tool responses entering the model's context

A hardened version of this file would make the security tests pass and leave
nothing to report. See NOTES-controls-i-noticed.md for the running list of
controls noticed while building this and deliberately left out.

The hops are laid out in the order the diagram numbers them, so the two can be
read against each other:

    hop 2   frontend->loop     operator message arrives
    hop 4   loop->llm          leaves our infrastructure
    hop 5   llm->loop          untrusted model output, may contain tool calls
    hop 6   loop->mcp          dispatch, file path chosen by the model
    hop 8   mcp->loop          third-party document content
    hop 9   loop->llm          tool_result: outside content enters context
    hop 14  loop->customs      irreversible
    hop 15  customs->loop      response
    hop 17  loop->frontend     reply to the operator
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI

from . import audit, state, tools
from .mcp_client import ManifestMCPClient
from .prompts import SYSTEM_PROMPT
from .state import Session

MODEL = os.environ.get("MODEL", "gpt-4o")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "12"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))

_client: AsyncOpenAI | None = None


def llm_client() -> AsyncOpenAI:
    """One API client for the process. Replaced wholesale by the tests."""
    global _client
    if _client is None:
        _client = AsyncOpenAI()
    return _client


def _assistant_message(message: Any) -> dict[str, Any]:
    """Convert the SDK's reply into a plain dict for the history.

    OpenAI keeps text and tool calls on one assistant message rather than in a
    list of blocks, and each tool call carries its arguments as a JSON string.
    """
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": message.content,
    }
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in calls
        ]
    return entry


def _tool_arguments(raw: str | None) -> dict[str, Any]:
    """Decode a tool call's arguments.

    The model supplies these as a JSON string. Decoding is necessary to make
    the call at all; nothing here checks that the decoded arguments are
    sensible, match the tool's schema, or agree with the parsed manifest.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"__unparsable_arguments__": raw}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


async def run_turn(
    session: Session,
    user_message: str,
    mcp: ManifestMCPClient,
    scope_verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one operator turn to completion, looping until the model stops.

    Returns the reply text plus the hops made, so the front end's plumbing view
    shows real byte counts from real hops rather than a reconstruction.
    """
    hop_log: list[dict[str, Any]] = []

    # hop 2 — the operator's message reaches the loop, having passed the scope
    # check at the entry point.
    hop_log.append(audit.log_hop(
        session.session_id, "frontend->loop", user_message, turn=0,
        action="operator_message", operator=session.operator.name,
        verdict=(scope_verdict or {}).get("verdict", "in_scope"),
        scope_verdict=scope_verdict,
        notes=f"operator message, {len(user_message)} chars",
    ))

    session.history.append({"role": "user", "content": user_message})

    # Local tool schemas and MCP tool schemas go to the model in one array. The
    # model cannot tell which of its tools cross a process boundary.
    tool_schemas = tools.LOCAL_TOOL_SCHEMAS + mcp.to_llm_tool_schema()

    reply = ""
    turn = 0
    cut_short = False

    while turn < MAX_TURNS:
        turn += 1

        # The system prompt travels as the first message rather than as its own
        # parameter, so it is in the same array as the conversation and the
        # manifest content — and is weighted no differently by the provider.
        request = {
            "model": MODEL,
            "max_completion_tokens": MAX_TOKENS,
            "tools": tool_schemas,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}]
                        + session.history,
        }

        # hop 4 — LEAVES OUR INFRASTRUCTURE. Everything assembled above goes to
        # the model provider: the system prompt, all four tool definitions, the
        # entire conversation history, and — once the manifest has been read —
        # every importer's name, address, goods description and commercial
        # value, on this turn and on every turn after it.
        hop_log.append(audit.log_hop(
            session.session_id, "loop->llm", request, turn=turn, model=MODEL,
            action="llm_request", operator=session.operator.name, verdict="sent",
            notes="system prompt + %d tool defs + %d history messages"
                  % (len(tool_schemas), len(session.history)),
        ))

        response = await llm_client().chat.completions.create(**request)

        choice = response.choices[0]
        message = choice.message

        # hop 5 — UNTRUSTED. This is model output, and it is an input to our
        # system. Everything in it, including tool arguments, is a suggestion.
        assistant = _assistant_message(message)
        tool_calls = assistant.get("tool_calls", [])
        hop_log.append(audit.log_hop(
            session.session_id, "llm->loop", assistant, turn=turn, model=MODEL,
            action="llm_reply", operator=session.operator.name,
            verdict=choice.finish_reason,
            notes=f"finish_reason={choice.finish_reason}, "
                  f"{len(tool_calls)} tool call(s)",
        ))

        session.history.append(assistant)

        text = (message.content or "").strip()
        if text:
            reply = text

        if not tool_calls:
            break

        # Our code dispatches. The model asked; it did not act.
        #
        # hop 9 — outside content enters the model's context. Whatever the MCP
        # server or the customs server returned is appended verbatim, as one
        # tool message per call, which is what the provider requires.
        for call in tool_calls:
            name = call["function"]["name"]
            arguments = _tool_arguments(call["function"]["arguments"])
            try:
                result = await tools.dispatch(name, arguments, session, mcp, turn)
                content = _as_tool_content(result)
            except Exception as exc:
                audit.log_hop(
                    session.session_id, "llm->loop", str(exc), turn=turn,
                    tool=name, notes=f"tool raised {type(exc).__name__}",
                )
                content = f"{type(exc).__name__}: {exc}"

            session.history.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": content,
            })
    else:
        # The loop ran out of iterations rather than the model finishing.
        cut_short = True
        reply = (
            reply
            + f"\n\n[The loop stopped after {MAX_TURNS} iterations without "
              "finishing. Nothing further was lodged.]"
        ).strip()

    state.save(session)

    # hop 17 — back to the operator.
    hop_log.append(audit.log_hop(
        session.session_id, "loop->frontend", reply, turn=turn,
        action="operator_reply", operator=session.operator.name,
        verdict="cut_short" if cut_short else "complete",
        notes=f"{turn} loop iteration(s)"
              + (f", MAX_TURNS={MAX_TURNS} reached, loop cut short" if cut_short else ""),
    ))

    return {
        "reply": reply,
        "turns": turn,
        "cut_short": cut_short,
        "scope_verdict": scope_verdict,
        "hops": hop_log,
        "state": session.public_state(),
    }


def _as_tool_content(result: Any) -> str:
    """Render a tool result for the model's context.

    Passed through whole. No size cap, no redaction, no marking to separate a
    third-party document's content from the loop's own instructions.
    """
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)
