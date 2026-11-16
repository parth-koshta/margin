"""Read-only Gmail access with your own Google sign-in. No AI connector involved.

Setup (once): create a Google Cloud OAuth client of type "Desktop app", then run
`margin setup-gmail path/to/client_secret.json`. The browser asks you to approve
read-only access; the resulting token is stored in the macOS Keychain.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import keychain

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"

K_CLIENT_ID, K_CLIENT_SECRET, K_REFRESH = "gmail_client_id", "gmail_client_secret", "gmail_refresh_token"

# Only fetch mail that could be a money movement or a card statement. Kept tight on purpose: Gmail's
# per-minute quota makes every fetched email cost time. Checked against a year of mail: these words
# matched every bank-alert email the broader search found, at a sixth of the volume.
SEARCH = ("(debited OR credited OR spent OR \"has been used\" OR withdrawn OR \"a/c\" OR \"amount due\" "
          "OR \"total due\" OR \"minimum due\" OR salary OR refund OR payout) "
          "-category:promotions -category:social -in:sent -in:chats")
# US banks word their alerts differently ("You made a $45.00 transaction with ...", "A deposit of ...", "Your
# available balance is ..."). Phrases only banks use are searched from anyone; the common words (transaction,
# payment, purchase...) only from US banks' own domains and the senders you trusted, so shop receipts and
# newsletters don't flood the sync. Used when your home currency isn't the rupee; in India the search is SEARCH.
US_PHRASES = ('"available balance" OR "current balance" OR "statement balance" OR "minimum payment" OR '
              '"direct deposit" OR "card ending" OR "account ending"')
US_WORDS = "transaction OR transactions OR purchase OR charge OR charged OR payment OR deposit OR withdrawal OR balance"


def search_query(settings: dict | None = None) -> str:
    """The Gmail search for bank alerts: SEARCH in India; outside it, SEARCH plus US banks' wording."""
    from . import money, parse   # noqa: PLC0415
    settings = settings or {}
    if money.home(settings) == "INR":
        return SEARCH
    domains = [d for d, _ in parse.US_DOMAINS]
    domains += [t for t in settings.get("trusted_senders") or [] if "." in t and " " not in t and "@" not in t]
    senders = " ".join(f"from:{d}" for d in dict.fromkeys(domains))
    words = SEARCH.split(") ", 1)[0] + ")"
    rest = SEARCH.split(") ", 1)[1]
    return f"({words} OR ({US_PHRASES}) OR (({US_WORDS}) {{{senders}}})) {rest}"


class AuthError(RuntimeError):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise AuthError(f"Google token endpoint said {e.code}: {body[:300]}") from None


