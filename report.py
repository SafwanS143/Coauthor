#!/usr/bin/env python3
"""
coauthor / report
=================

Turns raw events into sessions and a per-person summary.

Three different numbers per person, which mean different things:

  present    wall-clock time their client was connected to the doc.
             Includes "tab left open in another window". Weakest signal.
  active     time during which their cursor actually moved. A person reading
             carefully still scrolls and clicks, so this covers genuine reading;
             a parked tab does not accumulate it. Best proxy for real use.
  edits      revision/activity events attributed to them by Google itself.
             This is the number that survives a dispute.

    python report.py                 # summary to stdout
    python report.py --html out.html --csv timeline.csv
"""

import argparse
import csv
import html
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db

HERE = os.path.dirname(os.path.abspath(__file__))

RELOAD_GRACE_S = 90       # after a page reload, re-appear within this or we call it a leave
CURSOR_GAP_S = 150        # cursor events closer than this are one continuous active block
CURSOR_MIN_CREDIT_S = 30  # an isolated cursor move still counts for this much
EDIT_GAP_S = 600          # edits closer than this are one working session

# A collaborator can vanish from the list without having closed anything: the
# document disconnects after sitting untouched (measured once at roughly 17
# minutes), and browsers such as Chrome and Brave also unload background tabs to
# save memory. Both look identical to a closed tab from the DOM. The run-up does
# distinguish them though -- somebody who closes a tab was usually doing
# something moments earlier, whereas these follow a long stretch of stillness --
# so a departure preceded by this much silence is reported as going inactive
# rather than as leaving, since we cannot honestly claim they closed it.
IDLE_DISCONNECT_S = 600


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Identity
#
# The same human reaches this database under as many as three unrelated names.
# Drive's revision history reports the account handle ("achen"), the presence
# chip in the Docs UI reports the name shown to collaborators ("Alice Chen"),
# and Drive Activity reports only a numeric actor id ("people/1234..."). Nothing
# in any feed says they are the same person, so without a mapping one person's
# contribution is split across three rows.
#
# people_map.json therefore accepts:
#
#   "people/1234..."  : "Alice Chen"      resolve a Drive Activity actor id
#   "__aliases__"     : {"achen": "Alice Chen"}   merge one name into another
#   "__hidden__"      : ["Coauthor Watcher"]      drop from the report entirely
#   "anyname"         : null                      shorthand for hiding it
# ---------------------------------------------------------------------------


_STATUS_SUFFIXES = tuple(
    form
    for word in ("idle", "away", "inactive", "active", "offline")
    for form in (f"({word})", f"- {word}", f"– {word}", f"— {word}")
)


def normalize_person(name):
    """Strip a trailing presence-status marker: '(idle)', '- away', and so on.

    Google appends these to the collaborator chip once someone stops moving.
    Left in place they split one person into two rows holding half the time
    each. The watcher strips them on the way in; doing it again on the way out
    means rows written before that existed are still read correctly, with no
    migration.
    """
    out = str(name).strip()
    changed = True
    while changed:                            # "Alice (idle) (viewer)" needs two passes
        changed = False
        low = out.lower()
        for suffix in _STATUS_SUFFIXES:
            if low.endswith(suffix):
                out = out[: -len(suffix)].strip()
                changed = True
                break
    return out or "*"


def load_identity(path):
    """Read people_map.json into (aliases, hidden). Missing file is fine."""
    raw = {}
    if path and os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)

    aliases, hidden = {}, set()
    for key, value in raw.items():
        key = str(key).strip()
        if key.startswith("//"):
            continue                          # JSON has no comments; this is the convention
        if key == "__hidden__":
            hidden.update(str(h).strip().casefold() for h in (value or []))
        elif key == "__aliases__":
            for frm, to in (value or {}).items():
                aliases[str(frm).strip().casefold()] = str(to).strip()
        elif key == "__self__":
            continue                          # consumed by the poller, not a display alias
        elif value is None or not str(value).strip():
            hidden.add(key.casefold())        # mapping a name to null hides it
        else:
            aliases[key.casefold()] = str(value).strip()
    return aliases, hidden


def _lookup(key, aliases):
    """Tolerate a missing or spurious 'people/' prefix -- an easy typo to make."""
    if key in aliases:
        return aliases[key]
    if key.startswith("people/") and key[7:] in aliases:
        return aliases[key[7:]]
    prefixed = "people/" + key
    if prefixed in aliases:
        return aliases[prefixed]
    return None


