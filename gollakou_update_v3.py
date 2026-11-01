#!/usr/bin/env python3
"""Gollakou data updater.

Fetches REAL football data from API-Football (fixtures, odds, predictions)
and REAL headlines from public RSS feeds, then writes data.json.
Nothing is invented: if a value is not available it is left empty (null).
"""
import difflib
import html
import json
import os
import re
import sys
import unicodedata
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

MAX_PRED_PER_RUN = int(os.environ.get("MAX_PRED", "30"))
MAX_ODDS_PAGES = 2
MIN_REMAINING = 6          # stop calling the API when the daily quota is almost used
DAYS_BEFORE = 1
DAYS_AFTER = 1          # free plan: only yesterday, today and tomorrow

FEEDS = [
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news"),
    ("L'Equipe", "https://www.lequipe.fr/rss/actu_rss_Football.xml"),
]

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ODDS_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORTS = {
    2: "soccer_uefa_champs_league",
    39: "soccer_epl",
    140: "soccer_spain_la_liga",
    135: "soccer_italy_serie_a",
    78: "soccer_germany_bundesliga",
    61: "soccer_france_ligue_one",
    3: "soccer_uefa_europa_league",
    94: "soccer_portugal_primeira_liga",
    88: "soccer_netherlands_eredivisie",
    71: "soccer_brazil_campeonato",
    13: "soccer_conmebol_copa_libertadores",
    253: "soccer_usa_mls",
}
ODDS_MAX_SPORTS_PER_RUN = 8   # 1 credit per league per run (500 free credits / month)
ODDS_MIN_REMAINING = 15
PREFERRED_BOOKS = ["bet365", "pinnacle", "williamhill", "unibet_eu", "betfair_ex_eu", "marathonbet", "betsson"]
STOP_WORDS = {"fc", "cf", "sc", "ac", "as", "afc", "club", "de", "the", "calcio", "sad", "sv", "fk", "ssc", "cd", "ud", "rc"}

remaining = None
odds_remaining = None
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
                "home_logo": fx["teams"]["home"].get("logo"),
                "away_logo": fx["teams"]["away"].get("logo"),
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
            print("odds league", lid, "season", season, "page", page,
                  "items", len(body.get("response", [])), "paging", body.get("paging"))
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


def norm_team(name):
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("-", " "))
    words = [w for w in text.split() if w not in STOP_WORDS]
    return " ".join(words)


