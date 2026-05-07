"""
Fernet encryption for API keys — derives encryption key from JWT secret.
No extra env var needed. Keys are encrypted at rest in the DB.
"""
import base64
import hashlib
from cryptography.fernet import Fernet


_fernet_cache = None


def _get_fernet():
    """Derive a Fernet key from the JWT secret (PBKDF2-SHA256, 32 bytes, base64)."""
    global _fernet_cache
    if _fernet_cache:
        return _fernet_cache

    from auth_utils import get_jwt_secret
    secret = get_jwt_secret()

    # PBKDF2 with fixed salt — deterministic, same secret = same key
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        b"surgicalai-api-key-encryption-salt",
        iterations=100_000,
        dklen=32,
    )
    fernet_key = base64.urlsafe_b64encode(dk)
    _fernet_cache = Fernet(fernet_key)
    return _fernet_cache


def encrypt_api_key(plaintext):
    """Encrypt an API key string to base64 ciphertext string."""
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_api_key(ciphertext):
    """Decrypt a base64 ciphertext string to plaintext API key."""
    if not ciphertext:
        return ""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