def canon(name, aliases):
    """Resolve a stored name to its canonical form, following alias chains."""
    current = normalize_person(name)
    seen = set()
    while True:
        key = current.casefold()
        if key in seen:
            return current                    # a -> b -> a; stop rather than loop
        seen.add(key)
        nxt = _lookup(key, aliases)
        if nxt is None:
            return current
        current = nxt


def is_hidden(name, hidden):
    return str(name).strip().casefold() in hidden


def fmt_dur(seconds):
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_local(dt, tz_offset_hours):
    return (dt + timedelta(hours=tz_offset_hours)).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------


def presence_sessions(conn, aliases=None, open_until=None):
    """Sessions from raw events.

    open_until is where a session that is still running should be measured to.
    Without it, an open session ends at the last recorded event, which freezes
    both the elapsed time and the "last seen" column the moment the document
    goes quiet -- somebody sitting reading appears to have stopped existing.
    Callers pass the watcher's last heartbeat, so an open session grows in real
    time while the watcher is alive and stops dead when it is not.
    """
    aliases = aliases or {}
    rows = conn.execute(
        "SELECT ts, person, event, source FROM presence_events ORDER BY ts, id"
    ).fetchall()
    events = [(parse(r["ts"]), canon(r["person"], aliases), r["event"], r["source"])
              for r in rows]

    open_now = {}          # person -> session start
    sessions = []
    flags = []

    def bridge(i, t):
        """Close open sessions across a tracking gap -- unless they come back.

        A page reload and a watcher restart both blank the collaborator list for
        a few seconds. Treating that as everybody leaving splits one visit into
        several and invents departures nobody made, so anyone who reappears
        within the grace window keeps the session they already had.
        """
        for p, start in list(open_now.items()):
            reappears = any(
                tt <= t + timedelta(seconds=RELOAD_GRACE_S) and pp == p
                and e2 in ("join", "cursor")
                for tt, pp, e2, _ in events[i + 1:i + 400]
            )
            if not reappears:
                sessions.append({"person": p, "start": start, "end": t, "truncated": True})
                del open_now[p]

    for i, (t, person, ev, _src) in enumerate(events):
        # All three blank or freeze the collaborator list, so none of them is
        # evidence about who is in the document. A restart is a longer reload;
        # a dropped connection leaves the last known names painted on screen
        # indefinitely, which used to keep whoever was showing at that moment
        # marked present for hours after they had gone. bridge() keeps a session
        # if the person turns up again within the grace window and ends it at
        # the gap if they do not.
        if ev in ("page_reload", "watcher_stop", "disconnected"):
            if ev == "disconnected":
                flags.append((t, ev))
            bridge(i, t)
            continue

        if ev == "join":
            if person not in open_now:
                open_now[person] = t

        elif ev == "leave":
            if person in open_now:
                sessions.append({"person": person, "start": open_now.pop(person),
                                 "end": t, "truncated": False})

        elif ev == "cursor":
            # Cursor movement measures activity; it must not start a session.
            # A departure is backdated to the last time the chip was seen, so a
            # cursor event recorded moments later sorts after it and used to
            # reopen the session that had just closed -- leaving somebody shown
            # as present indefinitely. Arrivals come from join events, which the
            # page script emits in both chip and degraded modes.
            pass

        elif ev in ("overflow", "jserror"):
            flags.append((t, ev))

    if events:
        last_t = events[-1][0]
        end_at = max(open_until, last_t) if open_until else last_t
        for p, start in open_now.items():
            sessions.append({"person": p, "start": start, "end": end_at,
                             "truncated": True, "still_open": True})

    return sessions, flags


def active_blocks(conn, aliases=None):
    """Contiguous runs of cursor movement per person."""
    aliases = aliases or {}
    rows = conn.execute(
        "SELECT ts, person FROM presence_events WHERE event='cursor' ORDER BY person, ts"
    ).fetchall()
    per = defaultdict(list)
    for r in rows:
        per[canon(r["person"], aliases)].append(parse(r["ts"]))
    for times in per.values():
        times.sort()                          # aliasing can interleave two source names

    floor = timedelta(seconds=CURSOR_MIN_CREDIT_S)

    def emit(bucket, a, b):
        bucket.append((a, b if (b - a) >= floor else a + floor))

    out = defaultdict(list)
    for person, times in per.items():
        if not times:
            continue
        start = prev = times[0]
        for t in times[1:]:
            if (t - prev).total_seconds() > CURSOR_GAP_S:
                emit(out[person], start, prev)
                start = t
            prev = t
        emit(out[person], start, prev)
    return out


