#!/usr/bin/env python3
"""Canonical merge for the ideas store. All event and idea identity lives here.

    NOW_ISO=$(TZ=America/New_York date +%Y-%m-%dT%H:%M:%S%:z) \
      python3 tools/merge_ideas.py --add new_ideas.json

Input file:  {"events": [...], "ideas": [...]}  -- field list in CLAUDE.md.
Incoming events carry a file-local `key`; incoming ideas point at it with `ev_key`.

Writes three files:
  data/ideas.json          canonical store (full text). The only file a run should hand-edit -- and it shouldn't.
  docs/data/ideas.json     what the page loads: everything except pitch text and event notes
  docs/data/pitches.json   {"ideas": {id: pitch}, "events": {id: notes}}, lazy-loaded by the detail view

Rules:
  * An incoming idea matches an existing one by `ext` (external id, e.g. a DeMatteo link token), then by id/aka,
    then by (event, ticker, direction). A match merges; nothing is ever deleted.
  * Merging keeps the fuller value of every field: the longer pitch/thesis, sources unioned, empty fields filled.
    An incoming idea with "correct": true overwrites fields instead (used for corrections such as
    "eBay was pitched short"); it matches on (event, ticker) ignoring direction, and the retired id goes to `aka`.
  * When an `ext` match carries an earlier date, the earlier date and its event win (first time we were pitched).
  * The tool refuses to write if validation fails or if the idea count would drop.
"""
import argparse
import copy
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "data", "ideas.json")
PUB = os.path.join(ROOT, "docs", "data", "ideas.json")
PITCHES = os.path.join(ROOT, "docs", "data", "pitches.json")

EVENT_FIELDS = ["id", "aka", "d", "name", "host", "series", "fmt", "focus", "loc", "presenters",
                "summary", "notes", "src", "seen", "upd"]
IDEA_FIELDS = ["id", "aka", "ev", "d", "tk", "yf", "co", "dir", "tag", "pr", "fm", "th", "pt", "px_ref",
               "mcap_ref", "cat", "pitch", "ext", "src", "flag", "seen", "upd"]
