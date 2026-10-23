#!/usr/bin/env python3
"""Fetch prices and compute pitch performance. Runs in the GitHub Action (Yahoo is not reachable from
Claude's sandbox), writes docs/data/prices.json, never touches the ideas store.

    pip install yfinance && python3 tools/update_prices.py

Conventions (also shown in the page footer):
  * Pitch price  = close on the pitch date, or the last close before it if the market was shut.
  * T+30 / T+90  = first close on or after pitch date + 30 / 90 calendar days; blank until that day has traded.
  * Returns use dividend- and split-adjusted closes; displayed prices are split-adjusted closes.
  * Performance is sign-flipped for shorts, so positive always means the pitch worked.
  * Alpha       = performance relative to SPY over the same window (long: r - spy, short: spy - r).
  * Market cap  = shares outstanding x price, converted to USD. Cap at pitch scales today's cap by the
                  price change (assumes a constant share count).
"""
import bisect
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "data", "ideas.json")
OUT = os.path.join(ROOT, "docs", "data", "prices.json")
BENCH = "SPY"
ET = ZoneInfo("America/New_York")
META_MAX_AGE_DAYS = 7


# ---------------------------------------------------------------- pure computation (unit-tested)

class Series:
    """Daily closes for one symbol: parallel sorted lists of ISO dates, closes, adjusted closes."""

    def __init__(self, rows):
        rows = sorted((d, c, a) for d, c, a in rows if c is not None and a is not None)
        self.d = [r[0] for r in rows]
        self.c = [r[1] for r in rows]
        self.a = [r[2] for r in rows]

    def __bool__(self):
        return bool(self.d)

    def on_or_before(self, day):
        k = bisect.bisect_right(self.d, day) - 1
        return k if k >= 0 else None

    def on_or_after(self, day):
        k = bisect.bisect_left(self.d, day)
        return k if k < len(self.d) else None


def plus_days(day, n):
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def window(s, b, k0, k1):
    """Return of s from index k0 to k1, and the benchmark over the same dates."""
    r = s.a[k1] / s.a[k0] - 1
    rb = None
    if b:
        j0, j1 = b.on_or_before(s.d[k0]), b.on_or_before(s.d[k1])
        if j0 is not None and j1 is not None:
            rb = b.a[j1] / b.a[j0] - 1
    return r, rb


def perf(idea, s, b):
    """Performance record for one idea, or None when there is no price on/before the pitch date."""
    if not s:
        return None
    k0 = s.on_or_before(idea["d"])
    if k0 is None:
        # Pitched before the symbol's first trade (e.g. pre-IPO): start at the first close.
        k0 = s.on_or_after(idea["d"])
        if k0 is None:
            return None
    sign = -1 if idea["dir"] == "Short" else 1
    last = len(s.d) - 1
    out = {"px0": round(s.c[k0], 4), "px0_d": s.d[k0], "px": round(s.c[last], 4), "px_d": s.d[last]}

    def put(suffix, k1):
        r, rb = window(s, b, k0, k1)
        out["r" + suffix] = round(r, 5)
        out["p" + suffix] = round(sign * r, 5)
        if rb is not None:
            out["a" + suffix] = round(sign * (r - rb), 5)

    put("", last)
    for n in (30, 90):
        target = plus_days(idea["d"], n)
        k1 = s.on_or_after(target)
        if k1 is not None:
            put(str(n), k1)
            out[f"d{n}"] = s.d[k1]
        else:
            out[f"d{n}"] = target  # still pending; the page shows the countdown
    return out


def usd_mcap(meta, px_now, fx):
    """Market cap in USD from shares x price (price in quote currency), or Yahoo's marketCap as fallback."""
    cur = meta.get("cur") or "USD"
    scale = 1.0
    if cur in ("GBp", "GBX", "ZAc", "ILA"):  # quoted in minor units
        scale, cur = 0.01, {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}[cur]
    rate = 1.0 if cur == "USD" else fx.get(cur)
    if rate is None:
        return None
    if meta.get("shares") and px_now:
        return meta["shares"] * px_now * scale * rate
    if meta.get("mcap_local"):
        return meta["mcap_local"] * rate  # Yahoo reports marketCap in major units
    return None


def build(ideas, series, meta, fx, bench):
    out = {}
    for i in ideas:
        sym = i.get("yf")
        if not sym:
            continue
        rec = perf(i, series.get(sym), bench)
        if not rec:
            continue
        m = meta.get(sym, {})
        cap = usd_mcap(m, rec["px"], fx)
        if cap:
            rec["mcap"] = round(cap)
            rec["mcap0"] = round(cap * rec["px0"] / rec["px"]) if rec["px"] else None
        out[i["id"]] = rec
    return out


# ---------------------------------------------------------------- fetching (Action only)

def is_us(sym):
    """US listings have no exchange suffix (class shares use '-', e.g. BRK-B); FX pairs end in '=X'."""
    return "." not in sym and not sym.endswith("=X")


def last_complete_day(now_et):
    """Latest date whose bar may be stored. Before 16:30 ET today's US session is still trading and Yahoo's
    'today' bar is an intraday price, not a close; a bar dated after today is an Asian session in progress."""
    today = now_et.date()
    if now_et.time() < dt.time(16, 30):
        return (today - dt.timedelta(days=1)).isoformat()
    return today.isoformat()


