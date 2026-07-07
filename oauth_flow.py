"""
oauth_flow.py
=============
Google OAuth 2.0 callback flow for Streamlit Cloud.

How it works:
  1. User clicks "Connect Gmail" → we build the Google authorization URL
     with the Streamlit app URL as the redirect_uri.
  2. Google redirects back to the app with ?code=xxx&state=yyy in the URL.
  3. We detect the `code` param on load, exchange it for tokens, and store
     the credentials object in st.session_state["google_creds"].
  4. All engine / calendar calls pass that credentials object directly
     instead of reading from disk.

Requirements in Streamlit secrets (Settings → Secrets):
    GOOGLE_CLIENT_ID     = "...apps.googleusercontent.com"
    GOOGLE_CLIENT_SECRET = "GOCSPX-..."
    GOOGLE_REDIRECT_URI  = "https://your-app.streamlit.app"   ← no trailing /
    GEMINI_API_KEY       = "..."

In Google Cloud Console → OAuth 2.0 Client → Authorized redirect URIs add:
    https://your-app.streamlit.app
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

import streamlit as st

# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_secret(key: str, env_fallback: str | None = None) -> str | None:
    """Read a value from st.secrets, falling back to environment variable."""
    try:
        val = st.secrets.get(key)
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(env_fallback or key)


def get_client_id() -> str | None:
    return _get_secret("GOOGLE_CLIENT_ID")


def get_client_secret() -> str | None:
    return _get_secret("GOOGLE_CLIENT_SECRET")


def get_redirect_uri() -> str | None:
    return _get_secret("GOOGLE_REDIRECT_URI")


def is_oauth_configured() -> bool:
    """Return True if all required OAuth secrets are present."""
    return all([get_client_id(), get_client_secret(), get_redirect_uri()])


# ---------------------------------------------------------------------------
# Authorization URL builder
# ---------------------------------------------------------------------------

def build_auth_url() -> str:
    """Build the Google OAuth authorization URL and store a CSRF state token."""
    from google_auth_oauthlib.flow import Flow  # type: ignore

    client_config = {
        "web": {
            "client_id": get_client_id(),
            "client_secret": get_client_secret(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [get_redirect_uri()],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=get_redirect_uri(),
    )

    # Generate and store CSRF state token
    state = secrets.token_urlsafe(32)
    st.session_state["oauth_state"] = state

    auth_url, _ = flow.authorization_url(
        access_type="offline",       # get a refresh_token
        include_granted_scopes="true",
        prompt="consent",            # force consent so refresh_token is always returned
        state=state,
    )

    return auth_url


# ---------------------------------------------------------------------------
# Token exchange (called when ?code=... appears in the URL)
# ---------------------------------------------------------------------------

def exchange_code_for_token(code: str, state: str) -> bool:
    """Exchange the authorization code for credentials.

    Validates the CSRF state, exchanges the code, and stores the
    credentials in st.session_state["google_creds"] as a dict.

    Returns True on success, False on failure.
    """
    from google_auth_oauthlib.flow import Flow  # type: ignore

    # CSRF check
    expected_state = st.session_state.get("oauth_state", "")
    if state != expected_state:
        st.session_state["oauth_error"] = (
            "OAuth state mismatch — possible CSRF attack. Please try again."
        )
        return False

    client_config = {
        "web": {
            "client_id": get_client_id(),
            "client_secret": get_client_secret(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [get_redirect_uri()],
        }
    }

    try:
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=get_redirect_uri(),
            state=state,
        )
        # Build the full callback URL including the code and state params.
        # Streamlit gives us query_params as a dict.
        params = st.query_params
        # Reconstruct the full redirect URL the way google-auth expects it.
        redirect_uri = get_redirect_uri()
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{redirect_uri}/?{param_str}"

        flow.fetch_token(authorization_response=full_url)
        creds = flow.credentials

        # Store serialised credentials in session state
        st.session_state["google_creds"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else SCOPES,
        }
        st.session_state["oauth_state"] = None  # clear used state
        st.session_state["oauth_error"] = None
        return True

    except Exception as exc:  # noqa: BLE001
        st.session_state["oauth_error"] = f"Token exchange failed: {exc}"
        return False


# ---------------------------------------------------------------------------
# Credentials object builder (for use by engine.py / calendar_engine.py)
# ---------------------------------------------------------------------------

def get_credentials():
    """Return a valid google.oauth2.credentials.Credentials object from
    session state, refreshing if needed. Returns None if not logged in."""
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore

    creds_dict = st.session_state.get("google_creds")
    if not creds_dict:
        return None

    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        scopes=creds_dict.get("scopes", SCOPES),
    )

    # Refresh silently if expired
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Persist refreshed token back to session state
                st.session_state["google_creds"]["token"] = creds.token
            except Exception:
                # Refresh failed — user must log in again
                st.session_state["google_creds"] = None
                return None
        else:
            return None

    return creds


def is_authenticated() -> bool:
    """Return True if the user has a valid Google credential in session."""
    return get_credentials() is not None


def logout() -> None:
    """Clear all OAuth state from session."""
    for key in ("google_creds", "oauth_state", "oauth_error"):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Fetch user info (name + email) for display
# ---------------------------------------------------------------------------

def get_user_info() -> dict[str, str]:
    """Return {"name": ..., "email": ...} for the logged-in user, or {}."""
    creds = get_credentials()
    if not creds:
        return {}
    try:
        from googleapiclient.discovery import build  # type: ignore
        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = service.userinfo().get().execute()
        return {
            "name": info.get("name", ""),
            "email": info.get("email", ""),
            "picture": info.get("picture", ""),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main gate — call this at the TOP of app.py before anything else
# ---------------------------------------------------------------------------

def render_auth_gate() -> bool:
    """Render the login/callback UI.  Returns True if user is authenticated.

    Call this at the very top of app.py:

        from oauth_flow import render_auth_gate
        if not render_auth_gate():
            st.stop()

    The function handles three states:
      A) ?code=... in URL  → exchange code, then rerun clean
      B) authenticated     → return True (app proceeds)
      C) not authenticated → show login button, return False (app stops)
    """
    # ------------------------------------------------------------------ A
    # OAuth callback — code in URL query params
    # ------------------------------------------------------------------ A
    params = st.query_params
    code = params.get("code")
    state = params.get("state", "")

    if code:
        with st.spinner("Completing Google sign-in…"):
            success = exchange_code_for_token(code, state)
        # Clear the query params so the code isn't reused on next rerun
        st.query_params.clear()
        if success:
            st.rerun()
        else:
            err = st.session_state.get("oauth_error", "Unknown error")
            st.error(f"❌ Sign-in failed: {err}")
            if st.button("Try again"):
                st.rerun()
            return False

    # ------------------------------------------------------------------ B
    # Already authenticated
    # ------------------------------------------------------------------ B
    if is_authenticated():
        return True

    # ------------------------------------------------------------------ C
    # Not authenticated — show login page
    # ------------------------------------------------------------------ C
    _render_login_page()
    return False


def _render_login_page() -> None:
    """Render the full-page login UI."""
    # Center the login card
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("## ✍️ The Draft Desk")
        st.markdown(
            "Your AI-powered Gmail chief of staff.  \n"
            "Connect your Google account to get started."
        )
        st.divider()

        if not is_oauth_configured():
            st.error(
                "OAuth is not configured. Add `GOOGLE_CLIENT_ID`, "
                "`GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` to "
                "Streamlit secrets (Settings → Secrets)."
            )
            return

        err = st.session_state.get("oauth_error")
        if err:
            st.error(f"❌ {err}")
            st.session_state["oauth_error"] = None

        auth_url = build_auth_url()

        # Render a proper "Sign in with Google" button via HTML link
        st.markdown(
            f"""
            <a href="{auth_url}" target="_self" style="text-decoration:none;">
                <div style="
                    display: flex;
                    align-items: center;
                    gap: 12px;
                    background: #fff;
                    color: #3c4043;
                    border: 1px solid #dadce0;
                    border-radius: 4px;
                    padding: 10px 16px;
                    font-family: 'Google Sans', Roboto, Arial, sans-serif;
                    font-size: 15px;
                    font-weight: 500;
                    cursor: pointer;
                    width: fit-content;
                    box-shadow: 0 1px 3px rgba(0,0,0,.1);
                    margin: 8px auto;
                ">
                    <svg width="18" height="18" viewBox="0 0 18 18">
                        <path fill="#4285F4" d="M16.51 8H8.98v3h4.3c-.18 1-.74 1.48-1.6 2.04v2.01h2.6a7.8 7.8 0 0 0 2.38-5.88c0-.57-.05-.66-.15-1.18z"/>
                        <path fill="#34A853" d="M8.98 17c2.16 0 3.97-.72 5.3-1.94l-2.6-2a4.8 4.8 0 0 1-7.18-2.54H1.83v2.07A8 8 0 0 0 8.98 17z"/>
                        <path fill="#FBBC05" d="M4.5 10.52a4.8 4.8 0 0 1 0-3.04V5.41H1.83a8 8 0 0 0 0 7.18l2.67-2.07z"/>
                        <path fill="#EA4335" d="M8.98 4.18c1.17 0 2.23.4 3.06 1.2l2.3-2.3A8 8 0 0 0 1.83 5.4L4.5 7.49a4.77 4.77 0 0 1 4.48-3.3z"/>
                    </svg>
                    Sign in with Google
                </div>
            </a>
            """,
            unsafe_allow_html=True,
        )

        st.caption(
            "The app requests read access to your Gmail inbox, "
            "permission to send replies on your behalf, and calendar access."
        )