def edit_sessions(conn, aliases=None):
    aliases = aliases or {}
    rows = conn.execute(
        "SELECT ts, person, email FROM edit_events ORDER BY person, ts"
    ).fetchall()
    per = defaultdict(list)
    emails = {}
    for r in rows:
        person = canon(r["person"], aliases)
        per[person].append(parse(r["ts"]))
        if r["email"]:
            emails[person] = r["email"]
    for times in per.values():
        times.sort()                          # merged names arrive out of order

    out = defaultdict(list)
    for person, times in per.items():
        start = prev = times[0]
        for t in times[1:]:
            if (t - prev).total_seconds() > EDIT_GAP_S:
                out[person].append((start, prev))
                start = t
            prev = t
        out[person].append((start, prev))
    return out, emails, {p: len(v) for p, v in per.items()}


def build_summary(conn, aliases=None, hidden=None, open_until=None):
    aliases = aliases or {}
    hidden = hidden or set()
    psess, flags = presence_sessions(conn, aliases, open_until)
    active = active_blocks(conn, aliases)
    esess, emails, ecounts = edit_sessions(conn, aliases)

    psess = [s for s in psess if not is_hidden(s["person"], hidden)]
    active = {p: v for p, v in active.items() if not is_hidden(p, hidden)}
    esess = {p: v for p, v in esess.items() if not is_hidden(p, hidden)}

    people = set()
    people.update(s["person"] for s in psess)
    people.update(active)
    people.update(esess)
    people.discard("*")

    rows = []
    for p in sorted(people):
        pres = sum((s["end"] - s["start"]).total_seconds()
                   for s in psess if s["person"] == p)
        act = sum((b - a).total_seconds() for a, b in active.get(p, []))
        edt = sum((b - a).total_seconds() for a, b in esess.get(p, []))
        stamps = [s["start"] for s in psess if s["person"] == p]
        stamps += [a for a, _ in esess.get(p, [])]
        ends = [s["end"] for s in psess if s["person"] == p]
        ends += [b for _, b in esess.get(p, [])]
        rows.append({
            "person": p,
            "email": emails.get(p, ""),
            "present_s": pres,
            "active_s": act,
            "edit_span_s": edt,
            "visits": sum(1 for s in psess if s["person"] == p),
            "edit_events": ecounts.get(p, 0),
            "edit_sessions": len(esess.get(p, [])),
            "first": min(stamps) if stamps else None,
            "last": max(ends) if ends else None,
        })
    rows.sort(key=lambda r: (-r["edit_events"], -r["active_s"]))

    snaps = conn.execute("SELECT ts, words FROM snapshots ORDER BY ts").fetchall()
    return rows, psess, esess, flags, [(parse(s["ts"]), s["words"]) for s in snaps]


# ---------------------------------------------------------------------------


LEAVE_EVENTS = ("leave", "leave_idle")


def _classify_leaves(conn, raw, aliases):
    """Mark departures that follow a long silence as idle disconnects."""
    if not raw:
        return
    since = min(e["ts"] for e in raw) - timedelta(hours=2)
    moves = defaultdict(list)
    for c in conn.execute(
            "SELECT ts, person FROM presence_events WHERE event='cursor' AND ts >= ? "
            "ORDER BY ts", (since.isoformat().replace("+00:00", "Z"),)):
        moves[canon(c["person"], aliases)].append(parse(c["ts"]))

    for e in raw:
        if e["event"] != "leave":
            continue
        before = [m for m in moves.get(e["person"], []) if m <= e["ts"]]
        quiet = (e["ts"] - before[-1]).total_seconds() if before else None
        if quiet is None or quiet >= IDLE_DISCONNECT_S:
            e["event"] = "leave_idle"


def _drop_repeat_joins(raw):
    """Remove arrivals for people the log already shows as being in the document.

    A reload or a restart rebuilds the page, and the fresh script has no memory
    of who was already there, so it announces everyone again. Three "opened the
    document" lines with no departure between them reads as a malfunction.
    Decide walking forwards; hand back newest-first.
    """
    present, out, stopped_at = set(), [], None
    for e in reversed(raw):
        if e["event"] == "watcher_stop":
            stopped_at = e["ts"]
        elif e["event"] == "join":
            # After a long outage, whoever was on screen beforehand is no
            # evidence of anything now, so let the next arrival stand alone.
            if stopped_at and (e["ts"] - stopped_at).total_seconds() > RELOAD_GRACE_S:
                present.clear()
            stopped_at = None
            if e["person"] in present:
                continue
            present.add(e["person"])
        elif e["event"] in LEAVE_EVENTS:
            if e["person"] not in present:
                continue                      # a departure for someone already gone
            present.discard(e["person"])
        out.append(e)
    out.reverse()
    return out