def team_score(a, b):
    a, b = norm_team(a), norm_team(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if (a in b or b in a) and min(len(a), len(b)) >= 4:
        ratio = max(ratio, 0.8)
    return ratio


def pick_book(bookmakers):
    for key in PREFERRED_BOOKS:
        for b in bookmakers:
            if b.get("key") == key:
                return b
    return bookmakers[0] if bookmakers else None


def event_odds(event):
    book = pick_book(event.get("bookmakers") or [])
    if not book:
        return None
    for market in book.get("markets", []):
        if market.get("key") != "h2h":
            continue
        out = {"home": None, "draw": None, "away": None, "bookmaker": book.get("title")}
        for o in market.get("outcomes", []):
            if o.get("name") == event.get("home_team"):
                out["home"] = to_float(o.get("price"))
            elif o.get("name") == event.get("away_team"):
                out["away"] = to_float(o.get("price"))
            elif str(o.get("name")).lower() == "draw":
                out["draw"] = to_float(o.get("price"))
        if out["home"] and out["away"]:
            return out
    return None


def match_event(m, events):
    """Return the Odds API event that is surely the same game, else None."""
    try:
        kick = datetime.fromisoformat(m["kickoff"])
    except Exception:
        return None
    best, best_score = None, 0.0
    for ev in events:
        try:
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if abs((t - kick).total_seconds()) > 4 * 3600:
            continue
        sh = team_score(m["home"], ev.get("home_team"))
        sa = team_score(m["away"], ev.get("away_team"))
        if sh >= 0.72 and sa >= 0.72 and sh + sa > best_score:
            best, best_score = ev, sh + sa
    return best


def fetch_theoddsapi(matches):
    """Real decimal odds (1X2) from The Odds API for upcoming games. Free plan: 500 credits/month."""
    global odds_remaining
    if not ODDS_API_KEY:
        print("ODDS_API_KEY missing: no odds")
        return {}
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=36)
    wanted = {}
    for m in matches.values():
        if m["status"] not in UPCOMING or m["league_id"] not in ODDS_SPORTS:
            continue
        try:
            kick = datetime.fromisoformat(m["kickoff"])
        except Exception:
            continue
        if now <= kick <= horizon:
            wanted.setdefault(m["league_id"], []).append(m)
    order = [lid for lid in ODDS_SPORTS if lid in wanted][:ODDS_MAX_SPORTS_PER_RUN]
    found = {}
    for lid in order:
        if odds_remaining is not None and odds_remaining <= ODDS_MIN_REMAINING:
            print("The Odds API quota almost used, stopping")
            break
        params = urllib.parse.urlencode({"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h",
                                         "oddsFormat": "decimal", "dateFormat": "iso"})
        url = "%s/sports/%s/odds?%s" % (ODDS_BASE, ODDS_SPORTS[lid], params)
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
                rem = r.headers.get("x-requests-remaining")
                if rem is not None:
                    odds_remaining = int(float(rem))
                events = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print("odds league", lid, "failed:", e)
            continue
        hit = 0
        for m in wanted[lid]:
            ev = match_event(m, events)
            odds = event_odds(ev) if ev else None
            if odds:
                found[m["id"]] = odds
                hit += 1
        print("theoddsapi league", lid, "events", len(events), "matched", hit, "of", len(wanted[lid]),
              "credits left", odds_remaining)
    return found


def valid_prediction(p):
    """The API sends a 33/33/33 placeholder when it has no real prediction."""
    if not p:
        return False
    advice = (p.get("advice") or "").lower()
    return "no predictions available" not in advice


def fetch_prediction(fid):
    body = api("/predictions", {"fixture": fid})
    resp = body.get("response") or []
    if not resp:
        return None
    p = resp[0].get("predictions") or {}
    pct = p.get("percent") or {}
    winner = p.get("winner") or {}
    total = (resp[0].get("comparison") or {}).get("total") or {}
    result = {
        "strength_home": total.get("home"),
        "strength_away": total.get("away"),
        "home": pct.get("home"),
        "draw": pct.get("draw"),
        "away": pct.get("away"),
        "advice": p.get("advice"),
        "winner": winner.get("name"),
        "comment": winner.get("comment"),
    }
    return result if valid_prediction(result) else None


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


MEDIA_NS = "{http://search.yahoo.com/mrss/}"
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def https(url):
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def find_image(it):
    """Return (image, fallback) URLs from an RSS item, or ("", "")."""
    found = []
    for tag in (MEDIA_NS + "content", MEDIA_NS + "thumbnail"):
        for el in it.iter(tag):
            url = (el.get("url") or "").strip()
            kind = (el.get("type") or el.get("medium") or "image").lower()
            if url.startswith("http") and "video" not in kind:
                found.append(url)
    for el in it.findall("enclosure"):
        url = (el.get("url") or "").strip()
        kind = (el.get("type") or "").lower()
        if url.startswith("http") and (kind.startswith("image") or url.lower().split("?")[0].endswith(IMG_EXT)):
            found.append(url)
    if not found:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', it.findtext("description") or "")
        if m and m.group(1).startswith("http"):
            found.append(m.group(1))
    if not found:
        return "", ""
    original = https(found[0])
    better = original
    # BBC serves several widths of the same picture; ask for a sharper one
    if "ichef.bbci.co.uk" in original and "/240/" in original:
        better = original.replace("/240/", "/640/")
    return better, (original if better != original else "")


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
                low = link.lower()
                if "football" not in low and "soccer" not in low:
                    continue  # keep football stories only
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
                image, fallback = find_image(it)
                items.append({"title": title, "link": link, "source": source,
                              "published": published, "summary": summary,
                              "image": image or None, "image_fallback": fallback or None})
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


