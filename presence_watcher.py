#!/usr/bin/env python3
"""
coauthor / presence_watcher
===========================

Parks a real Chrome session inside a Google Doc and records who is present.

Google only pushes collaborator presence to clients that are actually connected
to the document, so the only way to get continuous join/leave timestamps is to
keep a client connected. That is what this does. It never types, never edits,
and never touches the document content.

Usage
-----
    python presence_watcher.py --login          # one-time interactive sign-in
    python presence_watcher.py --discover       # dump DOM candidates (selector tuning)
    python presence_watcher.py                  # run the watcher

Config is read from config.json (see config.example.json).
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

import db

HERE = os.path.dirname(os.path.abspath(__file__))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("profile_dir", os.path.join(HERE, "chrome-profile"))
    cfg.setdefault("db_path", os.path.join(HERE, "coauthor.sqlite3"))
    cfg.setdefault("poll_interval_ms", 3000)
    cfg.setdefault("reload_minutes", 30)
    cfg.setdefault("headless", False)
    cfg.setdefault("extra_selectors", [])
    cfg.setdefault("viewport", {"width": 1680, "height": 1000})
    return cfg


def doc_url(cfg):
    return f"https://docs.google.com/document/d/{cfg['doc_id']}/edit"


# Google decorates the collaborator chip with a status once someone stops
# moving -- "Alice Chen (idle)". Stored verbatim, that is a second person who
# owns half of Alice's time. The page script strips these, but it only reloads
# when the watcher restarts, so the same normalisation runs here as a backstop.
# Defined in report.py so the watcher, the report and the dashboard all agree
# on what a person is called. Imported here rather than duplicated.
from report import normalize_person  # noqa: E402


# ---------------------------------------------------------------------------
# Injected page script.
#
# Google's CSS class names are minified but the *semantic* prefixes
# (docs-collaborator..., kix-cursor...) have been stable for years, so we match
# on substrings rather than exact classes, and union several sources.
# ---------------------------------------------------------------------------
PAGE_SCRIPT = r"""
(() => {
  if (window.__coauthor_installed) return;
  window.__coauthor_installed = true;

  const UI_NOISE = new Set(['share','comment','comments','history','more','menu','undo','redo',
    'print','spelling','paint format','zoom','close','present','search','help','file','edit',
    'view','insert','format','tools','extensions','add-ons','open comment history',
    'show all comments','join call','last edit was seconds ago','saving...','saved to drive']);

  function clean(s) {
    if (s === null || s === undefined) return null;
    let t = String(s).replace(/\u00a0/g, ' ').trim().replace(/\s+/g, ' ');
    if (!t) return null;
    t = t.replace(/^Profile (photo|picture) of\s+/i, '');
    t = t.replace(/^Avatar (for|of)\s+/i, '');
    t = t.replace(/\s*\(you\)\s*$/i, '');
    t = t.replace(/[''`]s (cursor|caret|profile.*)$/i, '');
    t = t.replace(/\s+is (currently )?(viewing|editing|commenting|here|in this file).*$/i, '');
    t = t.replace(/\s*[\-\u2013\u2014]\s*(viewer|editor|commenter|owner)$/i, '');
    t = t.replace(/\s*\((viewer|editor|commenter|owner)\)$/i, '');
    // Google appends an idle/away marker to the chip once someone stops moving.
    // Left in place it splits one person into two rows that each hold half the time.
    t = t.replace(/\s*\((idle|away|inactive|active|anonymous)\)$/i, '');
    t = t.replace(/\s*[\-\u2013\u2014]\s*(idle|away|inactive)$/i, '');
    t = t.trim();
    if (!t) return null;
    if (t.length > 80) return null;
    if (UI_NOISE.has(t.toLowerCase())) return null;
    if (/^\+\d+$/.test(t)) return null;              // "+3" overflow chip
    if (/^\d+$/.test(t)) return null;
    return t;
  }

  function emit(o) {
    // Callers may supply ts to backdate an event -- a debounced leave is
    // reported at the moment the person was last seen, not when we gave up.
    o.ts = o.ts || new Date().toISOString();
    try { if (window.__coauthorEmit) window.__coauthorEmit(JSON.stringify(o)); } catch (e) {}
  }

  function chipNodes() {
    return document.querySelectorAll(
      '[class*="collaborator" i],[class*="presence" i],[class*="docs-collab" i]');
  }

  // Returns name -> {chip, cursor, custom}: which signals saw this person on
  // this pass. A person can legitimately be seen by several at once, so the
  // sources are flags rather than a single first-wins label -- the old label
  // said "cursor" for everyone simply because cursors were scanned first, which
  // made the stored source useless as evidence.
  function collect() {
    const found = new Map();
    const mark = (v, src) => {
      const c = clean(v);
      if (!c) return;
      let e = found.get(c);
      if (!e) { e = { chip: false, cursor: false, custom: false }; found.set(c, e); }
      e[src] = true;
    };

    // 1. live cursors. Proof the person is actively moving, but transient:
    //    Google stops rendering a remote cursor within seconds of it going
    //    still, so this must never be used to decide that someone has left.
    document.querySelectorAll('.kix-cursor, [class*="kix-cursor"]').forEach(el => {
      const n = el.querySelector('[class*="cursor-name"], [class*="cursorname"]');
      mark(n ? n.textContent : el.getAttribute('aria-label'), 'cursor');
    });

    // 2. avatar chips in the header. The durable signal: measured over a five
    //    minute run, the chip persists through switching tabs and applications
    //    and disappears only when the document is genuinely closed.
    chipNodes().forEach(el => {
      mark(el.getAttribute('data-tooltip'), 'chip');
      mark(el.getAttribute('aria-label'), 'chip');
      mark(el.getAttribute('title'), 'chip');
    });

    // 3. user-supplied selectors from config
    (window.__coauthor_extra_selectors || []).forEach(sel => {
      try {
        document.querySelectorAll(sel).forEach(el => {
          mark(el.getAttribute('data-tooltip') || el.getAttribute('aria-label')
               || el.getAttribute('title') || el.textContent, 'custom');
        });
      } catch (e) {}
    });

    return found;
  }

  // Cursor screen position per person -> changes mean the person is actually
  // moving/typing, as opposed to leaving a tab open.
  function cursorFingerprint() {
    const m = {};
    document.querySelectorAll('.kix-cursor, [class*="kix-cursor"]').forEach(el => {
      const n = el.querySelector('[class*="cursor-name"], [class*="cursorname"]');
      const name = clean(n ? n.textContent : null);
      if (!name) return;
      const st = el.style || {};
      m[name] = (st.top || '') + '|' + (st.left || '') + '|' + (st.height || '');
    });
    return m;
  }

  window.__coauthor_dump = function () {
    const out = [];
    chipNodes().forEach(el => out.push({
      kind: 'chip', cls: el.className && el.className.toString().slice(0, 160),
      tag: el.tagName,
      tooltip: el.getAttribute('data-tooltip'),
      aria: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      text: (el.textContent || '').trim().slice(0, 60)
    }));
    document.querySelectorAll('[class*="kix-cursor"]').forEach(el => out.push({
      kind: 'cursor', cls: el.className && el.className.toString().slice(0, 160),
      aria: el.getAttribute('aria-label'),
      text: (el.textContent || '').trim().slice(0, 60)
    }));
    return JSON.stringify({ resolved: Array.from(collect().keys()), nodes: out }, null, 2);
  };

  // name -> timestamp we last actually saw them.
  //
  // Emitting a leave the moment somebody misses one poll produces constant
  // false departures: Google hides a collaborator's live cursor after a couple
  // of seconds of inactivity, and the avatar chip is not always matched on
  // every pass, so a person sitting and reading flickers out and back. Instead
  // hold them present until they have been missing for the whole grace period,
  // and backdate the leave to when we last saw them so the session length
  // stays honest.
  let present = new Map();
  let prevCursors = {};
  let lastDisconnectEmit = 0;
  const LEAVE_GRACE_MS = window.__coauthor_leave_grace_ms || 45000;

  function tick() {
    try {
      const map = collect();
      const now = Date.now();

      // Who counts as "in the document".
      //
      // Chips when chips are working, because they track the connection rather
      // than the mouse: someone reading, alt-tabbed or in another window keeps
      // their chip, and it disappears when they actually close the tab. Cursors
      // vanish after seconds of stillness, so letting them decide departures
      // reported every quiet moment as leaving and coming back.
      //
      // If no chip resolves at all -- Google renames its classes now and then
      // -- fall back to cursors so the watcher degrades instead of going blind.
      const chipNames = new Set();
      for (const [p, s] of map) if (s.chip || s.custom) chipNames.add(p);
      const authoritative = chipNames.size ? chipNames : new Set(map.keys());
      const degraded = chipNames.size === 0 && map.size > 0;

      for (const p of authoritative) {
        if (!present.has(p)) {
          const s = map.get(p) || {};
          emit({ event: 'join', person: p,
                 source: s.chip ? 'chip' : (s.custom ? 'custom' : 'cursor') });
        }
        present.set(p, now);
      }
      // A wider grace while degraded, since cursors flicker by nature.
      const grace = degraded ? Math.max(LEAVE_GRACE_MS, 45000) : LEAVE_GRACE_MS;
      for (const [p, seen] of Array.from(present)) {
        if (!authoritative.has(p) && now - seen > grace) {
          emit({ event: 'leave', person: p, source: degraded ? 'poll-degraded' : 'chip',
                 ts: new Date(seen).toISOString() });
          present.delete(p);
        }
      }

      const fp = cursorFingerprint();
      for (const k in fp) if (prevCursors[k] !== fp[k]) emit({ event: 'cursor', person: k, source: 'cursor' });
      prevCursors = fp;

      // collapsed "+N" chip: more collaborators than the header can render
      const nodes = Array.from(chipNodes());
      for (const el of nodes) {
        const t = ((el.getAttribute('aria-label') || el.textContent) || '').trim();
        if (/^\+\d+$/.test(t)) { emit({ event: 'overflow', person: '*', extra: t }); break; }
      }

      const txt = document.body ? (document.body.innerText || '') : '';
      if (/Trying to connect|You are offline|Reconnecting|Document is not saved|couldn't (load|open) the file/i.test(txt)) {
        const n = Date.now();
        if (n - lastDisconnectEmit > 60000) { lastDisconnectEmit = n; emit({ event: 'disconnected', person: '*' }); }
      }
    } catch (e) {
      emit({ event: 'jserror', person: '*', extra: String(e).slice(0, 200) });
    }
  }

  setInterval(tick, window.__coauthor_interval_ms || 3000);
  setTimeout(tick, 4000);   // first read once the editor has painted
})();
"""


# How long the page may sit disconnected before it is reloaded to recover. Long
# enough that an ordinary blip resolves itself, short enough that presence is
# not frozen for the half-hour until the next scheduled reload.
DISCONNECT_RECOVER_S = 75


class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = db.connect(cfg["db_path"])
        self.running = True
        self.seen = 0
        self.disconnected_since = None

    def set_error(self, kind, message):
        """Record why the watcher cannot run, for the dashboard to show.

        Exiting non-zero stops the heartbeat, so the dashboard already knows
        something is wrong -- but "tracking is paused" gives a reader no way to
        tell a crashed process from an expired Google session that needs one
        person to sign in again. Store the reason so it can say which.
        """
        try:
            db.set_meta(self.conn, "watcher_error",
                        json.dumps({"kind": kind, "message": message, "ts": now_iso()}))
        except Exception as e:
            print(f"could not record error state: {e}", file=sys.stderr)

    def clear_error(self):
        try:
            db.set_meta(self.conn, "watcher_error", "")
        except Exception:
            pass

    def on_event(self, payload):
        try:
            ev = json.loads(payload)
        except Exception:
            return
        person = normalize_person(ev.get("person") or "*")
        kind = ev.get("event") or "?"
        db.add_presence(
            self.conn,
            ev.get("ts") or now_iso(),
            person,
            kind,
            ev.get("source"),
            ev.get("extra"),
        )
        self.seen += 1

        # A disconnected page keeps rendering whoever was on screen when the
        # connection dropped, so presence freezes: no cursors, no departures,
        # and everybody stays listed forever. Note when it started so the main
        # loop can reload and recover, and clear it as soon as anything moves,
        # which only a live connection can produce.
        if kind == "disconnected":
            if self.disconnected_since is None:
                self.disconnected_since = time.time()
        elif kind in ("join", "leave", "cursor"):
            self.disconnected_since = None

        if kind in ("join", "leave", "overflow", "disconnected", "jserror"):
            print(f"[{ev.get('ts')}] {kind:<12} {person}"
                  + (f"  ({ev.get('extra')})" if ev.get("extra") else ""), flush=True)

    def run(self, discover=False, login=False):
        cfg = self.cfg
        os.makedirs(cfg["profile_dir"], exist_ok=True)

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=cfg["profile_dir"],
                headless=False if (login or discover) else cfg["headless"],
                viewport=cfg["viewport"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate",
                ],
            )

            if login:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://accounts.google.com/")
                print("\nSign in to the Google account that has access to the document.")
                print("Complete 2FA if prompted. When the doc opens fine, press Enter here.\n")
                try:
                    input()
                except EOFError:
                    time.sleep(300)
                page.goto(doc_url(cfg))
                print("Loaded the doc. Press Enter again to save the profile and exit.")
                try:
                    input()
                except EOFError:
                    pass
                ctx.close()
                return

            ctx.expose_function("__coauthorEmit", lambda payload: self.on_event(payload))
            ctx.add_init_script(
                f"window.__coauthor_interval_ms = {int(cfg['poll_interval_ms'])};"
                f"window.__coauthor_leave_grace_ms = "
                f"{int(cfg.get('leave_grace_seconds', 45)) * 1000};"
                f"window.__coauthor_extra_selectors = {json.dumps(cfg['extra_selectors'])};"
            )
            ctx.add_init_script(PAGE_SCRIPT)

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(doc_url(cfg), wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(8000)

            if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
                print("Not signed in. Run with --login first.", file=sys.stderr)
                self.set_error("signed_out",
                               "The tracking account has been signed out by Google. "
                               "It needs signing in again before presence can be recorded.")
                ctx.close()
                sys.exit(2)

            # Sitting on the document URL is not proof of access. Google serves
            # "You need access" from docs.google.com with a normal 200, so the
            # check above passes and the watcher happily announces that it is
            # watching while recording nothing at all, for as long as it runs.
            # Wait for the editor itself, and only then trust the page.
            try:
                page.wait_for_selector(
                    ".kix-appview-editor, .docs-texteventtarget-iframe", timeout=15_000)
            except Exception:
                title = (page.title() or "").strip()
                body = page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 2000)")
                if "access denied" in title.lower() or "you need access" in body.lower():
                    print(f"No access to this document as the signed-in account "
                          f"(page title: {title!r}).\n"
                          f"Share the document with that account, or re-run with "
                          f"--login to sign in as one that has access.",
                          file=sys.stderr)
                    self.set_error("no_access",
                                   "The tracking account cannot open this document. "
                                   "It needs to be shared with that account.")
                    ctx.close()
                    sys.exit(3)
                print(f"Editor did not appear (page title: {title!r}); "
                      f"continuing anyway.", file=sys.stderr)

            if discover:
                dump = page.evaluate("() => window.__coauthor_dump ? window.__coauthor_dump() : 'not installed'")
                print(dump)
                ctx.close()
                return

            self.clear_error()          # got this far, so whatever it was is over
            db.add_presence(self.conn, now_iso(), "*", "watcher_start", "python")
            print(f"Watching {doc_url(cfg)}\nCtrl-C to stop.\n", flush=True)

            def stop(*_):
                self.running = False
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)

            last_reload = time.time()
            reload_after = cfg["reload_minutes"] * 60
            last_beat = 0.0

            while self.running:
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    break

                # Liveness signal. Presence rows are only written when something
                # changes, so an empty document produces silence that is
                # indistinguishable from a dead watcher. This says "still here"
                # regardless. It lives in meta rather than presence_events so it
                # updates one row instead of adding ~2900 a day.
                if time.time() - last_beat > 30:
                    try:
                        db.set_meta(self.conn, "watcher_heartbeat", now_iso())
                        last_beat = time.time()
                    except Exception as e:
                        print(f"heartbeat failed: {e}", file=sys.stderr)

                # Keep the session alive without touching content: a pointer move
                # over the header area. No keystrokes, no clicks in the body.
                if int(time.time()) % 240 == 0:
                    try:
                        page.mouse.move(40, 12)
                    except Exception:
                        pass

                # Recover from a dropped connection. Until this existed the page
                # could sit disconnected until the next scheduled reload half an
                # hour later, with the collaborator list frozen the whole time,
                # so whoever happened to be on screen stayed "here now" long
                # after leaving and nobody new was ever seen.
                stuck = (self.disconnected_since is not None
                         and time.time() - self.disconnected_since > DISCONNECT_RECOVER_S)

                if stuck or time.time() - last_reload > reload_after:
                    if stuck:
                        print(f"disconnected for "
                              f"{int(time.time() - self.disconnected_since)}s; reloading",
                              file=sys.stderr)
                    db.add_presence(self.conn, now_iso(), "*", "page_reload", "python")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=120_000)
                        page.wait_for_timeout(6000)
                    except Exception as e:
                        print(f"reload failed: {e}", file=sys.stderr)
                    last_reload = time.time()
                    self.disconnected_since = None

            db.add_presence(self.conn, now_iso(), "*", "watcher_stop", "python")
            print(f"\nStopped. {self.seen} events recorded to {cfg['db_path']}")
            ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--login", action="store_true", help="interactive one-time sign-in")
    ap.add_argument("--discover", action="store_true", help="dump DOM candidates and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    Watcher(cfg).run(discover=args.discover, login=args.login)


if __name__ == "__main__":
    main()