def fetch_series(yf, symbols, start, now_et=None):
    """Daily closes per symbol. US, non-US and FX symbols are downloaded in separate batches: mixing exchanges
    in one download misaligned dates (2026-10-22: most US names came back a day stale). Bars after the last
    completed session are dropped so a mid-session price is never stored as a close."""
    import pandas as pd  # noqa: F401  (yfinance dependency)
    now_et = now_et or dt.datetime.now(ET)
    cutoff = last_complete_day(now_et)
    groups = [[s for s in symbols if is_us(s)], [s for s in symbols if not is_us(s) and not s.endswith("=X")],
              [s for s in symbols if s.endswith("=X")]]
    got = {}
    for group in groups:
        for chunk in [group[k:k + 80] for k in range(0, len(group), 80)]:
            df = yf.download(chunk, start=start, auto_adjust=False, actions=False, group_by="ticker",
                             threads=True, progress=False)
            for sym in chunk:
                try:
                    # group_by="ticker" gives (symbol, field) columns -- recent yfinance does so even for one symbol.
                    multi = getattr(df.columns, "nlevels", 1) > 1
                    if multi and sym not in df.columns.get_level_values(0):
                        raise KeyError(sym)
                    sub = df[sym] if multi else df
                    rows = [(ix.date().isoformat(), float(c), float(a))
                            for ix, c, a in zip(sub.index, sub["Close"], sub["Adj Close"])
                            if c == c and a == a and ix.date().isoformat() <= cutoff]
                except (KeyError, TypeError, AttributeError):
                    rows = []
                if rows:
                    got[sym] = Series(rows)
    return got


def refetch_stragglers(yf, series, start, now_et=None):
    """US names whose last bar is behind SPY's are re-downloaded once; a newer answer replaces the old one."""
    bench = series.get(BENCH)
    if not bench:
        return []
    behind = [s for s, ser in series.items() if s != BENCH and is_us(s) and ser.d[-1] < bench.d[-1]]
    if not behind:
        return []
    again = fetch_series(yf, behind, start, now_et)
    fixed = [s for s in behind if s in again and again[s].d[-1] > series[s].d[-1]]
    for s in fixed:
        series[s] = again[s]
    return [s for s in behind if s not in fixed]


def fetch_meta(yf, sym):
    info = yf.Ticker(sym).info or {}
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "cur": info.get("currency"),
        "ex": info.get("exchange"),
        "shares": info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"),
        "mcap_local": info.get("marketCap"),
        "fetched": dt.date.today().isoformat(),
    }


def fetch_fx(yf, currencies):
    """USD per unit of each currency, from Yahoo's <CUR>USD=X pairs."""
    need = sorted({c for c in currencies if c and c != "USD"})
    if not need:
        return {}
    got = fetch_series(yf, [f"{c}USD=X" for c in need], (dt.date.today() - dt.timedelta(days=10)).isoformat())
    return {c: got[f"{c}USD=X"].c[-1] for c in need if f"{c}USD=X" in got}


def main():
    import yfinance as yf

    with open(STORE, encoding="utf-8") as fh:
        ideas = json.load(fh)["ideas"]
    prev = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as fh:
            prev = json.load(fh)

    syms = sorted({i["yf"] for i in ideas if i.get("yf")})
    if not syms:
        print("No priced symbols yet; nothing to do.")
        return
    start = plus_days(min(i["d"] for i in ideas), -10)
    series = fetch_series(yf, syms + [BENCH], start)
    bench = series.get(BENCH)
    if not bench:
        sys.exit(f"REFUSING TO WRITE: no {BENCH} data (Yahoo unreachable?)")
    if prev.get("asof") and bench.d[-1] < prev["asof"]:
        sys.exit(f"REFUSING TO WRITE: Yahoo's latest {BENCH} bar is {bench.d[-1]}, older than the published "
                 f"{prev['asof']}. Leaving the page as it is; the next run will retry.")
    stale = refetch_stragglers(yf, series, start)
    if stale:
        print(f"{len(stale)} US symbols still behind {BENCH} ({bench.d[-1]}) after a retry: {', '.join(stale[:30])}")
    prev_ok = len(prev.get("ideas", {}))
    missing = [s for s in syms if s not in series]

    meta = dict(prev.get("meta", {}))
    today = dt.date.today()
    for s in syms:
        m = meta.get(s)
        stale = not m or (today - dt.date.fromisoformat(m.get("fetched", "1970-01-01"))).days >= META_MAX_AGE_DAYS
        if stale and s in series:
            try:
                meta[s] = fetch_meta(yf, s)
            except Exception as exc:  # one bad symbol must not sink the run
                print(f"meta failed for {s}: {exc}", file=sys.stderr)
            time.sleep(0.25)
    minor = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}
    fx = fetch_fx(yf, [minor.get(m.get("cur"), m.get("cur")) for m in meta.values()])

    out_ideas = build(ideas, series, meta, fx, bench)
    if prev_ok and len(out_ideas) < 0.5 * prev_ok:
        sys.exit(f"REFUSING TO WRITE: only {len(out_ideas)} priced ideas vs {prev_ok} last run")

    doc = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "asof": bench.d[-1],
        "bench": BENCH,
        "ideas": out_ideas,
        "meta": {s: meta[s] for s in sorted(meta) if s in syms},
        "missing": missing,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    os.replace(tmp, OUT)
    print(f"fx: {', '.join(f'{k}={v:.4g}' for k, v in sorted(fx.items()))}")
    print(f"priced {len(out_ideas)}/{len(ideas)} ideas as of {doc['asof']}; {len(missing)} symbols with no data: "
          f"{', '.join(missing[:30])}")


if __name__ == "__main__":
    main()
