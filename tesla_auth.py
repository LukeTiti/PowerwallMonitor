"""Tesla's unofficial "Owner API" login (SSO + PKCE), implemented directly
against Tesla's own endpoints — no third-party Tesla client library.

This is the same login flow the Tesla mobile app itself uses. The actual
username/password entry happens on Tesla's real page (auth.tesla.com) in
your own browser — this module never sees your password, only the
authorization code Tesla hands back afterward.
"""

import base64
import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SSO_BASE = "https://auth.tesla.com"
OWNER_API_BASE = "https://owner-api.teslamotors.com"
CLIENT_ID = "ownerapi"
REDIRECT_URI = "tesla://auth/callback"
SCOPE = "openid email offline_access"

# Tesla's endpoints reject requests that don't look like a real client.
# The login page itself (auth.tesla.com in your browser) doesn't need this —
# these headers are only for the programmatic token/API calls below.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "X-Tesla-User-Agent": "TeslaApp/4.10.0",
}


def generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
    return verifier.decode(), challenge.decode()


def build_authorize_url(email: str) -> tuple[str, str, str]:
    """Returns (url_to_open_in_a_browser, code_verifier, state)."""
    verifier, challenge = generate_pkce()
    state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
    params = {
        "client_id": CLIENT_ID,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "login_hint": email,
    }
    return f"{SSO_BASE}/oauth2/v3/authorize?{urlencode(params)}", verifier, state


def parse_redirect(pasted_url: str) -> str:
    """Pull the authorization code out of the URL the browser tried (and
    failed) to redirect to after login. Raises ValueError if not found."""
    query = parse_qs(urlparse(pasted_url).query)
    if "code" not in query:
        raise ValueError(f"No 'code' parameter found in: {pasted_url!r}")
    return query["code"][0]


def exchange_code(code: str, verifier: str) -> dict:
    """Trade the one-time authorization code for access/refresh tokens."""
    resp = httpx.post(
        f"{SSO_BASE}/oauth2/v3/token",
        http2=True,
        headers=HEADERS,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
    )
    resp.raise_for_status()
    return _with_expiry(resp.json())


def refresh_tokens(refresh_token: str) -> dict:
    resp = httpx.post(
        f"{SSO_BASE}/oauth2/v3/token",
        http2=True,
        headers=HEADERS,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": SCOPE,
        },
    )
    resp.raise_for_status()
    return _with_expiry(resp.json())


def _with_expiry(tokens: dict) -> dict:
    tokens["obtained_at"] = time.time()
    tokens["expires_at"] = tokens["obtained_at"] + tokens["expires_in"]
    return tokens


def save_tokens(tokens: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(tokens, f)
    os.chmod(path, 0o600)


def load_tokens(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def get_valid_access_token(path: str) -> str:
    """Returns a live access token, transparently refreshing (and
    persisting the refreshed tokens) if the cached one is near expiry."""
    tokens = load_tokens(path)
    if time.time() > tokens["expires_at"] - 300:
        tokens = refresh_tokens(tokens["refresh_token"])
        save_tokens(tokens, path)
    return tokens["access_token"]
