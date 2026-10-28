#!/usr/bin/env python3
"""Gollakou data updater.

Fetches REAL football data from API-Football (fixtures, odds, predictions)
and REAL headlines from public RSS feeds, then writes data.json.
Nothing is invented: if a value is not available it is left empty (null).
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
BASE = "https://v3.football.api-sports.io"
TZ = "America/Port-au-Prince"
DATA_FILE = "data.json"

# API-Football league ids
LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "Champions League",
    3: "Europa League",
    94: "Liga Portugal",
    88: "Eredivisie",
    71: "Brasileirao",
    253: "MLS",
    13: "Copa Libertadores",
}

MAX_PRED_PER_RUN = int(os.environ.get("MAX_PRED", "20"))
MAX_ODDS_PAGES = 2
MIN_REMAINING = 6          # stop calling the API when the daily quota is almost used
DAYS_BEFORE = 1
DAYS_AFTER = 2

FEEDS = [
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news"),
    ("L'Equipe", "https://www.lequipe.fr/rss/actu_rss_Football.xml"),
]

remaining = None
UPCOMING = {"NS", "TBD"}


class QuotaError(Exception):
    pass


def api(path, params):
    """One API-Football request. Returns the parsed JSON body."""
    global remaining
    if remaining is not None and remaining <= MIN_REMAINING:
        raise QuotaError("daily quota almost used")
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-apisports-key": API_KEY})
    with urllib.request.urlopen(req, timeout=40) as r:
        rem = r.headers.get("x-ratelimit-requests-remaining")
        if rem is not None:
            try:
                remaining = int(rem)
            except ValueError:
                pass
        body = json.loads(r.read().decode("utf-8"))
    errs = body.get("errors")
    if errs:
        print("API errors for", path, params, "->", errs)
    return body


def load_old():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_fixtures():
    today = datetime.now(timezone.utc).date()
    out = {}
    for delta in range(-DAYS_BEFORE, DAYS_AFTER + 1):
        day = (today + timedelta(days=delta)).isoformat()
        body = api("/fixtures", {"date": day, "timezone": TZ})
        for fx in body.get("response", []):
            lg = fx.get("league", {})
            if lg.get("id") not in LEAGUES:
                continue
            fid = fx["fixture"]["id"]
            out[fid] = {
                "id": fid,
                "league_id": lg["id"],
                "league": LEAGUES[lg["id"]],
                "country": lg.get("country"),
                "season": lg.get("season"),
                "round": lg.get("round"),
                "kickoff": fx["fixture"].get("date"),
                "status": fx["fixture"].get("status", {}).get("short"),
                "elapsed": fx["fixture"].get("status", {}).get("elapsed"),
                "home": fx["teams"]["home"]["name"],
                "away": fx["teams"]["away"]["name"],
                "score_home": fx.get("goals", {}).get("home"),
                "score_away": fx.get("goals", {}).get("away"),
                "odds": None,
                "prediction": None,
            }
    return out


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_odds(matches):
    """Match Winner odds (bet id 1) per league; real bookmaker odds only."""
    found = {}
    leagues = {}
    for m in matches.values():
        if m["status"] in UPCOMING:
            leagues[m["league_id"]] = m["season"]
    for lid, season in leagues.items():
        page = 1
        while page <= MAX_ODDS_PAGES:
            body = api("/odds", {"league": lid, "season": season, "bet": 1, "page": page})
            for item in body.get("response", []):
                fid = item.get("fixture", {}).get("id")
                books = item.get("bookmakers") or []
                if not books:
                    continue
                book = next((b for b in books if b.get("id") == 8), books[0])
                for bet in book.get("bets", []):
                    if bet.get("id") == 1:
                        vals = {v.get("value"): to_float(v.get("odd")) for v in bet.get("values", [])}
                        found[fid] = {
                            "home": vals.get("Home"),
                            "draw": vals.get("Draw"),
                            "away": vals.get("Away"),
                            "bookmaker": book.get("name"),
                        }
            paging = body.get("paging") or {}
            if page >= int(paging.get("total") or 1):
                break
            page += 1
    return found


def fetch_prediction(fid):
    body = api("/predictions", {"fixture": fid})
    resp = body.get("response") or []
    if not resp:
        return None
    p = resp[0].get("predictions") or {}
    pct = p.get("percent") or {}
    winner = p.get("winner") or {}
    return {
        "home": pct.get("home"),
        "draw": pct.get("draw"),
        "away": pct.get("away"),
        "advice": p.get("advice"),
        "winner": winner.get("name"),
        "comment": winner.get("comment"),
    }


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_news():
    items = []
    for source, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; GollakouBot/1.0)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                root = ET.fromstring(r.read())
            count = 0
            for it in root.iter("item"):
                title = strip_html(it.findtext("title"))
                link = (it.findtext("link") or "").strip()
                if not title or not link:
                    continue
                try:
                    dt = parsedate_to_datetime(it.findtext("pubDate"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    published = dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    published = None
                summary = strip_html(it.findtext("description"))
                if len(summary) > 180:
                    summary = summary[:177].rstrip() + "..."
                items.append({"title": title, "link": link, "source": source,
                              "published": published, "summary": summary})
                count += 1
                if count >= 12:
                    break
            print("news ok:", source, count)
        except Exception as e:
            print("news failed:", source, e)
    seen, unique = set(), []
    for n in sorted(items, key=lambda n: n["published"] or "", reverse=True):
        if n["link"] in seen:
            continue
        seen.add(n["link"])
        unique.append(n)
    return unique[:40]


def main():
    old = load_old()
    old_matches = {m["id"]: m for m in old.get("matches", [])}
    matches = {}
    note = None

    if not API_KEY:
        print("API_FOOTBALL_KEY is missing")
        note = "API key missing"
    else:
        try:
            matches = fetch_fixtures()
            print("fixtures:", len(matches))
            try:
                odds = fetch_odds(matches)
            except QuotaError:
                odds = {}
            except Exception as e:
                print("odds failed:", e)
                odds = {}
            # keep older real values when nothing new came back
            for fid, m in matches.items():
                prev = old_matches.get(fid, {})
                m["odds"] = odds.get(fid) or prev.get("odds")
                m["prediction"] = prev.get("prediction")
            todo = sorted(
                [m for m in matches.values() if m["status"] in UPCOMING and not m["prediction"]],
                key=lambda m: m["kickoff"] or "",
            )[:MAX_PRED_PER_RUN]
            for m in todo:
                try:
                    m["prediction"] = fetch_prediction(m["id"])
                except QuotaError:
                    break
                except Exception as e:
                    print("prediction failed:", m["id"], e)
        except QuotaError:
            note = "daily quota reached, kept previous data"
        except Exception as e:
            print("football data failed:", e)
            note = "football data unavailable, kept previous data"

    if not matches and old_matches:
        matches = old_matches
    match_list = sorted(matches.values(), key=lambda m: m["kickoff"] or "")

    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "quota_remaining": remaining,
        "matches": match_list,
        "news": fetch_news() or old.get("news", []),
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("matches:", len(match_list), "news:", len(data["news"]), "quota left:", remaining)


if __name__ == "__main__":
    main()
