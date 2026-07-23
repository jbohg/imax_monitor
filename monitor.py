#!/usr/bin/env python3
"""Monitor AMC showtimes for a movie and ping a Telegram bot on availability.

Data sources, in order of preference:
  1. Official AMC API (https://developers.amctheatres.com) if AMC_API_KEY is set.
  2. Fallback: best-effort scrape of the amctheatres.com showtimes page.

State (state.json) remembers which showtimes you were already notified about,
so re-runs stay quiet unless something changes.
"""

import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
import yaml

STATE_FILE = "state.json"
AMC_API = "https://api.amctheatres.com/v2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ── helpers ──────────────────────────────────────────────────────────────────

def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"theatre_id": None, "showtimes": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def parse_windows(windows):
    """['18:30-22:00'] -> [(time(18,30), time(22,0))]"""
    out = []
    for w in windows or []:
        a, b = w.split("-")
        h1, m1 = map(int, a.split(":"))
        h2, m2 = map(int, b.split(":"))
        out.append((time(h1, m1), time(h2, m2)))
    return out


def in_schedule(dt_local, schedule):
    windows = parse_windows(schedule.get(DAY_KEYS[dt_local.weekday()], []))
    return any(a <= dt_local.time() <= b for a, b in windows)


# ── source 1: official AMC API ───────────────────────────────────────────────

def amc_headers():
    return {"X-AMC-Vendor-Key": os.environ["AMC_API_KEY"], "User-Agent": UA}


def resolve_theatre_id(query):
    r = requests.get(
        f"{AMC_API}/theatres",
        params={"name": query, "page-size": 10},
        headers=amc_headers(),
        timeout=30,
    )
    r.raise_for_status()
    theatres = r.json().get("_embedded", {}).get("theatres", [])
    if not theatres:
        raise RuntimeError(f"No AMC theatre found matching {query!r}")
    t = theatres[0]
    print(f"Resolved theatre: {t.get('name')} (id={t.get('id')})")
    return t["id"]


