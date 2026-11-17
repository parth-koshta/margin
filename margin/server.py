"""The Margin app: a small web server on this Mac only, at http://margin.localhost/.

- macOS (launchd, agent com.<you>.margin.web) listens on 127.0.0.1 and ::1 port 80 at all times and
  starts this server when a page is opened, so the address always works, after restarts and reboots
  too. Not reachable from other machines.
- Every request must carry the secret key (server.key): the Margin app hands it over once and the
  browser keeps it in a private cookie. The Host header must be one of this Mac's own names, so other
  websites open in the browser can't read or change anything.
- The process exits after IDLE_MINUTES without a page checking in; launchd starts it again on the
  next visit.

Without the launchd agent (`margin serve` by hand) it falls back to 127.0.0.1:47215.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ai, categorize, dashboard, db, icon, llm, lock, networth, paths, release, review, suggest, sync, tidy

IDLE_MINUTES = 20
HOSTNAME = "margin.localhost"                  # *.localhost always means this Mac: Chrome and macOS never ask the
                                               # network for it, and Chrome counts it as secure (no "Not Secure")
OLD_HOSTNAMES = tuple(sync.load_settings().get("old_hostnames", []))   # earlier addresses; they forward here
PORT = 47215                                   # only when not started by launchd
KEY_PATH = paths.DATA_DIR / "server.key"
COOKIE = "margin_key"
HOSTS = ("127.0.0.1", "localhost", "[::1]", HOSTNAME, *OLD_HOSTNAMES)


def _key() -> str:
    try:
        k = KEY_PATH.read_text().strip()
        if len(k) >= 24:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(24)
    KEY_PATH.write_text(k)
    os.chmod(KEY_PATH, 0o600)
    return k
INFO_PATH = paths.DATA_DIR / "server.json"


_PAGE_TOKENS = """:root{color-scheme:light;--bg:#f7f7f5;--surface:#ffffff;--line:#e9e9e5;--text:#121211;--text2:#4f4f4b;--muted:#6c6c67;
  --accent:#2a78d6;--accent-soft:rgba(42,120,214,.10);--neg:#c2392f}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0c0c0d;--surface:#151517;--line:#242427;--text:#f2f2f0;
  --text2:#b9b9b4;--muted:#8f8f8a;--accent:#4a8fe7;--accent-soft:rgba(74,143,231,.14);--neg:#f0675d}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;color:var(--text);-webkit-font-smoothing:antialiased;
  background:radial-gradient(900px 520px at 50% -8%,var(--accent-soft),transparent 70%),var(--bg)}"""

SIGN_IN_PAGE = ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Margin</title>""" + icon.HEAD_TAGS + "<style>" + _PAGE_TOKENS + """
body{display:flex;align-items:center;justify-content:center;padding:24px 20px}
main{max-width:440px;text-align:center}
.logo{width:52px;height:52px;margin:0 auto 22px;display:block}
h1{font:650 24px/1.25 -apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;letter-spacing:-.02em;margin:0 0 10px}
p{margin:0 0 10px;color:var(--muted)}
b{color:var(--text2);font-weight:600}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--surface);border:1px solid var(--line);padding:2px 6px;border-radius:6px;color:var(--text2)}
</style></head><body><main>""" + icon.svg().decode().replace("<svg ", '<svg class="logo" aria-hidden="true" ', 1) + """
<h1>Let this browser in once</h1>
<p>This browser hasn't been let in yet. Run <code>margin open</code> in Terminal once; it opens this page with its key.</p>
<p>After that, this address works on its own. Or skip the browser and use the <b>Margin</b> app.</p>
</main></body></html>""").encode()