# Text fields where the longer value wins on a plain merge.
LONGER_WINS = {"pitch", "th", "notes", "summary", "presenters", "cat", "pt"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def slug(s, n=None):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    if n:
        s = s[:n].strip("-")
    return s


def norm_tk(tk):
    return re.sub(r"\s+", " ", (tk or "").strip().upper())


def event_id(e):
    return "-".join(p for p in [e["d"], slug(e.get("host"), 14), slug(e.get("name"), 48)] if p)


def idea_id(i):
    return f'{i["ev"]}--{slug(norm_tk(i["tk"]))}-{i["dir"][0].lower()}'


def union_src(a, b):
    parts = []
    for s in (a or "").split(" || ") + (b or "").split(" || "):
        s = s.strip()
        if s and s not in parts:
            parts.append(s)
    return " || ".join(parts)


def merge_fields(cur, inc, fields, correct=False):
    changed = False
    for f in fields:
        if f in ("id", "aka", "seen", "upd", "ev", "d"):
            continue
        v = inc.get(f)
        if v in (None, "", []):
            continue
        old = cur.get(f)
        if f == "src":
            new = union_src(old, v)
        elif correct or old in (None, "", []):
            new = v
        elif f in LONGER_WINS and isinstance(v, str) and len(v) > len(str(old)):
            new = v
        else:
            new = old
        if new != old:
            cur[f] = new
            changed = True
    return changed


def load_store():
    if not os.path.exists(STORE):
        return {"updated": None, "events": [], "ideas": []}
    with open(STORE, encoding="utf-8") as fh:
        return json.load(fh)


def validate(store):
    errs = []
    evs = {}
    for e in store["events"]:
        for f in ("id", "d", "name", "host"):
            if not e.get(f):
                errs.append(f"event {e.get('id') or e.get('name')}: missing {f}")
        if e.get("d") and not DATE_RE.match(e["d"]):
            errs.append(f"event {e.get('id')}: bad date {e['d']!r}")
        if e.get("id") in evs:
            errs.append(f"duplicate event id {e['id']}")
        evs[e.get("id")] = e
    seen = set()
    for i in store["ideas"]:
        tag = i.get("id") or f'{i.get("tk")}@{i.get("ev")}'
        for f in ("id", "ev", "d", "tk", "co", "dir", "th"):
            if not i.get(f):
                errs.append(f"idea {tag}: missing {f}")
        if i.get("dir") not in ("Long", "Short"):
            errs.append(f"idea {tag}: dir must be Long or Short, got {i.get('dir')!r}")
        if i.get("d") and not DATE_RE.match(i["d"]):
            errs.append(f"idea {tag}: bad date {i['d']!r}")
        if i.get("ev") not in evs:
            errs.append(f"idea {tag}: unknown event {i.get('ev')!r}")
        if i.get("id") in seen:
            errs.append(f"duplicate idea id {i['id']}")
        seen.add(i.get("id"))
        if "px_ref" in i and i["px_ref"] not in (None, ""):
            try:
                float(i["px_ref"])
            except (TypeError, ValueError):
                errs.append(f"idea {tag}: px_ref must be a number, got {i['px_ref']!r}")
    return errs


def clean(rec, fields):
    out = {}
    for f in fields:
        v = rec.get(f)
        if v not in (None, "", []):
            out[f] = v
    return out


def merge(store, inc, now):
    stats = {"ev_new": 0, "ev_upd": 0, "new": 0, "upd": 0, "corrected": 0}
    events = {e["id"]: e for e in store["events"]}
    ev_alias = {}
    for e in store["events"]:
        for a in e.get("aka", []):
            ev_alias[a] = e["id"]
    # Same date + same series + same name (case/punct-insensitive) is the same event.
    ev_natural = {(e["d"], slug(e.get("series") or ""), slug(e["name"])): e["id"] for e in store["events"]}

    keymap = {}
    created = set()
    for raw in inc.get("events", []):
        e = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        if not e.get("d") or not e.get("name") or not e.get("host"):
            raise SystemExit(f"REFUSING TO WRITE: incoming event missing d/name/host: {raw.get('key') or raw}")
        eid = event_id(e)
        nat = (e["d"], slug(e.get("series") or ""), slug(e["name"]))
        target = events.get(eid) or events.get(ev_alias.get(eid, "")) or events.get(ev_natural.get(nat, ""))
        if target:
            if merge_fields(target, e, EVENT_FIELDS):
                target["upd"] = now
                stats["ev_upd"] += 1
        else:
            target = clean(e, EVENT_FIELDS)
            target["id"] = eid
            target["seen"] = now
            target["upd"] = now
            events[eid] = target
            ev_natural[nat] = eid
            created.add(eid)
            stats["ev_new"] += 1
        if raw.get("key"):
            keymap[raw["key"]] = target["id"]

    ideas = store["ideas"]
    by_id = {i["id"]: i for i in ideas}
    by_ext = {i["ext"]: i for i in ideas if i.get("ext")}
    alias = {a: i for i in ideas for a in i.get("aka", [])}

    def natural(ev, tk, d=None):
        return [i for i in ideas if i["ev"] == ev and norm_tk(i["tk"]) == tk and (d is None or i["dir"] == d)]

    for raw in inc.get("ideas", []):
        i = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        ev = keymap.get(i.get("ev_key")) or i.get("ev")
        if ev in ev_alias:
            ev = ev_alias[ev]
        if not ev or ev not in events:
            raise SystemExit(f"REFUSING TO WRITE: idea {i.get('tk')} points at unknown event {i.get('ev_key') or i.get('ev')!r}")
        i["ev"] = ev
        i.pop("ev_key", None)
        i["tk"] = norm_tk(i.get("tk"))
        if i.get("dir"):
            i["dir"] = i["dir"].strip().capitalize()
        if "yf" in i and i["yf"] is not None:
            i["yf"] = i["yf"].strip().upper()
        correct = bool(i.pop("correct", False))
        if i.get("dir") not in ("Long", "Short"):
            raise SystemExit(f"REFUSING TO WRITE: idea {i.get('tk')} has dir {i.get('dir')!r}")
        nid = idea_id(i)

        cur = None
        if i.get("ext") and i["ext"] in by_ext:
            cur = by_ext[i["ext"]]
        if cur is None:
            cur = by_id.get(nid) or alias.get(nid)
        if cur is None:
            m = natural(ev, i["tk"], None if correct else i["dir"])
            cur = m[0] if m else None

        if cur is None:
            rec = clean(i, IDEA_FIELDS)
            rec["id"] = nid
            if not rec.get("d"):
                rec["d"] = events[ev]["d"]
            if "yf" not in rec and "yf" in i:  # explicit "" means unlisted; keep the marker
                rec["yf"] = ""
            rec["seen"] = now
            rec["upd"] = now
            ideas.append(rec)
            by_id[nid] = rec
            if rec.get("ext"):
                by_ext[rec["ext"]] = rec
            stats["new"] += 1
            continue

        changed = merge_fields(cur, i, IDEA_FIELDS, correct=correct)
        # An ext match with an earlier date: the earliest pitch wins (date and event move together).
        if i.get("ext") and i.get("d") and i["d"] < cur["d"]:
            cur["d"], cur["ev"] = i["d"], ev
            changed = True
        if correct and i.get("dir") and i["dir"] != cur["dir"]:
            cur["dir"] = i["dir"]
            changed = True
        new_id = idea_id(cur)
        if new_id != cur["id"]:
            cur.setdefault("aka", [])
            if cur["id"] not in cur["aka"]:
                cur["aka"].append(cur["id"])
            by_id.pop(cur["id"], None)
            cur["id"] = new_id
            by_id[new_id] = cur
        if changed:
            cur["upd"] = now
            stats["corrected" if correct else "upd"] += 1

    # A re-listed idea (e.g. DeMatteo's daily list) folds into its first event, and an idea that moves to an
    # earlier date leaves its old event behind. Drop events left with no ideas -- unless they carry notes.
    used = {i["ev"] for i in ideas}
    for eid in [k for k, e in events.items() if k not in used and not e.get("notes")]:
        del events[eid]
        if eid in created:
            stats["ev_new"] -= 1
    store["events"] = sorted(events.values(), key=lambda e: (e["d"], e["id"]), reverse=True)
    store["ideas"] = sorted(ideas, key=lambda i: (i["d"], i["ev"], i["tk"]), reverse=True)
    return stats


def possible_dups(store):
    """Same ticker + same direction pitched within 3 days at different events -- for a human to judge."""
    out = []
    ideas = sorted(store["ideas"], key=lambda i: (i["tk"], i["dir"], i["d"]))
    for a, b in zip(ideas, ideas[1:]):
        if a["tk"] == b["tk"] and a["dir"] == b["dir"] and a["ev"] != b["ev"]:
            gap = (dt.date.fromisoformat(b["d"]) - dt.date.fromisoformat(a["d"])).days
            if gap <= 3:
                out.append((a["id"], b["id"]))
    return out


def write_all(store, now):
    store["updated"] = now
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    os.makedirs(os.path.dirname(PUB), exist_ok=True)
    pub_events, pub_ideas = [], []
    pitches = {"ideas": {}, "events": {}}
    for e in store["events"]:
        pe = {k: v for k, v in e.items() if k not in ("notes", "aka")}
        if e.get("notes"):
            pe["has_notes"] = 1
            pitches["events"][e["id"]] = e["notes"]
        pub_events.append(pe)
    for i in store["ideas"]:
        pi = {k: v for k, v in i.items() if k not in ("pitch", "aka")}
        if i.get("pitch"):
            pitches["ideas"][i["id"]] = i["pitch"]
        pub_ideas.append(pi)
    for path, obj, indent in ((STORE, store, 1), (PUB, {"updated": now, "events": pub_events, "ideas": pub_ideas}, 0),
                              (PITCHES, pitches, 0)):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent if indent else None,
                      separators=None if indent else (",", ":"))
            fh.write("\n")
        with open(tmp, encoding="utf-8") as fh:
            json.load(fh)
        os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", action="append", default=[], help="JSON file of new events/ideas (repeatable)")
    ap.add_argument("--report", action="store_true", help="print store stats and possible duplicates")
    ap.add_argument("--build", action="store_true", help="rewrite docs/data from data/ideas.json without adding")
    args = ap.parse_args()
    now = os.environ.get("NOW_ISO") or dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")

    store = load_store()
    before = len(store["ideas"])
    if args.add:
        snapshot = copy.deepcopy(store)
        total = {}
        for path in args.add:
            with open(path, encoding="utf-8") as fh:
                inc = json.load(fh)
            for k, v in merge(store, inc, now).items():
                total[k] = total.get(k, 0) + v
        errs = validate(store)
        if errs:
            print("REFUSING TO WRITE: validation failed:", file=sys.stderr)
            for e in errs[:40]:
                print("  " + e, file=sys.stderr)
            sys.exit(2)
        if len(store["ideas"]) < before:
            print(f"REFUSING TO WRITE: idea count would drop {before} -> {len(store['ideas'])}", file=sys.stderr)
            sys.exit(2)
        if store != snapshot:
            write_all(store, now)
        print(f"events +{total.get('ev_new', 0)} new, {total.get('ev_upd', 0)} updated | "
              f"ideas +{total.get('new', 0)} new, {total.get('upd', 0)} updated, {total.get('corrected', 0)} corrected | "
              f"store: {len(store['events'])} events, {len(store['ideas'])} ideas")
    elif args.build:
        errs = validate(store)
        if errs:
            print("REFUSING TO WRITE:", *errs[:40], sep="\n  ", file=sys.stderr)
            sys.exit(2)
        write_all(store, store.get("updated") or now)
    if args.report or not (args.add or args.build):
        errs = validate(store)
        dups = possible_dups(store)
        longs = sum(1 for i in store["ideas"] if i["dir"] == "Long")
        print(f"{len(store['events'])} events, {len(store['ideas'])} ideas ({longs} long, {len(store['ideas']) - longs} short)")
        print(f"{len(errs)} validation errors")
        for e in errs[:20]:
            print("  " + e)
        print(f"{len(dups)} possible duplicate pairs (same ticker+direction within 3 days, different events)")
        for a, b in dups[:30]:
            print(f"  {a}  <->  {b}")


if __name__ == "__main__":
    main()