def to_pct(value):
    try:
        return int(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def implied_probs(odds):
    """Market probabilities (%) taken from 1X2 decimal odds, bookmaker margin removed."""
    try:
        inv = [1 / float(odds["home"]), 1 / float(odds["draw"]), 1 / float(odds["away"])]
    except Exception:
        return None
    total = sum(inv)
    return [100 * x / total for x in inv]


def build_picks(matches, limit=10):
    """Lowest-risk picks = double-chance options (1X or X2).
    Probability = the provider's figure; when real market odds exist we take the
    LOWER of the two figures, so a pick is only called safe if both agree.
    These are probabilities, never guarantees."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=30)
    picks = []
    for m in matches:
        if m.get("status") not in UPCOMING or not valid_prediction(m.get("prediction")):
            continue
        try:
            kick = datetime.fromisoformat(m["kickoff"])
        except Exception:
            continue
        if not (now <= kick <= horizon):
            continue
        p = m["prediction"]
        ph, pd, pa = to_pct(p.get("home")), to_pct(p.get("draw")), to_pct(p.get("away"))
        if None in (ph, pd, pa):
            continue
        api = {"1X": ph + pd, "X2": pd + pa}
        market = implied_probs(m.get("odds"))
        mk = {"1X": market[0] + market[1], "X2": market[1] + market[2]} if market else None
        # without market odds to confirm it, a pick can never be rated better than 75 %
        score = {k: (min(api[k], mk[k]) if mk else min(api[k], 75)) for k in api}
        code = max(score, key=score.get)
        prob = int(round(score[code]))
        label = m["home"] + " oswa nul" if code == "1X" else "Nul oswa " + m["away"]
        sh, sa = to_pct(p.get("strength_home")), to_pct(p.get("strength_away"))
        gap = abs(sh - sa) if sh is not None and sa is not None else 0
        picks.append({
            "id": m["id"], "league": m["league"], "league_id": m["league_id"],
            "kickoff": m["kickoff"], "home": m["home"], "away": m["away"],
            "home_logo": m.get("home_logo"), "away_logo": m.get("away_logo"),
            "market": code, "label": label, "prob": prob, "gap": gap,
            "risk": "Ba" if prob >= 85 else ("Mwayen" if prob >= 70 else "Wo"),
            "source": "API + kòt mache a" if mk else "API sèlman",
            "odds": m.get("odds"),
        })
    picks.sort(key=lambda x: (-x["prob"], -x["gap"], x["kickoff"]))
    return picks[:limit]


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
            odds = {}
            if os.environ.get("API_FOOTBALL_ODDS") == "1":   # needs a paid API-Football plan
                try:
                    odds = fetch_odds(matches)
                except QuotaError:
                    odds = {}
                except Exception as e:
                    print("odds failed:", e)
                    odds = {}
            try:
                for fid, o in fetch_theoddsapi(matches).items():
                    odds.setdefault(fid, o)
            except Exception as e:
                print("The Odds API failed:", e)
            # keep older real values when nothing new came back
            for fid, m in matches.items():
                prev = old_matches.get(fid, {})
                m["odds"] = odds.get(fid) or prev.get("odds")
                cached = prev.get("prediction")
                m["prediction"] = cached if valid_prediction(cached) else None
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
        "odds_quota_remaining": odds_remaining,
        "matches": match_list,
        "picks": build_picks(match_list),
        "news": fetch_news() or old.get("news", []),
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("matches:", len(match_list), "news:", len(data["news"]), "quota left:", remaining)


if __name__ == "__main__":
    main()