LOCK_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Margin · locked</title><!--ICON-->
<style>
""" + _PAGE_TOKENS + """
body{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:48px 20px 28px}
.clock{user-select:none}
.time{font:200 clamp(64px,12vw,96px)/1 -apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;letter-spacing:-.05em;font-variant-numeric:tabular-nums}
.date{margin-top:10px;color:var(--text2);font-size:15px}
main{margin-top:52px;display:flex;flex-direction:column;align-items:center}
.logo{width:46px;height:46px;display:block}
h1{font:600 19px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;letter-spacing:-.02em;margin:16px 0 4px}
.sub{color:var(--muted);margin:0}
.fp{margin-top:30px;width:80px;height:80px;border-radius:50%;border:1px solid var(--line);background:var(--surface);color:var(--accent);
  display:grid;place-items:center;cursor:pointer;position:relative;box-shadow:0 10px 30px rgba(0,0,0,.08);
  transition:transform .15s ease,border-color .15s ease}
.fp:hover{border-color:var(--accent);transform:scale(1.04)}
.fp:focus-visible{outline:none;box-shadow:0 0 0 3px var(--bg),0 0 0 5px var(--accent)}
.fp svg{width:38px;height:38px}
.fp[aria-busy="true"]::after{content:"";position:absolute;inset:-7px;border-radius:50%;border:2px solid var(--accent);
  opacity:0;animation:pulse 1.6s ease-out infinite}
@keyframes pulse{0%{opacity:.55;transform:scale(.92)}100%{opacity:0;transform:scale(1.18)}}
.fp.shake{animation:shake .35s ease}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
.label{margin-top:12px;font-weight:600;font-size:14px}
.msg{min-height:20px;margin-top:2px;font-size:13px;color:var(--muted)}
.msg.err{color:var(--neg)}
.foot{margin-top:auto;padding-top:48px;color:var(--muted);font-size:12.5px;display:flex;align-items:center;gap:6px}
.foot svg{width:13px;height:13px;flex:none}
.foot b{font-weight:600;color:var(--text2)}
@media (prefers-reduced-motion:reduce){.fp,.fp:hover{transition:none;transform:none}.fp[aria-busy="true"]::after,.fp.shake{animation:none}}
</style></head><body>
<div style="flex:1"></div>
<div class="clock" aria-hidden="true"><div class="time" id="t"></div><div class="date" id="d"></div></div>
<main>
  <!--LOGO-->
  <h1>Margin is locked</h1>
  <p class="sub">Your money stays hidden until you unlock it.</p>
  <button class="fp" id="u" aria-label="Unlock with Touch ID" aria-busy="false">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M7.5 18.5c.9-1.4 1.5-3.2 1.5-5.5a3 3 0 0 1 6 0c0 1.2-.1 2.4-.4 3.5"/><path d="M12 13c0 3.2-.9 5.8-2.6 7.6"/>
      <path d="M5 15.5c.4-.8.5-1.6.5-2.5a6.5 6.5 0 0 1 11.6-4"/><path d="M18.4 11.5c.1.5.1 1 .1 1.5 0 2.6-.5 4.9-1.4 6.8"/>
      <path d="M14.6 20.5c.3-.8.6-1.7.8-2.6"/><path d="M6.6 6.3A8.9 8.9 0 0 1 12 4.5a9 9 0 0 1 3.4.7"/>
    </svg>
  </button>
  <div class="label">Unlock with Touch ID</div>
  <div class="msg" id="m" role="status" aria-live="polite">or your Mac password</div>
</main>
<div style="flex:1"></div>
<p class="foot"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>Locks <b>__MIN__</b> after you leave · everything stays on this Mac</p>
<script>
const b=document.getElementById('u'), m=document.getElementById('m');
const tick=()=>{ const n=new Date();
  document.getElementById('t').textContent=n.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}).replace(/\\s?(am|pm)$/i,'');
  document.getElementById('d').textContent=n.toLocaleDateString([],{weekday:'long',day:'numeric',month:'long'}); };
