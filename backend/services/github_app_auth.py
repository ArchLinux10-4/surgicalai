"""
github_app_auth.py — GitHub App authentication (new, additive, flag-gated).

Wraps PyGithub's Auth.AppAuth / GithubIntegration so callers never touch raw
JWTs or private keys directly. Each call gets a short-lived, auto-refreshing
installation token scoped ONLY to the repos that one specific installation
was granted — never a long-lived, full-account secret like the legacy PAT.

Multi-tenant safety: this app is registered ONCE (App ID / Client ID /
private key are platform-level env vars, never per-user). Hundreds of users
can each install the app independently on their own GitHub account/org;
every installation gets its own installation_id, and a token minted for one
installation_id can never reach another installation's repos.

Env vars required (set once by the platform operator, e.g. in Railway):
  GITHUB_APP_ID           - numeric App ID
  GITHUB_APP_PRIVATE_KEY  - full contents of the .pem private key
  GITHUB_APP_CLIENT_ID    - app client id (not used for JWT auth, kept for
                            future "login with GitHub" / app-user-auth flows)

Legacy PAT flow (routers/github.py) is completely untouched by this file.
"""
import os
from typing import Optional

from database import _dlog

try:
    from github import Auth, Github, GithubIntegration
    PYGITHUB_AVAILABLE = True
except ImportError:
    PYGITHUB_AVAILABLE = False


def is_github_app_configured() -> bool:
    """True only if all three platform-level env vars are present and
    PyGithub is importable. Never raises."""
    try:
        app_id = os.getenv("GITHUB_APP_ID", "").strip()
        private_key = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip()
        configured = bool(PYGITHUB_AVAILABLE and app_id and private_key)
        _dlog("github_app_configured_check",
              pygithub_available=PYGITHUB_AVAILABLE,
              has_app_id=bool(app_id), has_private_key=bool(private_key),
              configured=configured)
        return configured
    except Exception as e:
        _dlog("github_app_configured_check_error", error=str(e))
        return False


def get_app_slug() -> str:
    """App slug used to build the GitHub-hosted install URL. Falls back to
    a sane default if not explicitly set via env var."""
    slug = os.getenv("GITHUB_APP_SLUG", "surgical-ai-github").strip()
    _dlog("github_app_slug_resolved", slug=slug)
    return slug


def _get_app_auth():
    """Build an Auth.AppAuth instance from env vars. Raises if not
    configured — callers must check is_github_app_configured() first for
    graceful user-facing errors."""
    app_id = os.getenv("GITHUB_APP_ID", "").strip()
    private_key = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not app_id or not private_key:
        _dlog("github_app_auth_missing_env", has_app_id=bool(app_id), has_private_key=bool(private_key))
        raise RuntimeError("GitHub App is not configured on this server (missing App ID or private key).")
    try:
        # Private keys pasted into env vars sometimes have literal "\n"
        # instead of real newlines (common Railway/Heroku gotcha) — normalize.
        if "\\n" in private_key and "\n" not in private_key:
            private_key = private_key.replace("\\n", "\n")
        auth = Auth.AppAuth(int(app_id), private_key)
        _dlog("github_app_auth_built", app_id=app_id)
        return auth
    except Exception as e:
        _dlog("github_app_auth_build_failed", error=str(e))
        raise


def get_installation_client(installation_id: str, token_permissions: Optional[dict] = None) -> "Github":
    """Return a github.Github client scoped to one specific installation.

    The returned client's token auto-refreshes (PyGithub handles this
    internally via AppInstallationAuth) — callers do not need to worry
    about expiry. Every call through this client can only touch the repos
    that installation_id was granted at install time.
    """
    if not PYGITHUB_AVAILABLE:
        _dlog("github_app_client_pygithub_missing", installation_id=installation_id)
        raise RuntimeError("PyGithub not installed on server.")
    try:
        app_auth = _get_app_auth()
        install_auth = app_auth.get_installation_auth(int(installation_id), token_permissions)
        client = Github(auth=install_auth)
        _dlog("github_app_installation_client_ok", installation_id=installation_id)
        return client
    except Exception as e:
        _dlog("github_app_installation_client_failed", installation_id=installation_id, error=str(e))
        raise


def get_integration() -> "GithubIntegration":
    """Return a GithubIntegration instance for app-level calls (e.g. looking
    up installation account info right after a user installs)."""
    if not PYGITHUB_AVAILABLE:
        _dlog("github_app_integration_pygithub_missing")
        raise RuntimeError("PyGithub not installed on server.")
    app_auth = _get_app_auth()
    integration = GithubIntegration(auth=app_auth)
    _dlog("github_app_integration_ok")
    return integration


def get_installation_account_info(installation_id: str) -> dict:
    """Fetch the account (user/org) an installation belongs to, plus repo
    selection info. Used right after the install callback to record a
    human-readable label for the connection. Degrades to a minimal dict on
    any failure — never raises, since this is best-effort display data."""
    try:
        integration = get_integration()
        installation = integration.get_app_installation(int(installation_id))
        account_login = getattr(installation.raw_data.get("account", {}), "get", lambda *_: None)("login") \
            if isinstance(installation.raw_data.get("account"), dict) else None
        repo_selection = installation.raw_data.get("repository_selection", "selected")
        info = {
            "installation_id": str(installation_id),
            "account_login": account_login or "unknown",
            "repository_selection": repo_selection,
        }
        _dlog("github_app_installation_info_ok", **info)
        return info
    except Exception as e:
        _dlog("github_app_installation_info_failed", installation_id=installation_id, error=str(e))
        return {"installation_id": str(installation_id), "account_login": "unknown", "repository_selection": "unknown"}
