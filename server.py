#!/usr/bin/env python3
"""
coauthor / server
=================

Read-only web dashboard over the same SQLite database the report reads.

    uvicorn server:app --host 127.0.0.1 --port 8000

Behind Caddy in production; Caddy terminates TLS and this only ever binds to
localhost, so the password check below is the only thing standing between the
internet and the data. It is a single shared password, held in the environment,
compared in constant time, behind a rate limiter. That is proportionate for a
group-project dashboard -- it is not an identity system, and nothing here is
per-user.

Environment:
    COAUTHOR_PASSWORD   required; the shared view password
    COAUTHOR_SECRET     required; random string used to sign session cookies
    COAUTHOR_CONFIG     optional; path to config.json
"""


import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    raise SystemExit("Python 3.9+ required for zoneinfo")

import db
import report

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")

CONFIG_PATH = os.environ.get("COAUTHOR_CONFIG", os.path.join(HERE, "config.json"))
with open(CONFIG_PATH) as f:
    CFG = json.load(f)

PASSWORD = os.environ.get("COAUTHOR_PASSWORD")
SECRET = os.environ.get("COAUTHOR_SECRET")
if not PASSWORD or not SECRET:
    raise SystemExit(
        "Set COAUTHOR_PASSWORD and COAUTHOR_SECRET in the environment.\n"
        "Locally:  $env:COAUTHOR_PASSWORD='...'; $env:COAUTHOR_SECRET='...'\n"
        "On EC2:   they live in /etc/coauthor.env (see DEPLOY.md)."
    )

TZ = ZoneInfo(CFG.get("timezone", "America/Toronto"))
DB_PATH = CFG.get("db_path", os.path.join(HERE, "coauthor.sqlite3"))
SESSION_MAX_AGE = 14 * 24 * 3600
COOKIE_NAME = "coauthor_session"

