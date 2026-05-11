"""Authentication for the Intent Signals Console.

Supports two modes, selected automatically by what's in secrets.toml:

1. **Dev mode** — plaintext credentials in `[dev.users]`. For local testing
   without standing up Supabase. A warning banner appears on the login screen
   whenever dev mode is active, so it's obvious if you leave it on.

2. **Supabase Auth** — managed password-based authentication via `[supabase]`.
   This is the production path. Users live in your Supabase project's
   auth.users table; no plaintext credentials anywhere.

If `[dev.users]` is present and non-empty, dev mode wins (even if
`[supabase]` is also set). Remove `[dev.users]` to switch to Supabase.

# secrets.toml schema

    # Dev mode (local testing):
    [dev.users]
    "sales@rto.com.au"  = "Password123"
    "booker@rto.com.au" = "Password123"

    # Supabase mode (production):
    [supabase]
    url      = "https://your-project-ref.supabase.co"
    anon_key = "your-anon-public-key"

The `anon_key` is the correct one for client-side auth flows. DO NOT use the
`service_role` key — it has admin rights.

# First-time Supabase setup

1. Create a project at https://supabase.com. For an Australian RTO, pick
   the Sydney region (ap-southeast-2). You CANNOT change region later.
2. Settings > Data API > copy the Project URL and the "anon public" API key
   into .streamlit/secrets.toml under [supabase].
3. Authentication > Providers > Email: toggle "Confirm email" OFF for an
   internal admin-managed tool — otherwise manually-created users can't
   log in until they click a confirmation email they won't receive.
4. Authentication > Users > Add user. Set email + password.

# Session handling

Streamlit reruns the whole script on every interaction. The Supabase client
holds auth state internally, so we keep one client per Streamlit session
(each browser tab gets its own) and re-attach the user's tokens at the top
of every rerun via `client.auth.set_session(...)`. The SDK auto-refreshes
the access token transparently as long as the refresh token is still valid.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

import streamlit as st


# Session keys we set on login and clear on logout.
_AUTH_KEYS = (
    "authenticated",
    "username",
    "user_id",
    "access_token",
    "refresh_token",
    "auth_mode",
)

# App-level keys also cleared on logout so one user's state doesn't leak
# into the next person who signs in on the same browser session.
_APP_KEYS = (
    "signals",
    "selected_signal",
    "selected_signal_idx",
    "sequence",
    "compliance_result",
    "rewrite_count",
)


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def _dev_users() -> Dict[str, str]:
    """Return the [dev.users] mapping if present and non-empty. Empty dict
    means dev mode is off."""
    try:
        users = st.secrets.get("dev", {}).get("users", {})
        return dict(users) if users else {}
    except Exception:
        return {}


def _is_dev_mode() -> bool:
    return bool(_dev_users())


# ---------------------------------------------------------------------------
# Supabase client (only used in production mode)
# ---------------------------------------------------------------------------


def _get_client():
    """Return the Supabase client for this Streamlit session. Lazily imported
    so dev mode works even if the supabase package isn't installed."""
    if "_supabase_client" not in st.session_state:
        try:
            url = st.secrets["supabase"]["url"]
            key = st.secrets["supabase"]["anon_key"]
        except (KeyError, FileNotFoundError) as e:
            raise RuntimeError(
                "Missing Supabase config. Add [supabase] url and anon_key to "
                ".streamlit/secrets.toml, or use dev mode via [dev.users]. "
                "See auth.py docstring."
            ) from e
        from supabase import create_client  # lazy import
        st.session_state._supabase_client = create_client(url, key)
    return st.session_state._supabase_client


def _restore_supabase_session(client) -> bool:
    """Re-attach tokens from session_state to the client. Returns True if
    the session is still valid (auto-refresh succeeded if needed)."""
    access = st.session_state.get("access_token")
    refresh = st.session_state.get("refresh_token")
    if not access or not refresh:
        return False
    try:
        client.auth.set_session(access, refresh)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _clear_session() -> None:
    """Clear all auth + app session state. Used on logout and on detected
    session expiry."""
    for k in _AUTH_KEYS + _APP_KEYS:
        st.session_state.pop(k, None)


