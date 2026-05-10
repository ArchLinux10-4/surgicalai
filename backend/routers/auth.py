"""
Auth router — login, setup, and admin user management.

Endpoints:
  GET  /api/auth/setup-required   — is this a fresh install? (open)
  POST /api/auth/setup            — create first admin account (open, once only)
  POST /api/auth/login            — username + password → JWT (open)
  GET  /api/auth/me               — current user info (requires auth)
  GET  /api/auth/users            — list all users (admin only)
  POST /api/auth/users            — create user (admin only)
  PUT  /api/auth/users/{id}       — update user info/role (admin only)
  PUT  /api/auth/users/{id}/password — reset password (admin or self)
  DELETE /api/auth/users/{id}     — delete user (admin only)
"""
import uuid
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import get_db
from auth_utils import hash_password, verify_password, create_access_token

router = APIRouter()


# ─── Schemas ─────────────────────────────────────────────────────────────────

class SetupRequest(BaseModel):
    username: str
    password: str
    email: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    email: str = ""
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    email: str | None = None
    is_admin: bool | None = None
    is_active: bool | None = None


class ChangePasswordRequest(BaseModel):
    new_password: str


class SelfChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _require_admin(request: Request):
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def _get_user_by_username(username: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── Open endpoints ───────────────────────────────────────────────────────────

@router.get("/setup-required")
def setup_required():
    """Returns true if no admin account exists yet (first-run)."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return {"setup_required": count == 0}


@router.post("/setup")
def setup_admin(req: SetupRequest):
    """
    Create the first admin account. Only works when zero users exist.
    Subsequent calls are rejected — use the admin panel to create more users.
    """
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 0:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Setup already complete. Log in with your admin account."
        )

    if len(req.username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user_id = str(uuid.uuid4())
    hashed = hash_password(req.password)
    conn.execute(
        "INSERT INTO users (id, username, email, hashed_password, is_admin) VALUES (?, ?, ?, ?, 1)",
        (user_id, req.username.strip().lower(), req.email.strip().lower(), hashed)
    )
    conn.commit()
    conn.close()

    token = create_access_token(user_id, req.username.strip().lower(), is_admin=True)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "username": req.username.strip().lower(),
            "email": req.email,
            "is_admin": True,
        }
    }


@router.post("/login")
def login(req: LoginRequest):
    """Authenticate and receive a JWT token."""
    user = _get_user_by_username(req.username.strip().lower())
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Update last_login
    conn = get_db()
    conn.execute(
        "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],)
    )
    conn.commit()
    conn.close()

    token = create_access_token(user["id"], user["username"], bool(user["is_admin"]))
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "is_admin": bool(user["is_admin"]),
        }
    }


# ─── Authenticated endpoints ──────────────────────────────────────────────────

@router.get("/me")
def get_me(request: Request):
    """Return current authenticated user's info."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, email, is_admin, created_at, last_login FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)


# ─── Admin endpoints ──────────────────────────────────────────────────────────

@router.get("/users")
def list_users(request: Request):
    """List all users. Admin only."""
    _require_admin(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, email, is_admin, is_active, created_at, last_login FROM users ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/users")
def create_user(req: CreateUserRequest, request: Request):
    """Create a new user. Admin only."""
    _require_admin(request)

    if len(req.username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM users WHERE username = ?", (req.username.strip().lower(),)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="Username already exists")

    user_id = str(uuid.uuid4())
    hashed = hash_password(req.password)
    conn.execute(
        "INSERT INTO users (id, username, email, hashed_password, is_admin) VALUES (?, ?, ?, ?, ?)",
        (user_id, req.username.strip().lower(), req.email.strip().lower(), hashed, int(req.is_admin))
    )
    conn.commit()
    conn.close()

    return {
        "id": user_id,
        "username": req.username.strip().lower(),
        "email": req.email,
        "is_admin": req.is_admin,
        "is_active": True,
    }


@router.put("/users/{user_id}")
def update_user(user_id: str, req: UpdateUserRequest, request: Request):
    """Update user info or role. Admin only."""
    _require_admin(request)
    conn = get_db()
    updates = {}
    if req.email is not None:
        updates["email"] = req.email.strip().lower()
    if req.is_admin is not None:
        updates["is_admin"] = int(req.is_admin)
    if req.is_active is not None:
        updates["is_active"] = int(req.is_active)

    if not updates:
        conn.close()
        return {"ok": True, "updated": []}

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return {"ok": True, "updated": list(updates.keys())}


@router.put("/users/{user_id}/password")
def change_password(user_id: str, req: ChangePasswordRequest, request: Request):
    """Reset a user's password. Admin can reset anyone; user can reset themselves."""
    requester_id = getattr(request.state, "user_id", None)
    is_admin = getattr(request.state, "is_admin", False)

    if not is_admin and requester_id != user_id:
        raise HTTPException(status_code=403, detail="You can only change your own password")

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    conn = get_db()
    hashed = hash_password(req.new_password)
    conn.execute("UPDATE users SET hashed_password = ? WHERE id = ?", (hashed, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/change-password")
def self_change_password(req: SelfChangePasswordRequest, request: Request):
    """
    Self-service password change.

    InfoSec requirements enforced:
    - Current password verified (constant-time bcrypt) before any change
    - New password must differ from current
    - New password must match confirmation
    - Complexity: ≥8 chars, ≥1 upper, ≥1 lower, ≥1 digit
    - No information leakage — wrong current password returns 401, not 404
    """
    import re

    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Confirmation match (fast check first — no DB round-trip needed)
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")

    # Complexity requirements
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not re.search(r'[A-Z]', req.new_password):
        raise HTTPException(status_code=400, detail="Password must include at least one uppercase letter")
    if not re.search(r'[a-z]', req.new_password):
        raise HTTPException(status_code=400, detail="Password must include at least one lowercase letter")
    if not re.search(r'[0-9]', req.new_password):
        raise HTTPException(status_code=400, detail="Password must include at least one number")

    # Fetch current hash
    conn = get_db()
    row = conn.execute(
        "SELECT hashed_password FROM users WHERE id = ? AND is_active = 1", (user_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Verify current password (constant-time bcrypt)
    if not verify_password(req.current_password, row["hashed_password"]):
        conn.close()
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Reject reuse
    if verify_password(req.new_password, row["hashed_password"]):
        conn.close()
        raise HTTPException(status_code=400, detail="New password must be different from your current password")

    # Commit
    hashed = hash_password(req.new_password)
    conn.execute("UPDATE users SET hashed_password = ? WHERE id = ?", (hashed, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, request: Request):
    """Delete a user. Admin only. Cannot delete yourself."""
    _require_admin(request)
    requester_id = getattr(request.state, "user_id", None)
    if requester_id == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")

    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"ok": True}
