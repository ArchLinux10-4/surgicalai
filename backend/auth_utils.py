"""
JWT + password hashing utilities for SurgicalAI auth.
"""
import os
import secrets
from datetime import datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from database import get_setting, set_setting

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_jwt_secret() -> str:
    """Return JWT secret — from env var, or auto-generated and stored in DB."""
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        return env_secret
    secret = get_setting("jwt_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        set_setting("jwt_secret", secret)
    return secret


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, username: str, is_admin: bool) -> str:
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "username": username,
        "is_admin": is_admin,
        "exp": expire,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and verify JWT. Raises JWTError on failure."""
    return jwt.decode(token, get_jwt_secret(), algorithms=[ALGORITHM])