tick(); setInterval(tick,10000);
const say=(t,err)=>{ m.textContent=t; m.className='msg'+(err?' err':''); };
async function go(){ if(b.getAttribute('aria-busy')==='true') return;
  b.setAttribute('aria-busy','true'); say('Waiting for Touch ID…');
  try{ const r=await fetch('/api/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json();
    if(j.ok){ say('Unlocked'); location.reload(); return; }
    say(j.why||'Not unlocked',true); b.classList.remove('shake'); void b.offsetWidth; b.classList.add('shake'); }
  catch(e){ say('Margin isn\\u2019t running. Open it from the Margin app.',true); }
  b.setAttribute('aria-busy','false'); }
b.onclick=go;
// Ask once each time you come back to this page (never while it's in the background)
let armed=true; const here=()=>document.visibilityState==='visible'&&document.hasFocus();
const maybe=()=>{ if(armed&&here()){ armed=false; go(); } };
addEventListener('focus',maybe); document.addEventListener('visibilitychange',maybe);
addEventListener('blur',()=>{ if(b.getAttribute('aria-busy')!=='true') armed=true; }); maybe();
</script></body></html>"""


def lock_page(minutes: float) -> bytes:
    n = int(minutes) if float(minutes).is_integer() else minutes
    logo = icon.svg().decode().replace("<svg ", '<svg class="logo" aria-hidden="true" ', 1)
    return (LOCK_TEMPLATE.replace("<!--ICON-->", icon.HEAD_TAGS).replace("<!--LOGO-->", logo)
            .replace("__MIN__", f"{n} minute{'s' if n != 1 else ''}")).encode()


def _state(conn) -> dict:
    settings = sync.load_settings()
    rules = json.loads(paths.RULES_PATH.read_text()) if paths.RULES_PATH.exists() else []
    return {
        "data": dashboard.data(conn, networth.snapshot(conn), _status(conn)),
        "payees": suggest.add(conn, tidy.payee_list(conn)),   # each with one-tap category suggestions
        "senders": tidy.sender_list(conn, settings),
        "counts": tidy.uncategorized_counts(conn),
        "categories": {"out": tidy.SPEND + tidy.BOTH, "in": tidy.INCOME + tidy.BOTH},
        "groups": {"out": [["Spending", tidy.SPEND], ["Your own money", tidy.BOTH]],
                   "in": [["Income", tidy.INCOME],
                          ["Paid back (reduces that spending)", [c for c in tidy.SPEND if c != "Other spending"]],
                          ["Your own money", tidy.BOTH]]},
        "rules": [{"i": i, "match": r["match"] if isinstance(r["match"], str) else ", ".join(r["match"]),
                   "category": r["category"], "dir": r.get("direction")} for i, r in enumerate(rules)],
        "trusted": settings["trusted_senders"], "ignored": settings["ignored_senders"],
        "queued": len(json.loads(db.get_state(conn, "refetch_email_ids") or "[]")),
        "reviews": review.listing(conn),
        "open_flags": len(review.open_flags(conn)),
        "ai": {"on": bool(settings.get("ai_review", True)), "found": llm.ready(settings),
               "name": llm.name(settings), "local": llm.is_local(settings),    # e.g. "Codex"; local: on this Mac
               "why": llm.missing(settings)},                                   # why it isn't set up ('' if it is)
        "sorting": {"on": bool(settings.get("ai_sort", True)), "running": _SORTING.locked(),
                    "last": json.loads(db.get_state(conn, "ai_last") or "null"),
                    "decisions": ai.listing(conn), "bank_hints": ai.bank_hints(conn)},
    }


_SORTING = threading.Lock()


def _sort_in_background() -> bool:
    """Ask the AI to sort now, in a thread (it can take a few minutes). False if it's already running."""
    if not _SORTING.acquire(blocking=False):
        return False

    def work():
        conn = db.connect()
        try:
            ai.run(conn, sync.load_settings())
            sync.refresh_dashboard(conn)
            conn.commit()
        finally:
            conn.close()
            _SORTING.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def _status(conn) -> dict:
    last = db.get_state(conn, "last_sync")
    st = json.loads(last) if last else {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "problems": [],
                                        "counts": {}}
    newer = release.notice(conn)       # a newer release of Margin, from the nightly check
    if newer:
        st["update"] = newer
    return st


def _after_change(conn) -> None:
    sync.recategorize(conn, sync.load_settings())
    conn.commit()
    sync.refresh_dashboard(conn)   # today's net worth, in step with the change
    conn.commit()


DEMO: dict | None = None   # set by demo.py: {"start": fn, "status": fn -> (running, progress)}, a pretend sync


def _job_running() -> bool:
    if DEMO:
        return DEMO["status"]()[0]
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{paths.LAUNCHD_LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0 and "state = running" in r.stdout


class Handler(BaseHTTPRequestHandler):
    server_version = "Margin"

    def log_message(self, *a):   # no request logging: URLs carry nothing, but keep logs empty anyway
        pass

    # --- plumbing -------------------------------------------------------------------------------
    def _ok_host(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        ok = {f"{h}:{port}" for h in HOSTS}
        if port == 80:
            ok |= set(HOSTS)
        return host in ok

    def _url_key(self) -> str:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get("k") or [""])[0]

    def _cookie_key(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, val = part.strip().partition("=")
            if name == COOKIE:
                return val
        return ""

    def _ok_origin(self) -> bool:
        if not self._ok_host():
            return False
        key = self.headers.get("X-Margin-Key") or self._url_key() or self._cookie_key()
        return secrets.compare_digest(key, self.server.key)

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(min(n, 100_000)) or b"{}")

    # --- routes ---------------------------------------------------------------------------------
    def do_GET(self):
        self.server.touched = time.time()
        path = urllib.parse.urlparse(self.path).path
        if path in icon.ROUTES and self._ok_host():
            make, ctype = icon.ROUTES[path]
            return self._send(200, make(), ctype, cache="max-age=86400")
        if not self._ok_origin():
            return self._send(403, SIGN_IN_PAGE, "text/html; charset=utf-8")
        if path == "/" and self.headers.get("Host", "").split(":")[0] in OLD_HOSTNAMES:
            # The old address: carry this browser over to the new one (cookies belong to one name).
            self.send_response(303)
            self.send_header("Location", f"http://{HOSTNAME}/?k={self.server.key}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/" and self._url_key():
            # Key handed over by the Margin app: keep it in a private cookie and show the clean address.
            self.send_response(303)
            self.send_header("Set-Cookie", f"{COOKIE}={self.server.key}; Path=/; Max-Age=315360000; "
                                           "HttpOnly; SameSite=Strict")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        settings = sync.load_settings()
        if lock.locked(settings):
            if path == "/":
                return self._send(200, lock_page(lock.minutes(settings)), "text/html; charset=utf-8")
            if path == "/api/ping":
                return self._json({"ok": True, "locked": True})
            if path == "/api/progress":     # whether a sync runs, without any of its results
                return self._json({"running": _job_running(), "progress": None, "locked": True})
            return self._json({"error": "Margin is locked", "locked": True}, 423)
        conn = db.connect()
        try:
            if path == "/":
                html = dashboard.build(conn, networth.snapshot(conn), _status(conn), live_key=self.server.key,
                                       lock_minutes=lock.minutes(sync.load_settings()))
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(_state(conn))
            if path == "/api/progress" and DEMO:
                running, prog = DEMO["status"]()
                return self._json({"running": running, "progress": prog, "status": _status(conn)})
            if path == "/api/progress":
                try:
                    prog = json.loads(paths.PROGRESS_PATH.read_text())
                except (OSError, ValueError):
                    prog = None
                return self._json({"running": _job_running(), "progress": prog, "status": _status(conn)})
            if path == "/api/ping":
                return self._json({"ok": True})
            if path == "/api/glance":           # the Margin app's menu bar panel (423 above while locked)
                from . import glance
                return self._json(glance.summary(conn, sync.load_settings(), _status(conn), _job_running()))
            return self._send(404, b"not found", "text/plain")
        finally:
            conn.close()

    def do_POST(self):
        self.server.touched = time.time()
        if not self._ok_origin() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(403, b"forbidden", "text/plain")
        path = urllib.parse.urlparse(self.path).path
        b = self._body()
        settings = sync.load_settings()
        if path == "/api/unlock":
            ok, why = lock.unlock(settings)
            return self._json({"ok": ok, "why": why})
        if path == "/api/lock":
            lock.lock_now()
            return self._json({"locked": True})
        if path == "/api/heartbeat":           # the page is in front of you
            return self._json({"locked": lock.still_here(settings)})
        if lock.locked(settings):
            return self._json({"error": "Margin is locked", "locked": True}, 423)
        conn = db.connect()
        try:
            if path == "/api/rule":
                kw, cat, d = str(b.get("match", "")).strip(), str(b.get("category", "")), b.get("dir")
                allowed = set(tidy.SPEND + tidy.INCOME + tidy.BOTH)
                if not kw or cat not in allowed or d not in ("in", "out", None):
                    return self._json({"error": "pick a category and some text to match"}, 400)
                categorize.add_user_rule(kw, cat, d, match_text=False)
                _after_change(conn)
                changed = []
                if b.get("txn"):
                    # Rules never override payments you set by hand, but the one you just clicked must change,
                    # and so must that payee's other hand-set payments that had the same category.
                    t = conn.execute("SELECT * FROM transactions WHERE id=?", (int(b["txn"]),)).fetchone()
                    if t and t["category"] != cat:
                        payee = (t["counterparty"] or t["vpa"] or "").lower()
                        rows = conn.execute(
                            """SELECT id, category FROM transactions WHERE id=? OR (category_locked=1 AND direction=?
                               AND category=? AND LOWER(COALESCE(NULLIF(counterparty,''), vpa, ''))=?)""",
                            (t["id"], t["direction"], t["category"], payee)).fetchall()
                        changed = [[r["id"], r["category"]] for r in rows]
                        conn.executemany("UPDATE transactions SET category=? WHERE id=?", [(cat, r["id"]) for r in rows])
                        conn.commit()
                        sync.refresh_dashboard(conn)
                        conn.commit()
                st = _state(conn)
                st["changed"] = changed
                return self._json(st)
            if path == "/api/txns/restore":
                ch = [(str(c), int(i)) for i, c in b.get("changes", []) if str(c) in set(tidy.SPEND + tidy.INCOME + tidy.BOTH)]
                conn.executemany("UPDATE transactions SET category=? WHERE id=?", ch)
                conn.commit()
                sync.refresh_dashboard(conn)
                conn.commit()
                return self._json(_state(conn))
            if path == "/api/txn":
                cat = str(b.get("category", ""))
                if cat not in set(tidy.SPEND + tidy.INCOME + tidy.BOTH):
                    return self._json({"error": "pick a category"}, 400)
                conn.execute("UPDATE transactions SET category=?, category_locked=1 WHERE id=?",
                             (cat, int(b.get("id", 0))))
                conn.commit()
                sync.refresh_dashboard(conn)
                conn.commit()
                return self._json(_state(conn))
            if path == "/api/rule/delete":
                rules = json.loads(paths.RULES_PATH.read_text()) if paths.RULES_PATH.exists() else []
                i = int(b.get("i", -1))
                if 0 <= i < len(rules):
                    rules.pop(i)
                    paths.RULES_PATH.write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")
                    categorize.reload_rules()
                    _after_change(conn)
                return self._json(_state(conn))
            if path == "/api/senders":   # bulk hide / unhide
                keys, action = [str(k) for k in b.get("keys", []) if k], b.get("action")
                if not keys or action not in ("ignore", "unignore"):
                    return self._json({"error": "bad request"}, 400)
                if action == "ignore":
                    settings["ignored_senders"] += [k for k in keys if k not in settings["ignored_senders"]]
                else:
                    settings["ignored_senders"] = [k for k in settings["ignored_senders"] if k not in keys]
                paths.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
                _after_change(conn)
                return self._json(_state(conn))
            if path == "/api/sender":
                key, action = str(b.get("key", "")), b.get("action")
                if not key or action not in ("trust", "ignore", "untrust", "unignore"):
                    return self._json({"error": "bad request"}, 400)
                if action == "trust":
                    tidy.trust_sender(conn, settings, key)
                elif action == "ignore":
                    tidy.ignore_sender(conn, settings, key)
                else:
                    lst = "trusted_senders" if action == "untrust" else "ignored_senders"
                    settings[lst] = [s for s in settings[lst] if s != key]
                    paths.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
                _after_change(conn)
                return self._json(_state(conn))
            if path == "/api/stop":
                # A stop-loss is an alert level only: Margin never places, changes or cancels an order.
                from . import invest
                key, kind, name = str(b.get("key", "")), str(b.get("kind", "")), str(b.get("name", ""))[:120]
                price = b.get("price")
                if not key or kind not in invest.KINDS or (price is not None and not isinstance(price, (int, float))):
                    return self._json({"error": "bad request"}, 400)
                invest.set_stop(conn, key, kind, name, float(price) if price is not None else None)
                conn.commit()
                return self._json(_state(conn))
            if path == "/api/invest/review":
                from . import invreview
                kind = b.get("kind")
                if kind not in ("weekly", "monthly"):
                    return self._json({"error": "bad request"}, 400)
                r = invreview.run(conn, settings, kind, force=True)
                st = _state(conn)
                st["ran"] = {"kind": kind, "note": (r or {}).get("note", "") if r else "No investments tracked yet."}
                return self._json(st)
            if path == "/api/review/run":
                kind = b.get("kind")
                if kind not in review.KINDS:
                    return self._json({"error": "bad request"}, 400)
                r = review.run(conn, kind, settings, force=True)
                st = _state(conn)
                st["ran"] = {"kind": kind, "note": r["note"] if r else ""}
                return self._json(st)
            if path == "/api/flag":
                key, state = str(b.get("key", "")), b.get("state")
                if not key or state not in ("fine", "open"):
                    return self._json({"error": "bad request"}, 400)
                review.set_flag(conn, key, state)
                return self._json(_state(conn))
            if path == "/api/ai/run":
                started = _sort_in_background()
                st = _state(conn)
                st["sorting"]["running"] = True
                st["sort_started"] = started
                return self._json(st)
            if path in ("/api/ai/accept", "/api/ai/dismiss"):
                if path.endswith("accept"):
                    ai.accept(conn, settings, int(b.get("id", 0)))
                else:
                    ai.dismiss(conn, int(b.get("id", 0)))
                sync.refresh_dashboard(conn)
                conn.commit()
                return self._json(_state(conn))
            if path == "/api/ai/undo":
                ai.undo(conn, settings, int(b.get("id", 0)))
                sync.refresh_dashboard(conn)
                conn.commit()
                return self._json(_state(conn))
            if path == "/api/sync" and DEMO:
                DEMO["start"]()
                return self._json({"started": True})
            if path == "/api/sync":
                if not _job_running():
                    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{paths.LAUNCHD_LABEL}"],
                                   capture_output=True)
                return self._json({"started": True})
            return self._send(404, b"not found", "text/plain")
        finally:
            conn.close()


def _launchd_sockets() -> list:
    """The listening sockets launchd opened for this job (port 80), or [] when not started by launchd."""
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    except OSError:
        return []
    socks = []
    for name in (b"Web", b"Web6"):
        fds, n = ctypes.POINTER(ctypes.c_int)(), ctypes.c_size_t()
        if lib.launch_activate_socket(name, ctypes.byref(fds), ctypes.byref(n)) == 0:
            socks += [socket.socket(fileno=fds[i]) for i in range(n.value)]
            lib.free(fds)
    return socks


def _servers() -> list:
    socks = _launchd_sockets()
    if socks:
        out = []
        for sk in socks:
            h = ThreadingHTTPServer(sk.getsockname()[:2], Handler, bind_and_activate=False)
            h.socket.close()
            h.socket, h.server_address = sk, sk.getsockname()
            out.append(h)
        return out
    for port in (PORT, 0):
        try:
            return [ThreadingHTTPServer(("127.0.0.1", port), Handler)]
        except OSError:
            continue
    raise SystemExit("No free port for Margin's page.")


def serve() -> None:
    """Run the server in this process until it has been idle for IDLE_MINUTES."""
    paths.ensure_data_dir()
    # Same address and key every time, so an open Margin tab keeps working after a restart.
    servers = _servers()
    key = _key()
    for h in servers:
        h.key, h.touched, h.daemon_threads = key, time.time(), True
    httpd = servers[0]
    port = httpd.server_address[1]
    tmp = INFO_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "key": httpd.key, "pid": os.getpid()}))
    os.chmod(tmp, 0o600)
    tmp.replace(INFO_PATH)

    def reaper():
        while _SORTING.locked() or time.time() - max(h.touched for h in servers) < IDLE_MINUTES * 60:
            time.sleep(30)
        for h in servers:
            h.shutdown()

    for h in servers[1:]:
        threading.Thread(target=h.serve_forever, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    try:
        httpd.serve_forever()
    finally:
        try:
            if json.loads(INFO_PATH.read_text()).get("pid") == os.getpid():
                INFO_PATH.unlink()
        except (OSError, ValueError):
            pass


def _running_url() -> str | None:
    try:
        info = json.loads(INFO_PATH.read_text())
        url = f"http://127.0.0.1:{info['port']}"
        req = urllib.request.Request(f"{url}/api/ping", headers={"X-Margin-Key": info["key"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return f"{url}/?k={info['key']}"
    except (OSError, ValueError, KeyError):
        pass
    return None


def web_agent_installed() -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{paths.WEB_LABEL}"], capture_output=True)
    return r.returncode == 0


def hostname_ok() -> bool:
    try:
        return socket.gethostbyname(HOSTNAME) == "127.0.0.1"
    except OSError:
        return False


def address() -> str:
    """The address to show and bookmark."""
    if web_agent_installed():
        return f"http://{HOSTNAME}/" if hostname_ok() else "http://127.0.0.1/"
    return f"http://127.0.0.1:{PORT}/"


def open_app(browser: bool = True) -> str:
    """Open the page, letting this browser in (the key goes into a private cookie on the first visit)."""
    if web_agent_installed():
        url = f"{address()}?k={_key()}"    # launchd starts the server on this request
        if browser:
            subprocess.run(["open", url])
        return url
    # No launchd agent: start the server by hand (detached, so closing Terminal doesn't stop it).
    url = _running_url()
    if not url:
        INFO_PATH.unlink(missing_ok=True)
        env = dict(os.environ, PYTHONPATH=str(paths.PROJECT_DIR))
        subprocess.Popen([os.path.realpath(sys.executable), "-m", "margin", "serve"], env=env,
                         cwd=str(paths.PROJECT_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=open(paths.LOG_DIR / "server.err.log", "a"), start_new_session=True)
        for _ in range(50):
            time.sleep(0.1)
            url = _running_url()
            if url:
                break
    if not url:
        raise SystemExit("Margin's page didn't start. See ~/Library/Application Support/Margin/logs/server.err.log")
    if browser:
        subprocess.run(["open", url])
    return url
