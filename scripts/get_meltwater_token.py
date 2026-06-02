#!/usr/bin/env python3
"""Mint a Meltwater MCP access token (the Bearer "JWT") via OAuth 2.1 and write it to
.env as MELTWATER_MCP_JWT.

The Meltwater MCP is an OAuth-protected MCP server — the token is NOT a static value;
it's issued by the identity provider through an authorization-code + PKCE flow (with
dynamic client registration). This runs that flow with step-by-step logging:

  [1] Dynamic Client Registration (unless MELTWATER_OAUTH_CLIENT_ID is set).
  [2] Opens your browser to the authorize URL; you log in / consent.
  [3] Captures the redirect on http://localhost:<port>/callback
      (or paste the redirected URL manually if the browser can't reach it).
  [4] Exchanges the code (+ PKCE verifier) at the token endpoint.
  [5] Writes MELTWATER_MCP_JWT=<access_token> into .env (other lines preserved).

Run on a networked/VPN machine with a browser (stdlib only — certifi optional):

    python3 scripts/get_meltwater_token.py
    python3 scripts/get_meltwater_token.py --manual   # force manual paste of redirect URL

Env overrides (all optional; defaults below):
    MELTWATER_MCP_URL            resource the token is scoped to (RFC 8707)
    MELTWATER_OAUTH_AUTHORIZE    default https://app.meltwater.com/oauth/authorize
    MELTWATER_OAUTH_TOKEN        default https://authorize.meltwater.com/oauth/token
    MELTWATER_OAUTH_REGISTER     default https://app.meltwater.com/oauth/register
    MELTWATER_OAUTH_SCOPE        default "" (server default)
    MELTWATER_OAUTH_CLIENT_ID    skip DCR and use this client_id
    MELTWATER_OAUTH_CLIENT_SECRET optional (confidential client)
    MELTWATER_OAUTH_REDIRECT_PORT default 8765

Tokens expire — re-run when the in-app live call returns 401.
"""
from __future__ import annotations
import base64, hashlib, http.server, json, os, secrets, ssl, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser

# Use certifi's CA bundle on macOS where Python's stdlib SSL may not find system certs.
try:
    import certifi as _certifi
    if not os.environ.get("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = _certifi.where()
except ImportError:
    pass

_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _load_dotenv(path: str) -> None:
    """Minimal .env loader: set vars from .env without overriding the real environment."""
    if not os.path.exists(path):
        return
    for ln in open(path):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(_ENV_FILE)

AUTHORIZE = os.environ.get("MELTWATER_OAUTH_AUTHORIZE", "https://app.meltwater.com/oauth/authorize")
# Token endpoint lives on a different host than authorize (confirmed: app.../oauth/token
# 404s, authorize.../oauth/token answers). Override via MELTWATER_OAUTH_TOKEN if needed.
TOKEN = os.environ.get("MELTWATER_OAUTH_TOKEN", "https://authorize.meltwater.com/oauth/token")
REGISTER = os.environ.get("MELTWATER_OAUTH_REGISTER", "https://app.meltwater.com/oauth/register")
# authorize.meltwater.com is an Auth0 tenant: it uses `audience` (not RFC 8707 `resource`)
# to identify the target API, and standard OIDC scopes. offline_access => refresh token.
SCOPE = os.environ.get("MELTWATER_OAUTH_SCOPE", "openid profile email offline_access")
# The Auth0 API identifier for the MCP server. Find it in the MCP endpoint's
# WWW-Authenticate header / protected-resource metadata, then export it.
AUDIENCE = os.environ.get("MELTWATER_OAUTH_AUDIENCE", "")
# Legacy RFC 8707 resource param (kept for non-Auth0 servers); off by default now.
OMIT_RESOURCE = os.environ.get("MELTWATER_OAUTH_OMIT_RESOURCE", "1") not in ("", "0", "false")
RESOURCE = os.environ.get("MELTWATER_MCP_URL",
    "https://apim-apim-prod-ne-001.azure-api.net/meltwater-mcp-server-private-prod/v1/internal/mcp")
API_KEY = os.environ.get("MELTWATER_MCP_API_KEY", "")
PORT = int(os.environ.get("MELTWATER_OAUTH_REDIRECT_PORT", "8765"))
REDIRECT_URI = f"http://localhost:{PORT}/callback"
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _apikey_headers() -> dict:
    """Azure APIM subscription key — some endpoints behind the gateway require it."""
    return {"api-key": API_KEY} if API_KEY else {}


def _request(url: str, *, data: dict | None = None, json_body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict, str]:
    """One HTTP request with full diagnostics. On HTTP/network error, print the status
    and response BODY (where OAuth servers put the real error) and abort cleanly."""
    h = dict(headers or {})
    h.setdefault("Accept", "application/json")
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode(); h.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = urllib.parse.urlencode(data).encode(); h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        print(f"    -> HTTP {e.code} {e.reason} from {url}", file=sys.stderr)
        print(f"    -> response body: {raw[:1200]}", file=sys.stderr)
        raise SystemExit(f"FAILED at request to {url} (HTTP {e.code}). See body above.")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        hint = ""
        if isinstance(reason, ssl.SSLError):
            hint = "  (TLS/cert issue — `pip install certifi` or set SSL_CERT_FILE)"
        elif "Name or service not known" in str(reason) or "nodename" in str(reason):
            hint = "  (DNS — are you on the Meltwater VPN?)"
        print(f"    -> network error to {url}: {reason}{hint}", file=sys.stderr)
        raise SystemExit(f"FAILED to reach {url}.")


def _register_client() -> tuple[str, str | None]:
    cid = os.environ.get("MELTWATER_OAUTH_CLIENT_ID")
    if cid:
        print(f"[1] Using MELTWATER_OAUTH_CLIENT_ID={cid} (skipping registration)")
        return cid, os.environ.get("MELTWATER_OAUTH_CLIENT_SECRET")
    print(f"[1] Dynamic client registration -> POST {REGISTER}")
    # NOTE: no "scope" here — Auth0 DCR rejects it ("scope is not allowed").
    # Scope is requested on the authorize/token calls instead.
    payload = {
        "client_name": "battlecard-generator",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    _st, _hd, raw = _request(REGISTER, json_body=payload, headers=_apikey_headers())
    reg = json.loads(raw)
    cid = reg.get("client_id")
    if not cid:
        raise SystemExit(f"    -> registration returned no client_id: {raw[:500]}")
    secret = reg.get("client_secret")
    print(f"    -> registered client_id={cid}")
    print(f"    -> client_secret issued: {'YES' if secret else 'no'}; "
          f"token_endpoint_auth_method={reg.get('token_endpoint_auth_method', 'unspecified')}")
    return cid, secret


class _Catcher(http.server.BaseHTTPRequestHandler):
    code = None
    state = None
    error = None
    def do_GET(self):  # noqa: N802
        q = urllib.parse.urlparse(self.path)
        if q.path != "/callback":
            self.send_response(404); self.end_headers(); return
        params = urllib.parse.parse_qs(q.query)
        _Catcher.code = (params.get("code") or [None])[0]
        _Catcher.state = (params.get("state") or [None])[0]
        _Catcher.error = (params.get("error") or [None])[0]
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        msg = b"Authorized. Return to the terminal." if _Catcher.code else b"Authorization failed; see terminal."
        self.wfile.write(b"<h2>" + msg + b"</h2>")
    def log_message(self, *a):  # silence default logging
        pass


def _capture_code(state: str, manual: bool) -> str:
    if manual:
        pasted = input("Paste the FULL redirected URL (or just ?code=...): ").strip()
        params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query or pasted.lstrip("?"))
        if params.get("error"):
            raise SystemExit(f"    -> authorize returned error: {params['error'][0]}")
        code = (params.get("code") or [None])[0]
        if not code:
            raise SystemExit("    -> no ?code= found in the pasted URL.")
        return code
    # local callback server: serve until we capture a code (handles favicon/preflight too)
    server = http.server.HTTPServer(("localhost", PORT), _Catcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[3] Waiting for redirect on {REDIRECT_URI} (up to 5 min)...")
    try:
        for _ in range(300):
            if _Catcher.code is not None or _Catcher.error is not None:
                break
            time.sleep(1)
    finally:
        server.shutdown()
    if _Catcher.error:
        raise SystemExit(f"    -> authorize returned error: {_Catcher.error}")
    if _Catcher.code is None:
        raise SystemExit("    -> timed out waiting for the redirect. Re-run with --manual "
                         "to paste the redirected URL by hand.")
    if _Catcher.state != state:
        raise SystemExit("    -> state mismatch (possible CSRF); aborting.")
    return _Catcher.code


def _write_env_token(token: str) -> None:
    lines, found = [], False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as fh:
            for ln in fh:
                if ln.startswith("MELTWATER_MCP_JWT="):
                    lines.append(f"MELTWATER_MCP_JWT={token}\n"); found = True
                else:
                    lines.append(ln)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"MELTWATER_MCP_JWT={token}\n")
    with open(ENV_PATH, "w") as fh:
        fh.writelines(lines)
    os.chmod(ENV_PATH, 0o600)


def main(argv: list[str]) -> int:
    manual = "--manual" in argv
    print(f"Config:\n  authorize = {AUTHORIZE}\n  token     = {TOKEN}\n  register  = {REGISTER}"
          f"\n  audience  = {AUDIENCE or '(NONE — likely required for Auth0!)'}"
          f"\n  scope     = {SCOPE or '(none)'}\n  resource  = {'(omitted)' if OMIT_RESOURCE else RESOURCE}"
          f"\n  redirect  = {REDIRECT_URI}\n")

    client_id, client_secret = _register_client()
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }
    if AUDIENCE:
        auth_params["audience"] = AUDIENCE
    if not OMIT_RESOURCE:
        auth_params["resource"] = RESOURCE
    if SCOPE:
        auth_params["scope"] = SCOPE
    auth_url = AUTHORIZE + "?" + urllib.parse.urlencode(auth_params)

    print(f"[2] Open this URL and authorize:\n    {auth_url}")
    if not manual:
        webbrowser.open(auth_url)

    code = _capture_code(state, manual)
    print(f"    -> got authorization code ({code[:8]}...)")

    print(f"[4] Exchanging code at token endpoint -> POST {TOKEN}")
    data = {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "client_id": client_id, "code_verifier": verifier,
    }
    if AUDIENCE:
        data["audience"] = AUDIENCE
    if not OMIT_RESOURCE:
        data["resource"] = RESOURCE
    headers = _apikey_headers()
    if client_secret:
        # client_secret_basic: most servers that issue a secret expect HTTP Basic auth
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
        print("    -> authenticating client via HTTP Basic (client_secret_basic)")
    if API_KEY:
        print("    -> sending api-key header (APIM subscription key)")
    _st, _hd, raw = _request(TOKEN, data=data, headers=headers)
    tok = json.loads(raw)
    access = tok.get("access_token")
    if not access:
        raise SystemExit(f"    -> no access_token in response: {raw[:500]}")

    _write_env_token(access)
    exp = tok.get("expires_in")
    print(f"[5] OK: wrote MELTWATER_MCP_JWT to {ENV_PATH}"
          + (f" (expires in {exp}s — re-run when it 401s)" if exp else ""))
    if tok.get("refresh_token"):
        print("    -> a refresh_token was issued (auto-refresh can be added later).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