def _collapse_restarts(out, window_s=180):
    """Fold a stop and its following start into one entry.

    A deploy or the nightly recycle produces both within seconds. Two alarming
    lines for a few seconds of downtime reads far worse than the reality. The
    matching stop is not necessarily adjacent -- a join from the reconnecting
    page often lands between them -- so pair by time rather than by position.
    """
    drop = set()
    for i, cur in enumerate(out):
        if cur["event"] != "watcher_start":
            continue
        for j in range(i + 1, len(out)):      # older entries
            if (cur["ts"] - out[j]["ts"]).total_seconds() > window_s:
                break
            if out[j]["event"] == "watcher_stop":
                drop.update((i, j))
                break

    collapsed = []
    for i, cur in enumerate(out):
        if i not in drop:
            collapsed.append(cur)
        elif cur["event"] == "watcher_start":
            collapsed.append({**cur, "event": "watcher_restart"})
    return collapsed


def activity_log(conn, aliases=None, hidden=None, limit=300):
    """Newest-first feed of arrivals and departures, for the dashboard.

    page_reload is excluded on purpose. The watcher reloads every 30 minutes to
    dodge Google's idle disconnect, and listing that would bury the handful of
    events a reader actually cares about under routine churn.
    """
    aliases = aliases or {}
    hidden = hidden or set()
    interesting = ("join", "leave", "watcher_start", "watcher_stop",
                   "disconnected", "overflow")
    placeholders = ",".join("?" * len(interesting))
    rows = conn.execute(
        f"SELECT ts, person, event FROM presence_events WHERE event IN ({placeholders}) "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (*interesting, limit),
    ).fetchall()

    raw = []
    for r in rows:
        person = canon(r["person"], aliases)
        if person != "*" and is_hidden(person, hidden):
            continue
        raw.append({
            "ts": parse(r["ts"]),
            "person": None if person == "*" else person,
            "event": r["event"],
        })

    _classify_leaves(conn, raw, aliases)
    return _collapse_restarts(_drop_repeat_joins(raw))


def print_summary(rows, flags, tz):
    if not rows:
        print("No data yet.")
        return
    w = max(len(r["person"]) for r in rows) + 2
    print(f"{'person'.ljust(w)}{'present':>10}{'active':>10}{'visits':>8}"
          f"{'edits':>8}{'edit sess':>11}  last seen")
    print("-" * (w + 60))
    for r in rows:
        last = fmt_local(r["last"], tz) if r["last"] else "-"
        print(f"{r['person'].ljust(w)}{fmt_dur(r['present_s']):>10}"
              f"{fmt_dur(r['active_s']):>10}{r['visits']:>8}"
              f"{r['edit_events']:>8}{r['edit_sessions']:>11}  {last}")
    if flags:
        kinds = defaultdict(int)
        for _, k in flags:
            kinds[k] += 1
        print("\nflags: " + ", ".join(f"{k}x{v}" for k, v in kinds.items()))
        if kinds.get("overflow"):
            print("  overflow = header collapsed some avatars into '+N'; widen the "
                  "viewport in config.json so every collaborator chip renders.")


def write_csv(path, psess, esess, tz):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["person", "type", "start_local", "end_local", "minutes", "truncated"])
        for s in sorted(psess, key=lambda x: x["start"]):
            w.writerow([s["person"], "present", fmt_local(s["start"], tz),
                        fmt_local(s["end"], tz),
                        round((s["end"] - s["start"]).total_seconds() / 60, 1),
                        int(bool(s.get("truncated")))])
        for person, blocks in esess.items():
            for a, b in blocks:
                w.writerow([person, "editing", fmt_local(a, tz), fmt_local(b, tz),
                            round((b - a).total_seconds() / 60, 1), 0])


