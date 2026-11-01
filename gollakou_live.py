#!/usr/bin/env python3
"""Gollakou live scores.

One cheap API-Football request per run, and only while games are being played
(10 minutes before kickoff until 3h30 after). Writes live.json, which the site
reads on top of data.json. Nothing is invented: values come straight from the API.
"""
import json
import os
import sys
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
    with urllib.request.urlopen(req, timeout=30) as r:
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
            }

    if not fixtures:
        print("API returned nothing")
        return
    with open("live.json", "w", encoding="utf-8") as f:
        json.dump({"updated": now.isoformat(), "fixtures": fixtures}, f, ensure_ascii=False)
    print("live.json written for", len(fixtures), "games; quota left before run:", left)


if __name__ == "__main__":
    main()
