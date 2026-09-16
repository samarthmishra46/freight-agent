"""FastAPI backend for the freight lodgement agent, port 8000.

Deliberately unhardened: CORS is open, there is no authentication, the operator
identity is whatever the client asserts in headers, the session id is whatever
the client sends, and an uploaded file is written under the name the client
chose. See NOTES-controls-i-noticed.md.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from . import audit, confirmations, identity, scope, state, tools  # noqa: E402
from .identity import operator_from_headers  # noqa: E402
from .loop import MAX_TURNS, MODEL, run_turn  # noqa: E402
from .mcp_client import ManifestMCPClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FRONTEND_DIR = ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold one MCP subprocess for the lifetime of the server."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with ManifestMCPClient() as mcp:
        app.state.mcp = mcp
        yield


app = FastAPI(title="FreightAgent", version="1.0.0", lifespan=lifespan)

# Open to anything. No origin allowlist, no credentials policy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """One operator turn. Runs the loop until the model stops asking for tools."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    # Identity comes from the request headers and is not verified against
    # anything. Session id comes from the client and is not checked against the
    # caller either.
    operator = operator_from_headers(request.headers)
    session = state.get_or_create(body.get("session_id"), operator)

    # Scope the intent before anything reaches the provider. An out-of-scope
    # message costs a provider call, sends the operator's conversation to a
    # third party for no reason, and gets answered in the operator's name.
    verdict = scope.check_scope(message)
    if not verdict.in_scope:
        audit.log_hop(
            session.session_id, "frontend->loop", message, turn=0,
            action="operator_message", operator=operator.name,
            verdict=verdict.verdict, scope_verdict=verdict.as_dict(),
            notes=f"rejected at the entry point, not sent to the model: "
                  f"{verdict.reason}",
        )
        state.save(session)
        return JSONResponse(content={
            "reply": scope.REJECTION_MESSAGE,
            "turns": 0,
            "cut_short": False,
            "scope_verdict": verdict.as_dict(),
            "hops": [],
            "state": session.public_state(),
        })

    result = await run_turn(
        session, message, request.app.state.mcp, scope_verdict=verdict.as_dict()
    )
    return JSONResponse(content=result)


@app.post("/confirm")
async def confirm(request: Request) -> JSONResponse:
    """CP-8 — hops 12, 13 and 14. The only path to the customs server.

    The operator's client posts a token it received in `pending_confirmation`.
    The model never held that token and cannot produce one, so nothing in the
    conversation — including injected manifest text — can reach this endpoint.
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")

    token = body.get("token")
    session_id = body.get("session_id")
    if not isinstance(token, str) or not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="session_id and token are required")

    session = state.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")

    operator = identity.operator_from_headers(request.headers)
    session.operator = operator

    redemption = confirmations.redeem(token, session_id, operator)

    audit.log_hop(
        session_id, "frontend->loop", {"token": "[redacted]", "session_id": session_id},
        turn=0, action="operator_confirmation", operator=operator.name,
        verdict="redeemed" if redemption.ok else "refused",
        notes=f"confirmation redemption: {redemption.reason}",
    )

    if not redemption.ok:
        state.save(session)
        return JSONResponse(status_code=409, content={
            "status": "refused",
            "refused_by": "confirmation check (CP-8)",
            "reason": redemption.reason,
            "message": (
                "Nothing was lodged. " + redemption.reason.capitalize() + "."
            ),
            "state": session.public_state(),
        })

    pending = redemption.pending
    assert pending is not None      # ok=True always carries the record
    result = await tools.lodge_confirmed(session, pending)

    return JSONResponse(content={
        "status": result.get("status"),
        "action": pending.action,
        "reference": result.get("reference"),
        "errors": result.get("errors"),
        "message": result.get("message"),
        "confirmation_sha256": pending.payload_sha256,
        "state": session.public_state(),
    })


@app.post("/upload")
async def upload(request: Request, file: UploadFile) -> JSONResponse:
    """Store an uploaded manifest PDF and return its path.

    The filename is taken from the client. Nothing checks the extension, the
    content type, the size, or that the bytes are a PDF at all — and the
    returned path is what the model is later told to read.
    """
    name = Path(file.filename or "manifest.pdf").name
    destination = DATA_DIR / name
    destination.write_bytes(await file.read())

    operator = operator_from_headers(request.headers)
    session = state.get_or_create(request.query_params.get("session_id"), operator)
    session.manifest_path = str(destination.relative_to(ROOT))
    state.save(session)

    audit.log_hop(
        session.session_id, "frontend->loop", {"filename": name},
        turn=0, notes=f"PDF stored at {destination}, {destination.stat().st_size} bytes",
    )

    return JSONResponse(content={
        "session_id": session.session_id,
        "path": session.manifest_path,
        "filename": name,
        "bytes": destination.stat().st_size,
    })


@app.get("/session/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    """Read a session back. Any caller may read any session."""
    session = state.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    return JSONResponse(content=session.public_state())


@app.get("/session/{session_id}/hops")
async def get_hops(session_id: str) -> JSONResponse:
    """The audit log for one session, for the plumbing view."""
    return JSONResponse(content={"hops": audit.read_hops(session_id)})


@app.get("/config")
async def config() -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_turns": MAX_TURNS,
        "customs_url": os.environ.get("CUSTOMS_URL", "http://localhost:9000"),
        "api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
    }


if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