def setup(client_secret_path: str) -> str:
    """Interactive one-time consent. Returns the Gmail address that was connected."""
    with open(os.path.expanduser(client_secret_path)) as f:
        cfg = json.load(f)
    c = cfg.get("installed") or cfg.get("web") or {}
    client_id, client_secret = c.get("client_id"), c.get("client_secret")
    if not client_id:
        raise SystemExit("That file isn't a Google OAuth client JSON (no client_id). Pick the 'Desktop app' download.")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result["code"] = q.get("code", [None])[0]
                result["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Connected. You can close this tab." if result.get("code") else "Not connected. Check the terminal."
            self.wfile.write(f"<p style='font:16px system-ui;margin:3em'>{msg}</p>".encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Opening your browser to approve read-only Gmail access...")
    print(f"If it doesn't open, paste this into your browser:\n{url}\n")
    webbrowser.open(url)
    deadline = time.time() + 300
    server.timeout = 5
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if not result.get("code"):
        raise SystemExit(f"Gmail approval didn't finish ({result.get('error') or 'timed out'}).")

    tok = _post(TOKEN_URL, {
        "code": result["code"], "client_id": client_id, "client_secret": client_secret or "",
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if "refresh_token" not in tok:
        raise SystemExit("Google didn't return a long-lived token. Remove the app's access at "
                         "myaccount.google.com/permissions and run setup again.")
    keychain.set_secret(K_CLIENT_ID, client_id)
    keychain.set_secret(K_CLIENT_SECRET, client_secret or "")
    keychain.set_secret(K_REFRESH, tok["refresh_token"])
    profile = _get(f"{API}/profile", tok["access_token"])
    return profile.get("emailAddress", "?")


def is_configured() -> bool:
    return bool(keychain.get_secret(K_REFRESH))


def access_token() -> str:
    refresh = keychain.get_secret(K_REFRESH)
    if not refresh:
        raise AuthError("Gmail isn't connected yet. Run margin setup-gmail <client_secret.json>.")
    tok = _post(TOKEN_URL, {
        "client_id": keychain.get_secret(K_CLIENT_ID) or "",
        "client_secret": keychain.get_secret(K_CLIENT_SECRET) or "",
        "refresh_token": refresh, "grant_type": "refresh_token",
    })
    return tok["access_token"]


_HARD_403 = ("insufficientPermissions", "forbidden", "domainPolicy", "authError")


class _Pace:
    """One request rate shared by all threads. Gmail allows a fixed quota per user per minute; when we
    hit it, everyone waits for the next minute and the rate drops, then creeps back up while all is well."""

    def __init__(self, rate: float = 8.0, top: float = 15.0):
        self._lock = threading.Lock()
        self.rate, self.top = rate, top
        self._next = self._pause_until = 0.0
        self._ok = 0

    def wait(self):
        with self._lock:
            now = time.time()
            at = max(now, self._next, self._pause_until)
            self._next = at + 1 / self.rate
        if at > now:
            time.sleep(at - now)

    def ok(self):
        with self._lock:
            self._ok += 1
            if self._ok % 100 == 0:
                self.rate = min(self.top, self.rate + 0.5)

    def limited(self, window_start: float = 0):
        with self._lock:
            now = time.time()
            resume = window_start + 61 if window_start else now + 20
            self._pause_until = max(self._pause_until, min(resume, now + 65), now + 2)
            if now - getattr(self, "_cut_at", 0) > 30:   # one cut per limit event, not one per thread
                self.rate, self._cut_at = max(1.0, self.rate * 0.7), now


_PACE = _Pace()


def _get(url: str, token: str, tries: int = 8) -> dict:
    for attempt in range(tries):
        _PACE.wait()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read())
            _PACE.ok()
            return out
        except urllib.error.HTTPError as e:
            # Gmail reports rate limits as 429 or as 403 (rateLimitExceeded, userRateLimitExceeded, ...).
            reason, window = "", 0.0
            try:
                err = json.loads(e.read()).get("error", {})
                reason = ",".join(x.get("reason", "") for x in err.get("errors", [])) or err.get("status", "")
                for d in err.get("details", []):
                    window = float(d.get("metadata", {}).get("window_start_time", 0) or 0) or window
            except (ValueError, AttributeError):
                pass
            limited = e.code == 429 or (e.code == 403 and "ateLimit" in reason)
            retry = limited or e.code in (500, 502, 503, 504) or (
                e.code == 403 and not any(h in reason for h in _HARD_403))
            if retry and attempt < tries - 1:
                if limited:
                    _PACE.limited(window)
                else:
                    time.sleep(min(60, 2 ** attempt) + secrets.randbelow(1000) / 1000)
                continue
            raise AuthError(f"Gmail API said {e.code} {reason} for {url.split('?')[0]}".replace("  ", " "),
                            e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < tries - 1:
                time.sleep(min(60, 2 ** attempt))
                continue
            raise AuthError(f"Can't reach Gmail ({e})", 0) from None
    return {}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "td", "h1", "h2", "h3"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s)
    return html.unescape(" ".join(p.out))


def _body_text(payload: dict) -> str:
    plain, htm = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            txt = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
            (plain if mime == "text/plain" else htm if mime == "text/html" else []).append(txt)
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    if plain and sum(len(p.strip()) for p in plain) > 40:
        return "\n".join(plain)
    return "\n".join(_html_to_text(h) for h in htm) or "\n".join(plain)


class _Token:
    """Google access tokens last an hour; a long first sync needs fresh ones. Shared by worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._renew()

    def _renew(self):
        self.value, self.at = access_token(), time.time()

    def get(self) -> str:
        with self._lock:
            if time.time() - self.at > 40 * 60:
                self._renew()
            return self.value

    def refresh(self, stale: str) -> str:
        with self._lock:
            if self.value == stale:
                self._renew()
            return self.value


def _api(url: str, token: _Token) -> dict:
    t = token.get()
    try:
        return _get(url, t)
    except AuthError as e:
        if e.code != 401:
            raise
        return _get(url, token.refresh(t))


_SKIP = object()


def _fetch_one(mid: str, token: _Token):
    try:
        msg = _api(f"{API}/messages/{mid}?format=full", token)
    except AuthError as e:
        if e.code == 404:   # deleted since it was listed
            return None
        if e.code in (403, 429, 500, 502, 503, 504, 0):   # still limited after retries: try next sync
            return _SKIP
        raise
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": mid,
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "ts_epoch": int(msg.get("internalDate", "0")) / 1000,
        "text": _body_text(msg.get("payload", {})),
    }


def fetch_since(after_epoch: int, seen: set[str], progress=None, workers: int = 4, stats: dict | None = None,
                query: str = SEARCH):
    """Yield dicts {id, sender, subject, ts_epoch, text} for new matching messages.

    Lists matching ids first (cheap) so the total is known, then fetches bodies a few at a time.
    progress(done, total) is called as messages arrive. Messages Google keeps refusing are skipped
    and counted in stats["skipped"]; the caller must then not advance its cursor, so they're retried.
    """
    token = _Token()
    q = f"{query} after:{int(after_epoch)}"
    ids, page = [], None
    while True:
        params = {"q": q, "maxResults": 500}
        if page:
            params["pageToken"] = page
        listing = _api(f"{API}/messages?{urllib.parse.urlencode(params)}", token)
        ids += [m["id"] for m in listing.get("messages", []) if f"email:{m['id']}" not in seen]
        page = listing.get("nextPageToken")
        if not page:
            break
    yield from fetch_ids(ids, progress, workers, stats, token)


def fetch_ids(ids: list[str], progress=None, workers: int = 4, stats: dict | None = None, token=None):
    """Fetch these message ids. Ones Google keeps refusing go to stats["skipped_ids"] (retry later)."""
    token = token or _Token()
    stats = stats if stats is not None else {}
    stats.setdefault("skipped", 0)
    stats.setdefault("skipped_ids", [])
    in_a_row, total = 0, len(ids)
    if progress:
        progress(0, total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, total, 200):   # bounded batches keep memory flat
            batch = ids[start:start + 200]
            for i, msg in enumerate(pool.map(lambda mid: _fetch_one(mid, token), batch)):
                if msg is _SKIP:
                    stats["skipped"] += 1
                    stats["skipped_ids"].append(batch[i])
                    in_a_row += 1
                    if in_a_row >= 25:
                        raise AuthError(f"Gmail kept refusing requests ({stats['skipped']} emails); "
                                        "the next sync continues where this one stopped.", 429)
                    msg = None
                else:
                    in_a_row = 0
                if msg:
                    yield msg
                if progress:
                    progress(start + i + 1, total)


# Statements (CDSL CAS, INDmoney, NPS): a search, the message, and its PDF attachments.
def search(q: str, token: _Token | None = None, limit: int = 500) -> list[str]:
    """Ids of messages matching a Gmail search, newest first."""
    token = token or _Token()
    ids, page = [], None
    while len(ids) < limit:
        params = {"q": q, "maxResults": min(500, limit - len(ids))}
        if page:
            params["pageToken"] = page
        listing = _api(f"{API}/messages?{urllib.parse.urlencode(params)}", token)
        ids += [m["id"] for m in listing.get("messages", [])]
        page = listing.get("nextPageToken")
        if not page:
            break
    return ids


def message(mid: str, token: _Token | None = None) -> dict:
    """{id, sender, subject, ts_epoch, text, files: [(filename, attachment_id or inline bytes)]}."""
    token = token or _Token()
    msg = _api(f"{API}/messages/{mid}?format=full", token)
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    files = []

    def walk(part):
        if part.get("filename"):
            body = part.get("body", {})
            files.append((part["filename"], body.get("attachmentId") or
                          base64.urlsafe_b64decode((body.get("data") or "") + "==")))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return {"id": mid, "sender": headers.get("from", ""), "subject": headers.get("subject", ""),
            "ts_epoch": int(msg.get("internalDate", "0")) / 1000, "text": _body_text(payload), "files": files}


def attachment(mid: str, ref, token: _Token | None = None) -> bytes:
    """An attachment's bytes (ref is what message() put in files)."""
    if isinstance(ref, bytes):
        return ref
    token = token or _Token()
    data = _api(f"{API}/messages/{mid}/attachments/{ref}", token)["data"]
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
