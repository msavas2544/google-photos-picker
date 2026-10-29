"""OAuth 2.0 helpers for the Google Photos Picker API.

Three ways to authenticate:

* :class:`OAuthCredentials` – user tokens (access + refresh) with automatic refresh.
  Obtain them with :class:`OAuthFlow` (web apps) or :meth:`OAuthFlow.run_local_server`
  (scripts / CLI).
* :class:`StaticToken` – you manage the access token yourself.
* :class:`GoogleAuthCredentials` – wrap any ``google.auth`` credentials object
  (e.g. results from ``google-auth-oauthlib``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import httpx

from .errors import AuthError

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

#: Refresh this many seconds before the token actually expires.
EXPIRY_SKEW = 60


class Credentials:
    """Interface used by the clients. Subclass to plug in your own token source."""

    def token(self, http: httpx.Client) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    async def atoken(self, http: httpx.AsyncClient) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def invalidate(self, failed_token: Optional[str] = None) -> None:
        """Called after a 401 so the next :meth:`token` call refreshes.

        ``failed_token`` is the token that was rejected; implementations should
        ignore the call if they have already moved on to a newer token."""


class StaticToken(Credentials):
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def token(self, http: httpx.Client) -> str:
        return self.access_token

    async def atoken(self, http: httpx.AsyncClient) -> str:
        return self.access_token


@dataclass
class OAuthCredentials(Credentials):
    """User OAuth tokens with transparent refresh.

    ``on_refresh`` is called with the credentials after every successful refresh –
    use it to persist the new token (Google may rotate refresh tokens).
    """

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    expires_at: Optional[float] = None
    scopes: Tuple[str, ...] = ()
    token_uri: str = TOKEN_URI
    on_refresh: Optional[Callable[[OAuthCredentials], None]] = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._alock: Optional[asyncio.Lock] = None
        self._alock_loop: Any = None

    # -- state ---------------------------------------------------------------
    @property
    def expired(self) -> bool:
        if not self.access_token:
            return True
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - EXPIRY_SKEW

    def invalidate(self, failed_token: Optional[str] = None) -> None:
        with self._lock:
            if failed_token is None or failed_token == self.access_token:
                self.expires_at = 0

    def _refresh_request(self) -> Dict[str, str]:
        if not (self.refresh_token and self.client_id):
            raise AuthError("access token expired and no refresh_token/client_id available")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        return data

    def _apply(self, payload: Dict[str, Any]) -> None:
        self.access_token = payload["access_token"]
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        if payload.get("expires_in") is not None:
            self.expires_at = time.time() + float(payload["expires_in"])
        if payload.get("scope"):
            self.scopes = tuple(payload["scope"].split())
        if self.on_refresh:
            self.on_refresh(self)

    # -- sync ----------------------------------------------------------------
    def refresh(self, http: Optional[httpx.Client] = None) -> None:
        own = http is None
        http = http or httpx.Client(timeout=30)
        try:
            resp = http.post(self.token_uri, data=self._refresh_request())
        finally:
            if own:
                http.close()
        self._apply(_token_payload(resp))

    def token(self, http: httpx.Client) -> str:
        with self._lock:
            if self.expired:
                self.refresh(http)
            assert self.access_token
            return self.access_token

    # -- async ---------------------------------------------------------------
    async def arefresh(self, http: Optional[httpx.AsyncClient] = None) -> None:
        own = http is None
        http = http or httpx.AsyncClient(timeout=30)
        try:
            resp = await http.post(self.token_uri, data=self._refresh_request())
        finally:
            if own:
                await http.aclose()
        self._apply(_token_payload(resp))

    async def atoken(self, http: httpx.AsyncClient) -> str:
        loop = asyncio.get_running_loop()
        if self._alock is None or self._alock_loop is not loop:
            self._alock, self._alock_loop = asyncio.Lock(), loop
        async with self._alock:
            if self.expired:
                await self.arefresh(http)
            assert self.access_token
            return self.access_token

    # -- persistence -----------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "token_uri": self.token_uri,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], **kwargs: Any) -> OAuthCredentials:
        known = {k: data.get(k) for k in (
            "access_token", "refresh_token", "client_id", "client_secret", "expires_at")}
        return cls(
            **known,  # type: ignore[arg-type]
            scopes=tuple(data.get("scopes") or ()),
            token_uri=data.get("token_uri") or TOKEN_URI,
            **kwargs,
        )

    def save(self, path: Union[str, Path]) -> None:
        """Write the token to ``path`` with 0600 permissions."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.to_dict(), fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)  # atomic: readers never see a half-written token
        finally:
            if tmp.exists():
                tmp.unlink()

    @classmethod
    def load(cls, path: Union[str, Path], *, autosave: bool = True) -> OAuthCredentials:
        """Load from ``path``; with ``autosave`` refreshed tokens are written back."""
        data = json.loads(Path(path).read_text())
        creds = cls.from_dict(data)
        if autosave:
            creds.on_refresh = lambda c: c.save(path)
        return creds


class GoogleAuthCredentials(Credentials):
    """Adapter for ``google.auth.credentials.Credentials`` (requires ``google-auth``)."""

    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials
        self._lock = threading.Lock()

    def _ensure(self) -> str:
        from google.auth.transport.requests import Request  # type: ignore

        with self._lock:
            if not self.credentials.valid:
                self.credentials.refresh(Request())
            return str(self.credentials.token)

    def token(self, http: httpx.Client) -> str:
        return self._ensure()

    async def atoken(self, http: httpx.AsyncClient) -> str:
        return await asyncio.get_running_loop().run_in_executor(None, self._ensure)

    def invalidate(self, failed_token: Optional[str] = None) -> None:
        try:
            if failed_token is None or failed_token == self.credentials.token:
                self.credentials.token = None
        except Exception:  # pragma: no cover - defensive
            pass


