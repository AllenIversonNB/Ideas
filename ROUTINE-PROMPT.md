# ROUTINE-PROMPT.md

Reference copy of the scheduled task prompt for the Pitch Tracker.

**The live copy lives in the scheduled task itself** — this file is version control for it, not the
thing that runs. When you change one, change both, and say so in the commit message. Everything below
the rule is the prompt verbatim.

---

Update the Pitch Tracker from the AI@newbrookcapital.com Outlook mailbox, then commit to this repository.

This is for Joe Flaum, a buy-side investor at Newbrook Capital focused on TMT. The site is a permanent record of every stock pitch Newbrook receives from idea dinners, idea zooms, idea calls and similar idea-sharing events: event, date, ticker, long/short, presenter, presenter's firm, a one-line thesis, and the full pitch text. Prices and performance are NOT your job — a GitHub Action fetches them after you push.

Timezone is America/New_York (ET). Resolve today's date with `TZ=America/New_York date` at the start.

=== STEP 1 — PULL THE EMAIL ===

Use the Microsoft Outlook connector's email search tool:
- mailboxOwnerEmail = AI@newbrookcapital.com   <-- REQUIRED
- afterDateTime = 36 hours before now (ISO local timestamp). The overlap is harmless: the merge is idempotent.
- folderName = Inbox, order = newest, limit = 25, offset = 0
- Do NOT add `query` or `recipient` (refused together with mailboxOwnerEmail + dates).
- PAGINATE until the window is covered (follow nextOffset, or nextCursor as `cursor`).

If the connector errors or returns nothing, STOP and report. Never write over the store on a failed read.

=== STEP 2 — CANDIDATES ===

Read the full body of every email that could carry pitches. Bias toward inclusion:
- Monness, Crespi, Hardt (mchny.com): "MCH Idea Dinner Notes -- <Sector> -- <date>" — the notes are a PDF
  ATTACHMENT. Read the message, then read `attachments[].uri` verbatim. "Companies Discussed" emails have no
  direction — do not create ideas from them alone.
- DeMatteo Research (dmllc.com) "Morning Note" and salesperson emails: ideas are anchor links like "APP Short"
  pointing at https://go.dmllc.com/e/<n>/ideas-<TOKEN>/... and sentences like "On 10/14 we hosted a TMT Dinner
  that included investor views on APP Short, NYT Short, ...". Parse the raw HTML so you keep each href.
- Joe's own notes (sender jf@newbrookcapital.com, usually to Analysts@) such as "<Host> <Sector> dinner notes",
  including lightning rounds and replies that correct them ("Ebay was pitched short").
- Any other broker or platform email that contains the pitches themselves (ticker + direction) from an idea
  dinner/zoom/call, best-ideas event or roundtable. Invitations to a FUTURE ideas dinner are not pitches.

Read each `uri` EXACTLY as search returned it, including `?owner=AI%40newbrookcapital.com`. Bodies that are too
large get saved to a file — slice it with python in ~80k spans and read 100%.

=== STEP 3 — EXTRACT ===

Write everything to `new_ideas.json` = {"events": [...], "ideas": [...]} using the field list in CLAUDE.md.
Key rules:
- One idea per security per direction per event. Pairs: one row per leg (tag "pair: long X / short Y").
  "Dirty double" = dir Long, tag "dirty double".
- Only explicit long/short calls. No neutral discussion, no sell-side ratings, no company earnings calls.
- `d` = the day the pitch was presented (dinner date), not the email date, when they differ.
- MCH: per-idea `pr` = "Undisclosed", `fm` = ""; the event's `presenters` = the "Participating Presenters" list.
  `pitch` = the idea's full section verbatim, cleaned of PDF spacing artifacts and page headers.
- DeMatteo: `ext` = the link TOKEN (pairs: TOKEN-1, TOKEN-2). Tokens repeat daily — the merge tool de-duplicates
  on `ext`, so include every token you see; it only adds what's new. Dinner ideas go under a
  "DeMatteo <Sector> Dinner" event; otherwise a "DeMatteo Investor Idea: <TICKER> <Long|Short>" event dated the
  first time you see it. `pitch` = the sentences about it, prefixed "[YYYY-MM-DD] ".
- Joe's notes: event `notes` = the write-up's non-idea sections verbatim; anonymous participants `pr` = "Participant".
  A correction = resend that idea with `"correct": true` and the right direction.
- `th` is YOUR one-liner (<= 160 chars): what the presenter thinks the market misses and why it works.
- `yf` = Yahoo symbol (suffix map in CLAUDE.md); "" for private companies.

=== STEP 4 — MERGE, VALIDATE, PUBLISH ===

1. `NOW_ISO=$(TZ=America/New_York date +%Y-%m-%dT%H:%M:%S%:z) python3 tools/merge_ideas.py --add new_ideas.json`
   It refuses to write on bad input ("REFUSING TO WRITE") — fix the input, never bypass it.
   If nothing new was found, it writes nothing; that is a normal outcome. Report it and stop.
2. `python3 tools/test_tools.py` and `python3 tools/merge_ideas.py --report` (0 validation errors).
3. Commit `data/ideas.json docs/data/ideas.json docs/data/pitches.json` with the message
   "ideas: YYYY-MM-DD (+N new, M updated)". Never commit `new_ideas.json`.
4. `git push origin HEAD:main` — Pages serves main. If a `claude/...` branch is assigned, push main first,
   then `git push -u origin +HEAD:claude/...`. If main rejects as non-fast-forward (the price Action pushed),
   `git pull --rebase origin main` and push again — the Action only touches docs/data/prices.json. Never force-push main.
5. Verify: `git fetch origin main && git show origin/main:docs/data/ideas.json | md5sum` matches local. Retry once.

=== STEP 5 — REPORT (under 150 words) ===

- Date, emails scanned, candidates fully read
- New events (name + date) and new ideas (TICKER Long/Short), updates, corrections
- Anything you could not parse, and any `possible duplicate` pairs from --report
- Branch(es) pushed and the checksum result
