"""
Linear integration router.
Endpoints: connect, status, disconnect, teams, issues, issue detail, comment, complete.
Uses Linear's GraphQL API via httpx — no extra SDK needed.
Per-user encrypted PAT stored in user_api_keys (key_type='linear').
"""
from fastapi import APIRouter, HTTPException, Request
import httpx

from database import get_user_api_key, set_user_api_key
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
    """Per-user only — no global fallback (multi-tenant safe)."""
    if user_id:
        encrypted = get_user_api_key(user_id, "linear")
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                pass
    return ""


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
        data = _gql(key, "{ viewer { id name email avatarUrl } organization { id name } }")
        viewer = data.get("viewer", {})
        org = data.get("organization", {})
        return {
            "connected": True,
            "name": viewer.get("name"),
            "email": viewer.get("email"),
            "avatar_url": viewer.get("avatarUrl"),
            "workspace": org.get("name", ""),
        }
    except Exception:
        return {"connected": False}


@router.post("/connect")
async def linear_connect(body: dict, request: Request):
    # Accept both "api_key" and "token" — frontend sends "token"
    key = (body.get("api_key") or body.get("token") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key required")
    try:
        data = _gql(key, "{ viewer { id name email avatarUrl } organization { id name } }")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Linear API key: {e}")
    viewer = data.get("viewer", {})
    org = data.get("organization", {})
    user_id = _get_user_id(request)
    if user_id:
        set_user_api_key(user_id, "linear", encrypt_api_key(key))
    # Per-user only — no global key storage
    return {
        "ok": True,
        "name": viewer.get("name"),
        "email": viewer.get("email"),
        "avatar_url": viewer.get("avatarUrl"),
        "workspace": org.get("name", ""),
    }


@router.delete("/disconnect")
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
def linear_issues(request: Request, query: str = "", team_id: str = "", state: str = "", limit: int = 20):
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
        filters = []
        if team_id:
            filters.append(f'team: {{ id: {{ eq: "{team_id}" }} }}')
        if state:
            filters.append(f'state: {{ name: {{ eq: "{state}" }} }}')
        filter_clause = f'filter: {{ {", ".join(filters)} }}' if filters else ""
        gql = f"""
        query RecentIssues($limit: Int!) {{
          issues(first: $limit, orderBy: updatedAt {filter_clause}) {{
            nodes {{ id identifier title state {{ name color }} assignee {{ name }} priority url updatedAt }}
          }}
        }}"""
        data = _gql(key, gql, {"limit": limit})
        issues = data.get("issues", {}).get("nodes", [])

    # Normalize state from {name, color} object to flat string for frontend
    for issue in issues:
        st = issue.get("state")
        if isinstance(st, dict):
            issue["state"] = st.get("name", "Unknown")

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


@router.post("/issues/{issue_id}/complete")
async def linear_complete_issue(issue_id: str, body: dict, request: Request):
    """Mark a Linear issue as done — finds the completed workflow state and transitions."""
    user_id = _get_user_id(request)
    key = _get_linear_key(user_id)
    if not key:
        raise HTTPException(status_code=401, detail="Not connected to Linear")

    # 1) Get the issue's team
    issue_data = _gql(key, """
    query GetIssueTeam($id: String!) {
      issue(id: $id) { team { id } }
    }""", {"id": issue_id})
    team_id_val = issue_data.get("issue", {}).get("team", {}).get("id")
    if not team_id_val:
        raise HTTPException(status_code=404, detail="Issue or team not found")

    # 2) Find the "completed" type workflow state for this team
    states_data = _gql(key, f"""
    query {{
      workflowStates(filter: {{ team: {{ id: {{ eq: "{team_id_val}" }} }}, type: {{ eq: "completed" }} }}) {{
        nodes {{ id name }}
      }}
    }}""")
    done_states = states_data.get("workflowStates", {}).get("nodes", [])
    if not done_states:
        raise HTTPException(status_code=400, detail="No completed state found for this team")
    done_state_id = done_states[0]["id"]

    # 3) Transition the issue
    update_data = _gql(key, """
    mutation CompleteIssue($id: String!, $stateId: String!) {
      issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
        issue { id identifier title state { name } }
      }
    }""", {"id": issue_id, "stateId": done_state_id})
    result = update_data.get("issueUpdate", {})

    # 4) Optionally add a comment
    comment = (body.get("comment") or "").strip()
    if comment and result.get("success"):
        _gql(key, """
        mutation AddComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }""", {"issueId": issue_id, "body": comment})

    return {"ok": result.get("success", False), "issue": result.get("issue", {})}


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
