#!/usr/bin/env python3
"""
coauthor / activity_poller
==========================

The supported half of the system. Uses read-only Google APIs to answer
"who changed the document, when, and by how much" -- which is the part that
actually holds up as evidence of contribution.

Two sources:
  * Drive API revisions.list   -> timestamp + lastModifyingUser (real display
                                  name and, inside a Workspace domain, email)
  * Drive Activity API v2      -> finer-grained EDIT / COMMENT / MOVE events
                                  (actors come back as people/NNN ids; names
                                  are resolved from the revision list or from
                                  people_map.json)

Plus a periodic plain-text export so the report can compute word deltas.

    python activity_poller.py --once     # single pass
    python activity_poller.py            # loop forever at poll_minutes
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import db

HERE = os.path.dirname(os.path.abspath(__file__))

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.activity.readonly",
]


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_credentials(cfg):
    """Service account if configured, else the interactive OAuth flow.

    The service account path is what you want on an unattended box: OAuth apps
    left in "Testing" publishing status have their refresh tokens expired by
    Google after 7 days, which means a browser round-trip every week. A service
    account has no such expiry -- share the doc with its client_email and it
    just keeps working.
    """
    sa_path = cfg.get("service_account_path")
    if sa_path:
        if not os.path.exists(sa_path):
            sys.exit(f"Missing {sa_path}. See DEPLOY.md step 2.")
        return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)

    token_path = cfg.get("token_path", os.path.join(HERE, "token.json"))
    client_path = cfg.get("client_secret_path", os.path.join(HERE, "client_secret.json"))
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_path):
                sys.exit(f"Missing {client_path}. See README step 3.")
            flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


# ---------------------------------------------------------------------------


def poll_revisions(drive, conn, file_id):
    """Drive revisions give real names with no id-resolution problem."""
    added = 0
    token = None
    while True:
        try:
            resp = drive.revisions().list(
                fileId=file_id,
                pageSize=1000,
                pageToken=token,
                fields="nextPageToken,revisions(id,modifiedTime,"
                       "lastModifyingUser(displayName,emailAddress))",
            ).execute()
        except HttpError as e:
            print(f"revisions.list failed: {e}", file=sys.stderr)
            return 0
        for rev in resp.get("revisions", []):
            user = rev.get("lastModifyingUser") or {}
            name = user.get("displayName") or "unknown"
            db.add_edit(
                conn,
                rev["modifiedTime"],
                name,
                email=user.get("emailAddress"),
                kind="revision",
                revision_id=rev.get("id"),
            )
            added += 1
        token = resp.get("nextPageToken")
        if not token:
            break
    return added


def load_people_map(cfg):
    path = cfg.get("people_map_path", os.path.join(HERE, "people_map.json"))
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def poll_activity(activity, conn, file_id, since, people_map):
    """Drive Activity API. Actors are people/NNN; map them where we can."""
    added = 0
    token = None
    body_base = {
        "itemName": f"items/{file_id}",
        "pageSize": 100,
        "filter": f'time >= "{iso(since)}"',
    }
    while True:
        body = dict(body_base)
        if token:
            body["pageToken"] = token
        try:
            resp = activity.activity().query(body=body).execute()
        except HttpError as e:
            print(f"activity.query failed: {e}", file=sys.stderr)
            return added
        for act in resp.get("activities", []):
            ts = act.get("timestamp")
            if not ts:
                tr = act.get("timeRange") or {}
                ts = tr.get("endTime") or tr.get("startTime")
            if not ts:
                continue
            detail = act.get("primaryActionDetail", {})
            kind = "activity:" + (next(iter(detail), "unknown").upper())
            for actor in act.get("actors", []):
                known = (actor.get("user") or {}).get("knownUser") or {}
                pid = known.get("personName") or "unknown"
                name = people_map.get(pid, pid)
                if known.get("isCurrentUser") and pid not in people_map:
                    name = people_map.get("__self__", pid)
                db.add_edit(conn, ts, name, kind=kind, revision_id=None)
                added += 1
        token = resp.get("nextPageToken")
        if not token:
            break
    return added


def snapshot_text(drive, conn, file_id):
    try:
        data = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    except HttpError as e:
        print(f"export failed: {e}", file=sys.stderr)
        return None
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    prev = conn.execute("SELECT sha256 FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if prev and prev["sha256"] == sha:
        return None
    words = len(text.split())
    db.add_snapshot(conn, iso(now_utc()), words, len(text), sha)
    return words


def run_once(cfg, conn, drive, activity, people_map):
    file_id = cfg["doc_id"]
    lookback_days = int(cfg.get("activity_lookback_days", 60))
    last = db.get_meta(conn, "activity_cursor")
    since = (datetime.fromisoformat(last.replace("Z", "+00:00"))
             if last else now_utc() - timedelta(days=lookback_days))
    since = since - timedelta(minutes=10)     # small overlap; inserts are deduped

    r = poll_revisions(drive, conn, file_id)
    a = poll_activity(activity, conn, file_id, since, people_map)
    w = snapshot_text(drive, conn, file_id)
    db.set_meta(conn, "activity_cursor", iso(now_utc()))

    print(f"[{iso(now_utc())}] revisions={r} activity={a} "
          f"snapshot={'%d words' % w if w is not None else 'unchanged'}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    conn = db.connect(cfg.get("db_path", os.path.join(HERE, "coauthor.sqlite3")))

    creds = get_credentials(cfg)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    activity = build("driveactivity", "v2", credentials=creds, cache_discovery=False)
    people_map = load_people_map(cfg)

    if args.once:
        run_once(cfg, conn, drive, activity, people_map)
        return

    every = int(cfg.get("poll_minutes", 5)) * 60
    while True:
        try:
            run_once(cfg, conn, drive, activity, people_map)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(every)


if __name__ == "__main__":
    main()
