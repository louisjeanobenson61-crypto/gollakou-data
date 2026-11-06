#!/usr/bin/env python3
"""Gollakou live scores + match details (made for the FREE API-Football plan).

The free plan does not allow the "ids" parameter, so each run does this:
  1. ONE request with the day's fixtures (scores, minute, final results of every game)
  2. ONE request with the full details of ONE live game (events, statistics, line-ups),
     rotating from game to game, and keeping the details found earlier for the others.
Only runs while games are being played (10 minutes before kickoff until 3h30 after).
Nothing is invented: values come straight from the API. Writes live.json.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
BASE = "https://v3.football.api-sports.io"
RESERVE = 20        # requests kept for the twice-a-day update of data.json
TZ = "America/Port-au-Prince"
LIVE_CODES = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT")
LEAGUE_ORDER = [2, 39, 140, 135, 78, 61, 3, 94, 88, 71, 13, 253]


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


def fetch_previous():
    """Details found by earlier runs (live.json is kept on the "live" branch)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        return {}
    url = "https://raw.githubusercontent.com/%s/live/live.json?t=%d" % (repo, int(datetime.now().timestamp()))
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("fixtures", {})
    except Exception as e:
        print("no previous live.json:", e)
        return {}


def rank(league_id):
    return LEAGUE_ORDER.index(league_id) if league_id in LEAGUE_ORDER else 99


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
    window = [m for m in matches if in_window(m, now)]
    if not window:
        print("no game in the live window: nothing to do")
        return
    ids = {m["id"]: m for m in window}

    left = quota_left()
    if left is not None and left <= RESERVE:
        print("quota low (%s left): skipping this run" % left)
        return

    # 1) the day's fixtures: scores, minute and final results for every game
    day = (now - timedelta(hours=5)).date().isoformat()      # date in Haiti
    body = api("/fixtures", {"date": day, "timezone": TZ})
    if body.get("errors"):
        print("API errors:", body["errors"])
    by_id = {fx["fixture"]["id"]: fx for fx in body.get("response", [])}
    previous = fetch_previous()
    fixtures = {}
    for mid in ids:
        fx = by_id.get(mid)
        if not fx:
            continue
        st = fx.get("fixture", {}).get("status", {})
        entry = {
            "status": st.get("short"),
            "elapsed": st.get("elapsed"),
            "score_home": fx.get("goals", {}).get("home"),
            "score_away": fx.get("goals", {}).get("away"),
        }
        old = previous.get(str(mid)) or {}
        for key in ("events", "stats", "lineups"):          # keep the details found earlier
            if old.get(key):
                entry[key] = old[key]
        fixtures[str(mid)] = entry
    if not fixtures:
        print("API returned nothing for the day's fixtures")
        return

    # 2) full details of ONE live game (rotating)
    live_ids = sorted((mid for mid in ids if str(mid) in fixtures and fixtures[str(mid)]["status"] in LIVE_CODES),
                      key=lambda mid: (rank(ids[mid]["league_id"]), mid))
    detailed = None
    if live_ids and (left is None or left > RESERVE + 10):
        detailed = live_ids[int(now.timestamp() // 900) % len(live_ids)]
        try:
            body2 = api("/fixtures", {"id": detailed})
            if body2.get("errors"):
                print("API errors (details):", body2["errors"])
            resp = body2.get("response") or []
            if resp:
                fx = resp[0]
                e = fixtures[str(detailed)]
                e["events"] = build_events(fx)
                e["stats"] = build_stats(fx)
                e["lineups"] = build_lineups(fx)
        except Exception as ex:
            print("details failed:", ex)

    n_ev = sum(1 for f in fixtures.values() if f.get("events"))
    n_st = sum(1 for f in fixtures.values() if f.get("stats"))
    n_lu = sum(1 for f in fixtures.values() if f.get("lineups"))
    with open("live.json", "w", encoding="utf-8") as f:
        json.dump({"updated": now.isoformat(), "checked": len(ids), "fixtures": fixtures},
                  f, ensure_ascii=False, separators=(",", ":"))
    print("live.json written: %d games, %d live now, details refreshed for %s (events %d, stats %d, lineups %d); quota left before run: %s"
          % (len(fixtures), len(live_ids), detailed, n_ev, n_st, n_lu, left))


if __name__ == "__main__":
    main()