def load_client_secrets(path: Union[str, Path]) -> Tuple[str, Optional[str]]:
    """Read ``(client_id, client_secret)`` from a ``client_secret_*.json`` file as
    downloaded from the Google Cloud console (``installed`` or ``web`` clients)."""
    data = json.loads(Path(path).read_text())
    for key in ("installed", "web"):
        if key in data:
            data = data[key]
            break
    cid = data.get("client_id")
    if not cid:
        raise AuthError(f"{path}: no client_id found (is this a client_secret JSON file?)")
    return str(cid), data.get("client_secret") or None


def _token_payload(resp: httpx.Response) -> Dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": resp.text}
    if resp.status_code >= 400 or "access_token" not in payload:
        err = payload.get("error_description") or payload.get("error") or resp.text
        raise AuthError(f"token endpoint returned {resp.status_code}: {err}")
    return dict(payload)


def _pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


@dataclass
class AuthorizationRequest:
    url: str
    state: str
    code_verifier: Optional[str]


class OAuthFlow:
    """Authorization-code flow (with PKCE) against Google's OAuth 2.0 endpoints."""

    def __init__(
        self,
        client_id: str,
        client_secret: Optional[str],
        scopes: Sequence[str],
        redirect_uri: str = "http://127.0.0.1",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = tuple(scopes)
        self.redirect_uri = redirect_uri

    def authorization_url(
        self,
        *,
        state: Optional[str] = None,
        pkce: bool = True,
        offline: bool = True,
        prompt: Optional[str] = "consent",
        login_hint: Optional[str] = None,
        include_granted_scopes: bool = True,
    ) -> AuthorizationRequest:
        state = state or secrets.token_urlsafe(24)
        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
        }
        if offline:
            params["access_type"] = "offline"
        if prompt:
            params["prompt"] = prompt
        if login_hint:
            params["login_hint"] = login_hint
        if include_granted_scopes:
            params["include_granted_scopes"] = "true"
        verifier = None
        if pkce:
            verifier, challenge = _pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        return AuthorizationRequest(f"{AUTH_URI}?{urllib.parse.urlencode(params)}", state, verifier)

    def _exchange_data(self, code: str, code_verifier: Optional[str]) -> Dict[str, str]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        if code_verifier:
            data["code_verifier"] = code_verifier
        return data

    def _creds(self, payload: Dict[str, Any]) -> OAuthCredentials:
        creds = OAuthCredentials(client_id=self.client_id, client_secret=self.client_secret,
                                 scopes=self.scopes)
        creds._apply(payload)
        return creds

    def exchange_code(
        self, code: str, code_verifier: Optional[str] = None, *, http: Optional[httpx.Client] = None
    ) -> OAuthCredentials:
        own = http is None
        http = http or httpx.Client(timeout=30)
        try:
            resp = http.post(TOKEN_URI, data=self._exchange_data(code, code_verifier))
        finally:
            if own:
                http.close()
        return self._creds(_token_payload(resp))

    async def aexchange_code(
        self, code: str, code_verifier: Optional[str] = None, *,
        http: Optional[httpx.AsyncClient] = None,
    ) -> OAuthCredentials:
        own = http is None
        http = http or httpx.AsyncClient(timeout=30)
        try:
            resp = await http.post(TOKEN_URI, data=self._exchange_data(code, code_verifier))
        finally:
            if own:
                await http.aclose()
        return self._creds(_token_payload(resp))

    def run_local_server(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
        timeout: float = 300,
        print_fn: Callable[[str], None] = print,
    ) -> OAuthCredentials:
        """Installed-app flow: start a loopback server, open the consent page, wait."""
        import http.server
        import webbrowser

        result: Dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                if "code" in q or "error" in q:
                    result.update(q)
                    body = b"Authorization complete. You can close this window."
                    self.send_response(200)
                else:
                    body = b"Waiting for authorization..."
                    self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        server = http.server.HTTPServer((host, port), Handler)
        server.timeout = 1
        self.redirect_uri = f"http://{host}:{server.server_address[1]}"
        req = self.authorization_url()
        print_fn(f"Open this URL to authorize:\n\n  {req.url}\n")
        if open_browser:
            try:
                webbrowser.open(req.url)
            except Exception:  # pragma: no cover
                pass
        deadline = time.time() + timeout
        try:
            while not result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not result:
            raise AuthError("timed out waiting for the OAuth redirect")
        if "error" in result:
            raise AuthError(f"authorization failed: {result['error']}")
        if result.get("state") != req.state:
            raise AuthError("OAuth state mismatch (possible CSRF)")
        return self.exchange_code(result["code"], req.code_verifier)


def revoke(token: str, *, http: Optional[httpx.Client] = None) -> None:
    """Revoke an access or refresh token."""
    own = http is None
    http = http or httpx.Client(timeout=30)
    try:
        resp = http.post(REVOKE_URI, data={"token": token})
    finally:
        if own:
            http.close()
    if resp.status_code >= 400:
        raise AuthError(f"revoke failed: {resp.status_code} {resp.text}")