app = FastAPI(title="coauthor", docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

_attempts = {}                          # ip -> recent failed attempt timestamps
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW_S = 300
MAX_TRACKED_IPS = 4096


def _prune_attempts(now):
    """Forget addresses whose failures have all aged out.

    This login page is on the public internet and gets scanned continuously, so
    a table keyed by remote address grows for as long as the process lives if
    nothing clears it. Entries are cheap individually and the cap is a backstop
    against a source rotating addresses faster than they expire.
    """
    stale = [ip for ip, q in _attempts.items()
             if not q or now - q[-1] > ATTEMPT_WINDOW_S]
    for ip in stale:
        del _attempts[ip]
    if len(_attempts) > MAX_TRACKED_IPS:
        for ip in sorted(_attempts, key=lambda k: _attempts[k][-1])[:len(_attempts) // 2]:
            del _attempts[ip]


def _rate_limited(ip):
    now = time.time()
    if len(_attempts) > MAX_TRACKED_IPS // 2:
        _prune_attempts(now)
    q = _attempts.get(ip)
    if q is None:
        return False                    # unknown address: no failures to count
    while q and now - q[0] > ATTEMPT_WINDOW_S:
        q.popleft()
    return len(q) >= MAX_ATTEMPTS


def _record_failure(ip):
    _attempts.setdefault(ip, deque()).append(time.time())


def _sign(payload):
    mac = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def _valid_session(cookie):
    if not cookie or "." not in cookie:
        return False
    payload, _, mac = cookie.rpartition(".")
    expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    try:
        issued = int(payload.split(":", 1)[0])
    except (ValueError, IndexError):
        return False
    return (time.time() - issued) < SESSION_MAX_AGE


def _require(session):
    if not _valid_session(session):
        raise HTTPException(status_code=401, detail="not authenticated")


# The dashboard is a single file that changes whenever the project is
# redeployed, and a browser holding yesterday's copy looks exactly like a
# feature that failed to ship. Nothing here is worth caching -- the payload is
# small and the data behind it moves every few seconds.
NO_STORE = {"cache-control": "no-store, must-revalidate", "pragma": "no-cache"}


def _page(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def _build_id():
    """Fingerprint of the dashboard file, sent with every data refresh.

    An open tab polls for data but never re-evaluates its own JavaScript, so
    after a deploy it renders new data with old code -- which shows up as
    missing labels or raw field names and looks like a fresh bug. The page
    compares this against the value it started with and reloads itself once it
    changes.
    """
    try:
        st = os.stat(os.path.join(TEMPLATES, "dashboard.html"))
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return "unknown"


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    html = _page("login.html")
    banner = ""
    if error == "bad":
        banner = '<p class="err">That password is not right. Try again.</p>'
    elif error == "rate":
        banner = '<p class="err">Too many attempts. Wait five minutes.</p>'
    return HTMLResponse(html.replace("<!--ERROR-->", banner), headers=NO_STORE)


@app.post("/login")
def do_login(request: Request, password: str = Form("")):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return RedirectResponse("/login?error=rate", status_code=303)
    if not hmac.compare_digest(password, PASSWORD):
        _record_failure(ip)
        return RedirectResponse("/login?error=bad", status_code=303)
    _attempts.pop(ip, None)
    token = _sign(f"{int(time.time())}:{secrets.token_hex(8)}")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=os.environ.get("COAUTHOR_INSECURE_COOKIE") != "1",
    )
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(session: str = Cookie(None, alias=COOKIE_NAME)):
    if not _valid_session(session):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_page("dashboard.html"), headers=NO_STORE)


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe. Deliberately leaks nothing."""
    return {"ok": True}


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


def _conn():
    """Fresh read-only connection per request; SQLite is cheap to open."""
    uri = f"file:{DB_PATH}?mode=ro"
    try:
        c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError:
        return db.connect(DB_PATH)             # not created yet -- make it
    # The watcher writes to this file while we read it. Contention is rare at
    # the rate it writes, but the default behaviour on a locked database is to
    # fail instantly, which would surface as a 500 on the dashboard. Wait
    # instead; a few hundred milliseconds is invisible next to a 10s refresh.
    c.execute("PRAGMA busy_timeout = 3000")
    c.row_factory = sqlite3.Row
    return c


def _local(dt):
    return dt.astimezone(TZ).isoformat() if dt else None


def _watcher_error(raw):
    """Decode the watcher's stored failure reason, if it left one."""
    if not raw:
        return None
    try:
        e = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(e, dict) or not e.get("message"):
        return None
    ts = report.parse(e["ts"]) if e.get("ts") else None
    return {"kind": e.get("kind"), "message": e["message"], "since": _local(ts)}


def build_payload():
    # Reloaded per request so editing people_map.json takes effect without a
    # restart -- the whole point of it is fixing names while looking at them.
    aliases, hidden = report.load_identity(
        CFG.get("people_map_path", os.path.join(HERE, "people_map.json")))

    conn = _conn()
    try:
        heartbeat = db.get_meta(conn, "watcher_heartbeat")
        last_presence = conn.execute(
            "SELECT ts FROM presence_events ORDER BY ts DESC LIMIT 1"
        ).fetchone()

        # Measure sessions that are still running up to the watcher's last
        # heartbeat. While it is alive that is within 30 seconds of now, so open
        # sessions tick upward; once it dies they stop rather than inflating.
        open_until = report.parse(heartbeat) if heartbeat else None

        rows, psess, esess, flags, snaps = report.build_summary(
            conn, aliases, hidden, open_until)
        events = report.activity_log(conn, aliases, hidden)
        cursor = db.get_meta(conn, "activity_cursor")
        watcher_error = db.get_meta(conn, "watcher_error")
    finally:
        conn.close()

    now = datetime.now(timezone.utc)

    # --- watcher health -------------------------------------------------
    # Liveness comes from the heartbeat, not from the last presence row. An
    # empty document generates no presence rows at all, so judging health by
    # them reports a healthy watcher as offline the moment everyone leaves.
    # The heartbeat ticks every 30s whatever the document is doing.
    last_ev = report.parse(last_presence["ts"]) if last_presence else None
    beat = report.parse(heartbeat) if heartbeat else None
    stale_after = int(CFG.get("watcher_stale_minutes", 15))

    if beat is not None:
        mins_since = (now - beat).total_seconds() / 60
        watcher_ok = mins_since < 3            # 30s heartbeat; 3 min is generous
    else:
        # Database written before heartbeats existed. Fall back to the old
        # heuristic, which is wrong during idle periods but never claims a dead
        # watcher is alive.
        mins_since = (now - last_ev).total_seconds() / 60 if last_ev else None
        watcher_ok = mins_since is not None and mins_since < stale_after

    # A page that has lost its connection to Google freezes the collaborator
    # list, so presence readings from that window mean nothing. Sessions already
    # end at the disconnect; say so on the page too, rather than just showing a
    # quietly empty "in the document right now".
    disconnects = [t for t, ev in flags if ev == "disconnected"]
    last_disconnect = max(disconnects) if disconnects else None
    disconnected_now = (last_disconnect is not None
                        and (now - last_disconnect).total_seconds() < 180)

    last_poll = report.parse(cursor) if cursor else None
    poll_mins = (now - last_poll).total_seconds() / 60 if last_poll else None
    poller_ok = poll_mins is not None and poll_mins < int(CFG.get("poll_minutes", 5)) * 4

    # --- who is in the doc right now ------------------------------------
    live = {}
    if watcher_ok:
        for s in psess:
            if s.get("still_open"):
                live[s["person"]] = s["start"]

    people = []
    for r in rows:
        people.append({
            "person": r["person"],
            "email": r["email"],
            "present_s": r["present_s"],
            "active_s": r["active_s"],
            "edit_events": r["edit_events"],
            "edit_sessions": r["edit_sessions"],
            "visits": r["visits"],
            "first": _local(r["first"]),
            "last": _local(r["last"]),
            "live": r["person"] in live,
            "live_since": _local(live.get(r["person"])),
        })

    timeline = [
        {"person": s["person"], "start": _local(s["start"]), "end": _local(s["end"]),
         "kind": "present", "truncated": bool(s.get("truncated"))}
        for s in psess
    ]
    for person, blocks in esess.items():
        for a, b in blocks:
            timeline.append({"person": person, "start": _local(a), "end": _local(b),
                             "kind": "editing", "truncated": False})
    timeline.sort(key=lambda x: x["start"] or "")

    flag_counts = defaultdict(int)
    for _, kind in flags:
        flag_counts[kind] += 1

    doc_id = CFG.get("doc_id", "")
    return {
        "doc": {
            "title": CFG.get("doc_title", "Document"),
            "url": f"https://docs.google.com/document/d/{doc_id}/edit" if doc_id else None,
        },
        "generated_at": _local(now),
        "timezone": str(TZ),
        "build": _build_id(),
        "health": {
            "watcher_ok": watcher_ok,
            "watcher_minutes_since": round(mins_since, 1) if mins_since is not None else None,
            "watcher_last": _local(last_ev),
            "watcher_heartbeat": _local(beat),
            "disconnected_now": disconnected_now,
            "last_disconnect": _local(last_disconnect),
            "watcher_error": _watcher_error(watcher_error),
            "poller_ok": poller_ok,
            "poller_minutes_since": round(poll_mins, 1) if poll_mins is not None else None,
            "poller_last": _local(last_poll),
            "has_presence_data": last_ev is not None,
        },
        "people": people,
        "timeline": timeline,
        "events": [
            {"ts": _local(e["ts"]), "person": e["person"], "event": e["event"]}
            for e in events
        ],
        "flags": dict(flag_counts),
        "words": [{"ts": _local(t), "words": w} for t, w in snaps],
    }


@app.get("/api/summary")
def api_summary(session: str = Cookie(None, alias=COOKIE_NAME)):
    _require(session)
    return JSONResponse(build_payload(), headers=NO_STORE)


@app.get("/api/export.csv")
def api_csv(session: str = Cookie(None, alias=COOKIE_NAME)):
    _require(session)
    import csv
    import io
    payload = build_payload()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["person", "type", "start", "end", "minutes", "truncated"])
    for s in payload["timeline"]:
        a = datetime.fromisoformat(s["start"])
        b = datetime.fromisoformat(s["end"])
        w.writerow([s["person"], s["kind"], s["start"], s["end"],
                    round((b - a).total_seconds() / 60, 1), int(s["truncated"])])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"content-disposition": 'attachment; filename="coauthor-timeline.csv"'},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=CFG.get("web_host", "127.0.0.1"),
        port=int(CFG.get("web_port", 8000)),
    )
