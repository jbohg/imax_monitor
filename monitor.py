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
                "url": s.get("purchaseUrl")
                       or s.get("_links", {}).get("https://api.amctheatres.com/rels/v2/purchase", {}).get("href")
                       or "https://www.amctheatres.com/movie-theatres/san-francisco/amc-metreon-16/showtimes",
            }
        total = body.get("count", 0)
        page_size = body.get("pageSize", 100)
        page += 1
        url = url if page <= -(-total // page_size) else None


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
        theatre_id = resolve_theatre_id(cfg.get("theatre_query", "Metreon"))
        state["theatre_id"] = theatre_id

    # Collect matching showtimes across the date range
    matches = {}
    errors = []
    for offset in range(cfg.get("days_ahead", 14) + 1):
        day = today + timedelta(days=offset)
        try:
            shows = (fetch_showtimes_api(theatre_id, day) if use_api
                     else fetch_showtimes_scrape(day))
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

    for sid, s in sorted(matches.items(), key=lambda kv: kv[1]["dt"]):
        prev = known.get(sid, {})
        status = ("sold_out" if s["sold_out"]
                  else "almost" if s["almost"] else "available")
        line = (f"{s['dt']:%a %b %-d, %-I:%M %p} — {s['movie']}"
                + (f"\n   {s['attrs']}" if s["attrs"] else "")
                + f"\n   {s['url']}")

        if status == "available" and prev.get("status") != "available":
            verb = "🎟 Tickets AVAILABLE" if not prev else "🎟 Seats FREED UP"
            msgs.append(f"{verb}:\n{line}")
        elif (status == "almost" and cfg.get("notify_mode") == "almost"
              and prev.get("status") not in ("almost", "sold_out")):
            msgs.append(f"⏳ Almost sold out:\n{line}")
        elif (status == "sold_out" and cfg.get("notify_sold_out")
              and prev.get("status") == "available"):
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
