# Pitch Tracker

Every stock pitch Newbrook has received from idea dinners, idea zooms and idea calls, with performance since the pitch.

- **Site:** GitHub Pages from `docs/` on `main`.
- **Ideas:** added daily by a Claude Code routine that reads the AI@newbrookcapital.com mailbox (`ROUTINE-PROMPT.md`).
- **Prices:** the "Update prices" Action (`.github/workflows/prices.yml`) fetches Yahoo Finance data each weekday after the close, and whenever new ideas land.
- **Developer notes:** `CLAUDE.md`. Tests: `python3 tools/test_tools.py`.