def _set_authenticated(
    mode: str,
    username: str,
    user_id: str,
    access_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> None:
    """Set the session_state keys that signal authentication. Same shape
    for both modes so app.py doesn't have to care."""
    st.session_state.authenticated = True
    st.session_state.auth_mode = mode
    st.session_state.username = username
    st.session_state.user_id = user_id
    if access_token:
        st.session_state.access_token = access_token
    if refresh_token:
        st.session_state.refresh_token = refresh_token


# ---------------------------------------------------------------------------
# Login form
# ---------------------------------------------------------------------------


def login_form() -> bool:
    """Render a login form. Returns True if the user is authenticated."""
    dev_mode = _is_dev_mode()

    # Already authenticated — verify and short-circuit.
    if st.session_state.get("authenticated"):
        if dev_mode and st.session_state.get("auth_mode") == "dev":
            return True
        if not dev_mode and st.session_state.get("auth_mode") == "supabase":
            client = _get_client()
            if _restore_supabase_session(client):
                return True
        # Mode changed (e.g. dev section was removed) or session expired —
        # force re-login.
        _clear_session()

    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown("## ABC Training")
        st.caption("Intent Signals Console · RTO #5800")

        if dev_mode:
            st.warning(
                "⚠️ **Dev mode active.** Using plaintext credentials from "
                "`[dev.users]` in secrets.toml. Remove that section before "
                "deploying publicly."
            )

        st.divider()

        with st.form("login", clear_on_submit=False):
            email = st.text_input("Email", autocomplete="email")
            password = st.text_input(
                "Password", type="password", autocomplete="current-password"
            )
            submitted = st.form_submit_button(
                "Sign in", type="primary", use_container_width=True
            )

        if submitted:
            if dev_mode:
                _handle_dev_login(email, password)
            else:
                _handle_supabase_login(email, password)

    return False


def _handle_dev_login(email: str, password: str) -> None:
    """Plaintext credential check against [dev.users]."""
    users = _dev_users()
    expected = users.get(email or "")
    if expected is not None and expected == password:
        _set_authenticated(
            mode="dev",
            username=email,
            user_id=f"dev:{email}",
        )
        st.rerun()
    else:
        st.error("Invalid credentials")


def _handle_supabase_login(email: str, password: str) -> None:
    """Sign in against Supabase Auth, with a constant-time floor on the
    response to make username-enumeration via timing harder."""
    client = _get_client()
    t0 = time.monotonic()
    resp = None
    try:
        resp = client.auth.sign_in_with_password(
            {"email": email or "", "password": password or ""}
        )
    except Exception:
        # supabase-py raises AuthApiError for bad creds and various other
        # failure modes. All become "invalid credentials" in the UI — don't
        # leak which field was wrong, or whether the email is registered.
        resp = None

    elapsed = time.monotonic() - t0
    if elapsed < 0.4:
        time.sleep(0.4 - elapsed)

    if resp is not None and resp.session is not None and resp.user is not None:
        _set_authenticated(
            mode="supabase",
            username=resp.user.email or email,
            user_id=resp.user.id,
            access_token=resp.session.access_token,
            refresh_token=resp.session.refresh_token,
        )
        st.rerun()
    else:
        st.error("Invalid credentials")


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def logout_button() -> None:
    """Render a sign-out button in the sidebar."""
    if st.sidebar.button("Sign out", use_container_width=True):
        # In Supabase mode, tell the server to revoke the refresh token.
        # In dev mode, nothing to revoke server-side — just clear local state.
        if st.session_state.get("auth_mode") == "supabase":
            try:
                client = _get_client()
                client.auth.sign_out()
            except Exception:
                # If the remote sign-out fails (network, already-expired
                # session), still clear local state. User is logged out
                # client-side regardless.
                pass
        _clear_session()
        st.session_state.pop("_supabase_client", None)
        st.rerun()


# ---------------------------------------------------------------------------
# Helpers other modules can use
# ---------------------------------------------------------------------------


def current_user_id() -> Optional[str]:
    """Return a stable ID for the signed-in user, or None.

    For Supabase users this is the auth.users UUID. For dev users it's
    `dev:<email>`. Useful when storing generated sequences per-user in
    Postgres later.
    """
    return st.session_state.get("user_id")
