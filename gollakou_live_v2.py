#!/usr/bin/env python3
"""Gollakou live scores + match details.

One API-Football request per run (up to 20 games), only while games are being
played (10 minutes before kickoff until 3h30 after). For each game it keeps the
score, the minute, the events (goals, cards, substitutions), the statistics and
the line-ups, exactly as the API gives them. Nothing is invented.
Writes live.json, which the site reads on top of data.json.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
BASE = "https://v3.football.api-sports.io"
RESERVE = 20        # requests kept for the twice-a-day update of data.json
MAX_IDS = 20        # API limit for the "ids" parameter


def api(path, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"x-apisports-key": API_KEY})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def quota_left():
    """/status does not use up the daily quota."""
    try:
        resp = api("/status").get("response")
        req = resp.get("requests", {}) if isinstance(resp, dict) else {}
        return int(req["limit_day"]) - int(req["current"])
    except Exception as e:
        print("status check failed:", e)
        return None


def in_window(match, now):
    try:
        kick = datetime.fromisoformat(match["kickoff"])
    except Exception:
        return False
    return kick - timedelta(minutes=10) <= now <= kick + timedelta(hours=3, minutes=30)


def name_of(obj):
    return (obj or {}).get("name") if isinstance(obj, dict) else None


def build_events(fx):
    home_id = fx.get("teams", {}).get("home", {}).get("id")
    out = []
    for ev in fx.get("events") or []:
        t = ev.get("time") or {}
        side = "home" if (ev.get("team") or {}).get("id") == home_id else "away"
        out.append({
            "min": t.get("elapsed"),
            "extra": t.get("extra"),
            "side": side,
            "type": ev.get("type"),
            "detail": ev.get("detail"),
            "player": name_of(ev.get("player")),
            "assist": name_of(ev.get("assist")),
        })
    return out


def build_stats(fx):
    home_id = fx.get("teams", {}).get("home", {}).get("id")
    per_side = {"home": {}, "away": {}}
    order = []
    for block in fx.get("statistics") or []:
        side = "home" if (block.get("team") or {}).get("id") == home_id else "away"
        for s in block.get("statistics") or []:
            per_side[side][s.get("type")] = s.get("value")
            if s.get("type") not in order:
                order.append(s.get("type"))
    return [{"type": t, "home": per_side["home"].get(t), "away": per_side["away"].get(t)} for t in order]


def build_lineups(fx):
    home_id = fx.get("teams", {}).get("home", {}).get("id")
    out = {}
    for block in fx.get("lineups") or []:
        side = "home" if (block.get("team") or {}).get("id") == home_id else "away"

        def people(key):
            res = []
            for p in block.get(key) or []:
                pl = p.get("player") or {}
                res.append({"n": pl.get("number"), "name": pl.get("name"), "pos": pl.get("pos")})
            return res
        out[side] = {"formation": block.get("formation"), "coach": name_of(block.get("coach")),
                     "start": people("startXI"), "subs": people("substitutes")}
    return out


def main():
    if not API_KEY:
        print("API_FOOTBALL_KEY missing")
        return
    try:
        with open("data.json", encoding="utf-8") as f:
            matches = json.load(f).get("matches", [])
    except Exception as e:
        print("no data.json:", e)
        return

    now = datetime.now(timezone.utc)
    ids = [m["id"] for m in matches if in_window(m, now)]
    if not ids:
        print("no game in the live window: nothing to do")
        return

    left = quota_left()
    if left is not None and left <= RESERVE:
        print("quota low (%s left): skipping this run" % left)
        return

    fixtures = {}
    for i in range(0, min(len(ids), 2 * MAX_IDS), MAX_IDS):
        chunk = ids[i:i + MAX_IDS]
        body = api("/fixtures", {"ids": "-".join(str(x) for x in chunk)})
        if body.get("errors"):
            print("API errors:", body["errors"])
        for fx in body.get("response", []):
            st = fx.get("fixture", {}).get("status", {})
            fixtures[str(fx["fixture"]["id"])] = {
                "status": st.get("short"),
                "elapsed": st.get("elapsed"),
                "score_home": fx.get("goals", {}).get("home"),
                "score_away": fx.get("goals", {}).get("away"),
                "events": build_events(fx),
                "stats": build_stats(fx),
                "lineups": build_lineups(fx),
            }

    if not fixtures:
        print("API returned nothing")
        return
    n_ev = sum(1 for f in fixtures.values() if f["events"])
    n_st = sum(1 for f in fixtures.values() if f["stats"])
    n_lu = sum(1 for f in fixtures.values() if f["lineups"])
    with open("live.json", "w", encoding="utf-8") as f:
        json.dump({"updated": now.isoformat(), "checked": len(ids), "fixtures": fixtures},
                  f, ensure_ascii=False, separators=(",", ":"))
    print("live.json written for %d games (events %d, stats %d, lineups %d); quota left before run: %s"
          % (len(fixtures), n_ev, n_st, n_lu, left))


if __name__ == "__main__":
    main()