def fetch_showtimes_api(theatre_id, day: date):
    """Yield normalized showtime dicts for one date via the official API."""
    date_str = f"{day.month}-{day.day}-{day.year}"  # M-D-YYYY per AMC docs
    url = f"{AMC_API}/theatres/{theatre_id}/showtimes/{date_str}"
    page = 1
    while url:
        r = requests.get(url, params={"page-size": 100, "page-number": page},
                         headers=amc_headers(), timeout=30)
        if r.status_code == 404:
            return  # no showtimes posted for that date yet
        r.raise_for_status()
        body = r.json()
        for s in body.get("_embedded", {}).get("showtimes", []):
            attrs = [a.get("code", "") + " " + a.get("name", "")
                     for a in s.get("attributes", [])]
            yield {
                "id": str(s["id"]),
                "movie": s.get("movieName", ""),
                "when_local": s.get("showDateTimeLocal", ""),
                "sold_out": bool(s.get("isSoldOut")),
                "almost": bool(s.get("isAlmostSoldOut")),
                "canceled": bool(s.get("isCanceled")),
                "attrs": " | ".join(attrs),
                "links": s.get("_links", {}),
                "url": s.get("purchaseUrl")
                       or s.get("_links", {}).get("https://api.amctheatres.com/rels/v2/purchase", {}).get("href")
                       or "https://www.amctheatres.com/movie-theatres/san-francisco/amc-metreon-16/showtimes",
            }
        total = body.get("count", 0)
        page_size = body.get("pageSize", 100)
        page += 1
        url = url if page <= -(-total // page_size) else None


def _natural_row_key(row):
    """Sort rows naturally: A < B < ... and 1 < 2 < ... < 10."""
    row = str(row)
    return [int(t) if t.isdigit() else t.upper()
            for t in re.findall(r"\d+|\D+", row)]


def count_good_seats(showtime, cfg):
    """Return (ok, detail_str) for a showtime, or None if unknown.

    Follows the showtime's seat-related hypermedia link, then defensively
    scans the JSON for seat objects (anything with a row + availability
    field). 'Good' = available, not in the front N rows, not an excluded
    seat type. If require_adjacent is set, min_good_seats of them must sit
    in consecutive columns of the same row. Returns None on any failure so
    callers can fail open.
    """
    sf = cfg.get("seat_filter") or {}
    min_seats = int(sf.get("min_good_seats", 1))
    need_adjacent = bool(sf.get("require_adjacent")) and min_seats > 1
    href = None
    for rel, v in (showtime.get("links") or {}).items():
        if "seat" in rel.lower() and isinstance(v, dict) and not v.get("templated"):
            href = v.get("href")
            break
    if not href:
        return None
    try:
        r = requests.get(href, headers=amc_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Seat check failed for showtime {showtime.get('id')}: {e}")
        return None

    seats = []

    def get_column(seat, low):
        ck = next((low[k] for k in ("columnname", "column", "col",
                                    "seatnumber", "number") if k in low), None)
        val = seat.get(ck) if ck else None
        if val is None and "name" in low:  # fall back to digits in "C12"
            m = re.search(r"(\d+)$", str(seat[low["name"]]))
            val = m.group(1) if m else None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def walk(n):
        if isinstance(n, dict):
            low = {k.lower(): k for k in n}
            rk = next((low[k] for k in ("rowname", "row", "rowlabel") if k in low), None)
            ak = next((low[k] for k in ("available", "isavailable", "seatstatus",
                                        "status") if k in low), None)
            if rk and ak and not isinstance(n[rk], (dict, list)):
                seats.append((n, n[rk], n[ak], get_column(n, low)))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(data)
    if not seats:
        return None

    rows = sorted({str(r) for _, r, _, _ in seats}, key=_natural_row_key)
    front = set(rows[:int(sf.get("exclude_front_rows", 2))])
    bad_types = [t.lower() for t in sf.get("exclude_seat_types", [])]

    def is_available(v):
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("available", "open", "notsold", "true")

    def seat_type_ok(seat):
        blob = json.dumps(seat).lower()
        return not any(t in blob for t in bad_types)

    good = [(str(r), c) for seat, r, a, c in seats
            if is_available(a) and str(r) not in front and seat_type_ok(seat)]

    # Keep only the central portion of each row (aisle/edge seats are meh).
    band = float(sf.get("center_band", 1.0))
    if band < 1.0:
        row_extent = {}
        for _, r, _, c in seats:
            if c is not None:
                lo, hi = row_extent.get(str(r), (c, c))
                row_extent[str(r)] = (min(lo, c), max(hi, c))

        def in_band(r, c):
            if c is None or r not in row_extent:
                return True  # unknown geometry -> fail open
            lo, hi = row_extent[r]
            mid, half = (lo + hi) / 2, (hi - lo) * band / 2
            return mid - half <= c <= mid + half

        good = [(r, c) for r, c in good if in_band(r, c)]

    # Longest run of consecutive columns within each row
    best_run, best_row = 0, None
    by_row = {}
    for r, c in good:
        by_row.setdefault(r, []).append(c)
    for r, cols in by_row.items():
        cols = sorted(c for c in cols if c is not None)
        run = 1 if cols else 0
        best_here = run
        for a, b in zip(cols, cols[1:]):
            run = run + 1 if b == a + 1 else 1
            best_here = max(best_here, run)
        if best_here > best_run:
            best_run, best_row = best_here, r

    front_edge = rows[:len(front)][-1] if front else "?"
    if need_adjacent:
        cols_known = any(c is not None for _, c in good)
        if not cols_known:
            # can't verify adjacency — fail open on total count
            ok = len(good) >= min_seats
            detail = (f"{len(good)} seat(s) beyond row {front_edge} "
                      "(adjacency unverified)")
        else:
            ok = best_run >= min_seats
            detail = (f"{best_run} adjacent seat(s) in row {best_row}, "
                      f"{len(good)} total beyond row {front_edge}"
                      if ok else
                      f"only scattered singles beyond row {front_edge}")
    else:
        ok = len(good) >= min_seats
        good_rows = sorted({r for r, _ in good}, key=_natural_row_key)
        detail = (f"{len(good)} seat(s) beyond row {front_edge}"
                  + (f" (rows {good_rows[0]}–{good_rows[-1]})" if good_rows else ""))
    return ok, detail


# ── source 2: fallback page scrape (best effort) ─────────────────────────────

def fetch_showtimes_scrape(day: date):
    url = ("https://www.amctheatres.com/movie-theatres/san-francisco/"
           f"amc-metreon-16/showtimes?date={day.isoformat()}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("Could not find embedded JSON on AMC page "
                           "(site changed or request was blocked).")
    data = json.loads(m.group(1))

    found = []

    def walk(node):
        if isinstance(node, dict):
            if "showDateTimeLocal" in node and ("movieName" in node or "movieId" in node):
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    for s in found:
        attrs = s.get("attributes", [])
        if attrs and isinstance(attrs[0], dict):
            attrs = [a.get("code", "") + " " + a.get("name", "") for a in attrs]
        yield {
            "id": str(s.get("id") or s.get("showtimeId") or s["showDateTimeLocal"]),
            "movie": s.get("movieName", ""),
            "when_local": s.get("showDateTimeLocal", ""),
            "sold_out": bool(s.get("isSoldOut")),
            "almost": bool(s.get("isAlmostSoldOut")),
            "canceled": bool(s.get("isCanceled")),
            "attrs": " | ".join(map(str, attrs)),
            "url": url,
        }


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    state = load_state()
    tz = ZoneInfo(cfg.get("timezone", "America/Los_Angeles"))
    today = datetime.now(tz).date()
    use_api = bool(os.environ.get("AMC_API_KEY"))

    theatre_id = cfg.get("theatre_id") or state.get("theatre_id")
    if use_api and not theatre_id:
        try:
            theatre_id = resolve_theatre_id(cfg.get("theatre_query", "Metreon"))
            state["theatre_id"] = theatre_id
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code in (401, 403):
                print(f"AMC API rejected the key ({code}) — likely not activated "
                      "yet. Falling back to page scraping for this run.")
                use_api = False
            else:
                raise

    # Collect matching showtimes across the date range
    matches = {}
    errors = []
    for offset in range(cfg.get("days_ahead", 14) + 1):
        day = today + timedelta(days=offset)
        try:
            try:
                shows = list(fetch_showtimes_api(theatre_id, day)) if use_api \
                        else list(fetch_showtimes_scrape(day))
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if use_api and code in (401, 403):
                    print(f"AMC API rejected the key ({code}) — falling back "
                          "to page scraping for this run.")
                    use_api = False
                    shows = list(fetch_showtimes_scrape(day))
                else:
                    raise
            for s in shows:
                if cfg["movie_match"].lower() not in s["movie"].lower():
                    continue
                if s["canceled"]:
                    continue
                dt = datetime.fromisoformat(s["when_local"])
                if not in_schedule(dt, cfg.get("schedule", {})):
                    continue
                fmts = cfg.get("formats_any") or []
                if fmts and not any(f.lower() in s["attrs"].lower() for f in fmts):
                    continue
                s["dt"] = dt
                matches[s["id"]] = s
        except Exception as e:  # keep going; one bad day shouldn't kill the run
            errors.append(f"{day}: {e}")

    if errors and not matches and len(errors) > cfg.get("days_ahead", 14) // 2:
        # Everything failed — likely blocked or broken; tell yourself once a day.
        stamp = state.get("last_error_notice", "")
        if stamp != str(today):
            telegram("⚠️ Odyssey monitor: all fetches failed today. "
                     f"First error: {errors[0]}")
            state["last_error_notice"] = str(today)
        save_state(state)
        sys.exit(0)

    known = state.setdefault("showtimes", {})
    msgs = []

    sf = cfg.get("seat_filter") or {}
    for sid, s in sorted(matches.items(), key=lambda kv: kv[1]["dt"]):
        prev = known.get(sid, {})
        status = ("sold_out" if s["sold_out"]
                  else "almost" if s["almost"] else "available")
        seat_note = ""
        if status == "available" and use_api and sf.get("enabled"):
            res = count_good_seats(s, cfg)
            if res is None:
                seat_note = "\n   (seat locations unverified)"
            elif not res[0]:
                status = "front_only"   # tickets exist but not the seats we want
            else:
                seat_note = f"\n   {res[1]}"
        line = (f"{s['dt']:%a %b %-d, %-I:%M %p} — {s['movie']}"
                + (f"\n   {s['attrs']}" if s["attrs"] else "")
                + seat_note
                + f"\n   {s['url']}")

        if status == "available" and prev.get("status") != "available":
            verb = "🎟 Tickets AVAILABLE" if not prev else "🎟 Seats FREED UP"
            msgs.append(f"{verb}:\n{line}")
        elif (status == "almost" and cfg.get("notify_mode") == "almost"
              and prev.get("status") not in ("almost", "sold_out")):
            msgs.append(f"⏳ Almost sold out:\n{line}")
        elif (status == "sold_out" and cfg.get("notify_sold_out")
              and prev.get("status") in ("available", "front_only")):
            msgs.append(f"❌ Now sold out:\n{line}")

        known[sid] = {"status": status, "when": s["when_local"], "movie": s["movie"]}

    # Note showtimes that vanished entirely after being available
    if cfg.get("notify_sold_out"):
        for sid, prev in list(known.items()):
            if sid not in matches and prev.get("status") == "available":
                when = prev.get("when", "?")
                if when and when > datetime.now(tz).replace(tzinfo=None).isoformat():
                    msgs.append(f"❌ Showtime removed/sold out: {prev.get('movie')} @ {when}")
                known[sid]["status"] = "gone"

    if msgs:
        telegram("\n\n".join(msgs)[:4000])
        print(f"Sent {len(msgs)} notification(s).")
    else:
        print(f"No changes. {len(matches)} matching showtime(s) tracked.")

    save_state(state)


if __name__ == "__main__":
    main()
