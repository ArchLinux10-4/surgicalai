"""MCP connections — local install only.

Hosted deploys get 404 on every mutating route and `{available: false}`
from GET /status. Importing this module does not import the mcp package.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from database import USE_POSTGRES
from services.mcp_client import (
    INSTALL_HINT,
    MAX_SERVERS,
    add_server,
    delete_server,
    load_servers,
    mcp_allowed,
    mcp_installed,
    public_server,
    set_always_allow,
    set_enabled,
    test_server,
    validate_spec,
)

router = APIRouter()


def _guard_local_only():
    if not mcp_allowed():
        raise HTTPException(status_code=404, detail="Not available on hosted deployments.")


def _user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", "") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


class SecretRow(BaseModel):
    name: str = ""
    value: str = ""


class ConnectionBody(BaseModel):
    confirmed: bool = False
    name: str = ""
    transport: str = ""
    url: str = ""
    headers: List[SecretRow] = Field(default_factory=list)
    command: str = ""
    args: List[str] = Field(default_factory=list)
    env: List[SecretRow] = Field(default_factory=list)


class PatchBody(BaseModel):
    enabled: Optional[bool] = None
    tool: str = ""
    always_allow: Optional[bool] = None


def _spec(body: ConnectionBody) -> dict:
    return {
        "name": body.name,
        "transport": body.transport,
        "url": body.url,
        "headers": [row.model_dump() for row in body.headers],
        "command": body.command,
        "args": list(body.args),
        "env": [row.model_dump() for row in body.env],
    }


@router.get("/status")
def mcp_status(request: Request):
    _user_id(request)
    if USE_POSTGRES or not mcp_allowed():
        return {
            "available": False,
            "installed": False,
            "install_command": INSTALL_HINT,
            "connections": [],
        }
    user_id = _user_id(request)
    try:
        connections = [public_server(s) for s in load_servers(user_id)]
    except Exception:
        connections = []
    return {
        "available": True,
        "installed": mcp_installed(),
        "install_command": INSTALL_HINT,
        "connections": connections,
    }


@router.post("/test")
async def mcp_test(body: ConnectionBody, request: Request):
    _guard_local_only()
    _user_id(request)
    if not mcp_installed():
        raise HTTPException(status_code=400, detail="MCP client is not installed. " + INSTALL_HINT)
    result = await test_server(_spec(body))
    tools = [
        {"name": t.get("name") or "", "description": t.get("description") or ""}
        for t in result.get("tools") or []
    ]
    return {"ok": bool(result.get("ok")), "tools": tools, "error": result.get("error") or ""}


@router.post("/connections")
async def mcp_save(body: ConnectionBody, request: Request):
    _guard_local_only()
    user_id = _user_id(request)
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Confirm the tool list before saving.")
    if not mcp_installed():
        raise HTTPException(status_code=400, detail="MCP client is not installed. " + INSTALL_HINT)
    spec = _spec(body)
    problem = validate_spec(spec)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if len(load_servers(user_id)) >= MAX_SERVERS:
        raise HTTPException(status_code=400, detail=f"You can connect up to {MAX_SERVERS} MCP servers.")
    result = await test_server(spec)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "The connection test failed.")
    try:
        server = add_server(user_id, spec, result.get("tools") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "connection": public_server(server)}


@router.patch("/connections/{server_id}")
def mcp_patch(server_id: str, body: PatchBody, request: Request):
    _guard_local_only()
    user_id = _user_id(request)
    try:
        if body.tool and body.always_allow is not None:
            server = set_always_allow(user_id, server_id, body.tool, body.always_allow)
        elif body.enabled is not None:
            server = set_enabled(user_id, server_id, body.enabled)
        else:
            raise HTTPException(status_code=400, detail="Nothing to update.")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "connection": public_server(server)}


@router.delete("/connections/{server_id}")
def mcp_delete(server_id: str, request: Request):
    _guard_local_only()
    user_id = _user_id(request)
    if not delete_server(user_id, server_id):
        raise HTTPException(status_code=404, detail="That connection was not found.")
    return {"ok": True}
