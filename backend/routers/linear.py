"""
Linear integration router.
Endpoints: connect, status, disconnect, teams, issues, issue detail, comment (link commit).
Uses Linear's GraphQL API via httpx — no extra SDK needed.
Per-user encrypted PAT stored in user_api_keys (key_type='linear').
"""
from fastapi import APIRouter, HTTPException, Request
import httpx

from database import get_user_api_key, set_user_api_key, get_setting, set_setting
from crypto_utils import encrypt_api_key, decrypt_api_key
from auth_utils import decode_token

router = APIRouter()

LINEAR_GQL = "https://api.linear.app/graphql"


def _get_user_id(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        payload = decode_token(token)
        if payload:
            return str(payload.get("user_id", ""))
    return ""


def _get_linear_key(user_id: str) -> str:
    if user_id:
        encrypted = get_user_api_key(user_id, "linear")
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                pass
    return get_setting("linear_api_key", "")


def _gql(key: str, query: str, variables: dict = None) -> dict:
    resp = httpx.post(
        LINEAR_GQL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": key, "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise HTTPException(status_code=400, detail=data["errors"][0]["message"])
    return data.get("data", {})


@router.get("/status")
def linear_status(request: Request):
    user_id = _get_user_id(request)
    key = _get_linear_key(user_id)
    if not key:
        return {"connected": False}
    try:
        data = _gql(key, "{ viewer { id name email avatarUrl } }")
        viewer = data.get("viewer", {})
        return {"connected": True, "name": viewer.get("name"), "email": viewer.get("email"),
                "avatar_url": viewer.get("avatarUrl")}
    except Exception:
        return {"connected": False}


@router.post("/connect")
async def linear_connect(body: dict, request: Request):
    key = (body.get("api_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key required")
    try:
        data = _gql(key, "{ viewer { id name email avatarUrl } }")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Linear API key: {e}")
    viewer = data.get("viewer", {})
    user_id = _get_user_id(request)
    if user_id:
        set_user_api_key(user_id, "linear", encrypt_api_key(key))
    set_setting("linear_api_key", key)
    return {"ok": True, "name": viewer.get("name"), "email": viewer.get("email"),
            "avatar_url": viewer.get("avatarUrl")}


@router.post("/disconnect")
def linear_disconnect(request: Request):
    user_id = _get_user_id(request)
    if user_id:
        set_user_api_key(user_id, "linear", "")
    return {"ok": True}


@router.get("/teams")
def linear_teams(request: Request):
    user_id = _get_user_id(request)
    key = _get_linear_key(user_id)
    if not key:
        raise HTTPException(status_code=401, detail="Not connected to Linear")
    data = _gql(key, "{ teams { nodes { id name key } } }")
    return {"teams": data.get("teams", {}).get("nodes", [])}


@router.get("/issues")
def linear_issues(request: Request, query: str = "", team_id: str = "", limit: int = 20):
    user_id = _get_user_id(request)
    key = _get_linear_key(user_id)
    if not key:
        raise HTTPException(status_code=401, detail="Not connected to Linear")

    if query:
        gql = """
        query SearchIssues($query: String!, $limit: Int!) {
          issueSearch(query: $query, first: $limit) {
            nodes { id identifier title state { name color } assignee { name } priority url updatedAt }
          }
        }"""
        data = _gql(key, gql, {"query": query, "limit": limit})
        issues = data.get("issueSearch", {}).get("nodes", [])
    else:
        filter_clause = f'filter: {{ team: {{ id: {{ eq: "{team_id}" }} }} }}' if team_id else ""
        gql = f"""
        query RecentIssues($limit: Int!) {{
          issues(first: $limit, orderBy: updatedAt {filter_clause}) {{
            nodes {{ id identifier title state {{ name color }} assignee {{ name }} priority url updatedAt }}
          }}
        }}"""
        data = _gql(key, gql, {"limit": limit})
        issues = data.get("issues", {}).get("nodes", [])

    return {"issues": issues}


@router.get("/issues/{issue_id}")
def linear_issue_detail(issue_id: str, request: Request):
    user_id = _get_user_id(request)
    key = _get_linear_key(user_id)
    if not key:
        raise HTTPException(status_code=401, detail="Not connected to Linear")
    gql = """
    query IssueDetail($id: String!) {
      issue(id: $id) {
        id identifier title description state { name color }
        assignee { name email } priority url createdAt updatedAt
        labels { nodes { name color } }
        comments { nodes { body createdAt user { name } } }
      }
    }"""
    data = _gql(key, gql, {"id": issue_id})
    return data.get("issue", {})


@router.post("/issues/{issue_id}/comment")
async def linear_add_comment(issue_id: str, body: dict, request: Request):
    """Add a comment to a Linear issue — used to link a GitHub commit."""
    user_id = _get_user_id(request)
    key = _get_linear_key(user_id)
    if not key:
        raise HTTPException(status_code=401, detail="Not connected to Linear")
    comment_body = body.get("body", "")
    if not comment_body:
        raise HTTPException(status_code=400, detail="Comment body required")
    gql = """
    mutation CreateComment($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success comment { id }
      }
    }"""
    data = _gql(key, gql, {"issueId": issue_id, "body": comment_body})
    return data.get("commentCreate", {})