def write_html(path, rows, psess, esess, snaps, tz):
    people = [r["person"] for r in rows]
    colors = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed",
              "#0891b2", "#be123c", "#4d7c0f"]
    cmap = {p: colors[i % len(colors)] for i, p in enumerate(people)}

    spans = []
    for s in psess:
        spans.append((s["person"], s["start"], s["end"], "present"))
    for person, blocks in esess.items():
        for a, b in blocks:
            spans.append((person, a, b, "editing"))
    if spans:
        t0 = min(s[1] for s in spans)
        t1 = max(s[2] for s in spans)
        total = max((t1 - t0).total_seconds(), 1)
    else:
        t0 = t1 = datetime.now(timezone.utc)
        total = 1

    bars = []
    for person, a, b, kind in sorted(spans, key=lambda x: x[1]):
        left = (a - t0).total_seconds() / total * 100
        width = max((b - a).total_seconds() / total * 100, 0.35)
        op = "1" if kind == "editing" else "0.32"
        bars.append(
            f'<div class="row"><span class="lbl">{html.escape(person)}</span>'
            f'<span class="track"><i style="left:{left:.3f}%;width:{width:.3f}%;'
            f'background:{cmap.get(person, "#666")};opacity:{op}" '
            f'title="{html.escape(person)} {kind} '
            f'{fmt_local(a, tz)} to {fmt_local(b, tz)}"></i></span></div>')

    trs = "".join(
        f"<tr><td>{html.escape(r['person'])}</td>"
        f"<td class=n>{fmt_dur(r['present_s'])}</td>"
        f"<td class=n>{fmt_dur(r['active_s'])}</td>"
        f"<td class=n>{r['visits']}</td>"
        f"<td class=n><b>{r['edit_events']}</b></td>"
        f"<td class=n>{r['edit_sessions']}</td>"
        f"<td>{fmt_local(r['first'], tz) if r['first'] else '-'}</td>"
        f"<td>{fmt_local(r['last'], tz) if r['last'] else '-'}</td></tr>"
        for r in rows)

    wordline = ""
    if len(snaps) > 1:
        pts = []
        wmin = min(w for _, w in snaps)
        wmax = max(max(w for _, w in snaps), wmin + 1)
        for t, wv in snaps:
            x = (t - t0).total_seconds() / total * 100
            y = 100 - (wv - wmin) / (wmax - wmin) * 100
            pts.append(f"{x:.2f},{y:.2f}")
        wordline = (f'<h2>Document length</h2><svg viewBox="0 0 100 100" '
                    f'preserveAspectRatio="none" class="spark">'
                    f'<polyline points="{" ".join(pts)}" fill="none" '
                    f'stroke="#2563eb" stroke-width="0.8"/></svg>'
                    f'<p class=muted>{wmin} to {wmax} words</p>')

    doc = f"""<!doctype html><meta charset=utf-8><title>coauthor report</title>
<style>
body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:32px;color:#111;max-width:1100px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 10px}}
.muted{{color:#666;font-size:12px}}
table{{border-collapse:collapse;width:100%;margin-top:8px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #e5e5e5}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#666}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.row{{display:flex;align-items:center;margin:3px 0}}
.lbl{{width:170px;flex:none;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.track{{position:relative;flex:1;height:15px;background:#f3f4f6;border-radius:3px}}
.track i{{position:absolute;top:2px;height:11px;border-radius:2px;display:block}}
.spark{{width:100%;height:90px;background:#fafafa;border:1px solid #eee}}
.key{{font-size:12px;color:#555;margin-top:10px}}
</style>
<h1>Document activity report</h1>
<p class=muted>{fmt_local(t0, tz)} to {fmt_local(t1, tz)} (local, UTC{tz:+d})</p>
<table><tr><th>person<th>present<th>active<th>visits<th>edit events<th>edit sessions<th>first<th>last</tr>
{trs}</table>
<p class=key><b>present</b> = client connected to the doc, including an idle tab.
<b>active</b> = cursor actually moving, so reading and scrolling counts but a parked tab does not.
<b>edit events</b> = changes Google itself attributed to that person.</p>
<h2>Timeline</h2>
<p class=muted>Solid bars are editing sessions, faded bars are presence.</p>
{''.join(bars)}
{wordline}
"""
    with open(path, "w") as f:
        f.write(doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--html")
    ap.add_argument("--csv")
    args = ap.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
    tz = int(cfg.get("tz_offset_hours", -4))
    conn = db.connect(cfg.get("db_path", os.path.join(HERE, "coauthor.sqlite3")))
    aliases, hidden = load_identity(
        cfg.get("people_map_path", os.path.join(HERE, "people_map.json")))
    beat = db.get_meta(conn, "watcher_heartbeat")

    rows, psess, esess, flags, snaps = build_summary(
        conn, aliases, hidden, parse(beat) if beat else None)
    print_summary(rows, flags, tz)
    if args.csv:
        write_csv(args.csv, psess, esess, tz)
        print(f"\nwrote {args.csv}")
    if args.html:
        write_html(args.html, rows, psess, esess, snaps, tz)
        print(f"wrote {args.html}")


if __name__ == "__main__":
    main()
