#!/usr/bin/env python3
"""
resolve_people
==============

Builds people_map.json automatically instead of making you hand-write it.

The Drive Activity API gives actors as opaque `people/NNN` ids and will not
resolve them to names for users outside your contacts or Workspace directory.
The Drive revisions API, on the same document, gives real display names. Both
describe the same underlying edits, so the ids can be recovered by matching
timestamps.

For each unresolved id, every revision within +/- WINDOW seconds of one of its
activity events casts a vote for its display name. An id is resolved when one
name wins clearly enough.

    python resolve_people.py             # propose a mapping, write nothing
    python resolve_people.py --write     # write people_map.json
    python resolve_people.py --write --apply   # also rewrite stored rows
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from googleapiclient.discovery import build

import db
from activity_poller import get_credentials
from report import normalize_person

HERE = os.path.dirname(os.path.abspath(__file__))

WINDOW_S = 150          # revisions this close to an activity event vote for it
MIN_VOTES = 3           # ignore ids with too little evidence
MIN_SHARE = 0.65        # winning name must hold this share of the votes


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def resolve_self(creds):
    """The authenticated user is knowable exactly -- no correlation needed."""
    try:
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        user = drive.about().get(fields="user(displayName,emailAddress)").execute()["user"]
        return user.get("displayName"), user.get("emailAddress")
    except Exception as e:
        print(f"could not read own profile: {e}")
        return None, None


def correlate(conn):
    rows = conn.execute("SELECT ts, person, kind FROM edit_events").fetchall()

    named = []       # (time, display name) from revisions
    by_id = defaultdict(list)
    for r in rows:
        t = parse(r["ts"])
        if r["person"].startswith("people/"):
            by_id[r["person"]].append(t)
        elif r["kind"] == "revision":
            named.append((t, r["person"]))
    named.sort()

    proposals = {}
    for pid, times in by_id.items():
        votes = defaultdict(int)
        for t in times:
            for nt, name in named:
                delta = abs((nt - t).total_seconds())
                if delta <= WINDOW_S:
                    votes[name] += 1
                elif nt > t and delta > WINDOW_S:
                    break
        total = sum(votes.values())
        if total < MIN_VOTES:
            proposals[pid] = (None, total, dict(votes))
            continue
        winner, top = max(votes.items(), key=lambda kv: kv[1])
        proposals[pid] = ((winner if top / total >= MIN_SHARE else None), total, dict(votes))
    return proposals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--write", action="store_true", help="write people_map.json")
    ap.add_argument("--apply", action="store_true",
                    help="also rewrite already-stored rows to use resolved names")
    ap.add_argument("--normalize", action="store_true",
                    help="merge presence rows split by a '(idle)' style status suffix")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    conn = db.connect(cfg.get("db_path", os.path.join(HERE, "coauthor.sqlite3")))
    map_path = cfg.get("people_map_path", os.path.join(HERE, "people_map.json"))

    mapping = {}
    if os.path.exists(map_path):
        with open(map_path) as f:
            mapping = json.load(f)

    name, email = resolve_self(get_credentials(cfg))
    if name:
        mapping["__self__"] = name
        print(f"self: {name} <{email}>")

    proposals = correlate(conn)
    if not proposals:
        print("No unresolved people/NNN ids in the database yet.")

    for pid, (winner, total, votes) in sorted(proposals.items()):
        if winner:
            mapping[pid] = winner
            print(f"  {pid} -> {winner}   ({votes[winner]}/{total} votes)")
        else:
            detail = ", ".join(f"{k}:{v}" for k, v in sorted(votes.items(), key=lambda x: -x[1]))
            print(f"  {pid} -> UNRESOLVED  ({total} votes: {detail or 'none'})")

    if args.write:
        with open(map_path, "w") as f:
            json.dump(mapping, f, indent=2, sort_keys=True)
        print(f"\nwrote {map_path}")

    if args.apply:
        n = 0
        with db.write(conn) as c:
            for pid, nm in mapping.items():
                if pid.startswith("people/"):
                    cur = c.execute("UPDATE OR IGNORE edit_events SET person=? WHERE person=?",
                                    (nm, pid))
                    n += cur.rowcount
        print(f"rewrote {n} stored rows")

    if args.normalize:
        # Older rows may carry Google's "(idle)" style status suffix, which
        # splits one collaborator into two people who each hold part of the
        # time. New rows are normalised on the way in by the watcher.
        merged = 0
        with db.write(conn) as c:
            rows = c.execute("SELECT DISTINCT person FROM presence_events").fetchall()
            for row in rows:
                clean = normalize_person(row["person"])
                if clean != row["person"]:
                    cur = c.execute("UPDATE presence_events SET person=? WHERE person=?",
                                    (clean, row["person"]))
                    merged += cur.rowcount
                    print(f"  merged {row['person']!r} -> {clean!r} ({cur.rowcount} rows)")
        print(f"normalised {merged} presence rows"
              if merged else "no presence names needed normalising")

    unresolved = [p for p, (w, _, _) in proposals.items() if not w]
    if unresolved:
        print("\nStill unresolved. These are people whose edits never landed near a "
              "named revision -- usually comment-only or move/rename activity. Add them "
              f"by hand to {os.path.basename(map_path)} if you can identify them, or "
              "leave them; the report will just show the raw id.")


if __name__ == "__main__":
    main()
