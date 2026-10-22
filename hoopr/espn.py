"""Thin client for ESPN's public (keyless) NBA JSON endpoints, with a disk cache."""

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
WEB = "https://site.web.api.espn.com/apis/common/v3"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

CACHE_DIR = os.environ.get("HOOPR_CACHE_DIR", os.path.expanduser("~/.cache/hoopr"))

# Cache lifetimes (seconds)
LIVE = 10 * 60          # lines, injuries, schedules
DAILY = 12 * 3600       # season aggregates
FOREVER = 365 * 86400   # finished box scores


class ESPNError(RuntimeError):
    pass


def get_json(url, ttl=LIVE):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path) as f:
            return json.load(f)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 hoopr"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            break
        except Exception as e:  # network hiccup, retry with backoff
            last_err = e
            time.sleep(0.5 * 2 ** attempt)
    else:
        raise ESPNError(f"request failed: {url}: {last_err}")
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # unique per thread: parallel fetches of one URL
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def parallel(fn, items, workers=12):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def parse_date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def current_season(now=None):
    """ESPN labels a season by the year it ends (2026-27 -> 2027)."""
    now = now or datetime.now(timezone.utc)
    return now.year + 1 if now.month >= 9 else now.year


def pos_bucket(abbr):
    """Collapse ESPN positions to G / F / C."""
    abbr = (abbr or "").upper()
    if abbr.endswith("C"):
        return "C"
    if abbr.endswith("F"):
        return "F"
    return "G"


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _made_att(s):
    try:
        m, a = s.split("-")
        return float(m), float(a)
    except (AttributeError, ValueError):
        return 0.0, 0.0


# ---------------------------------------------------------------- players

def search_player(name):
    q = urllib.parse.quote(name.strip())
    data = get_json(f"{WEB}/search?query={q}&limit=10&type=player&sport=basketball&league=nba", ttl=DAILY)
    items = [i for i in data.get("items", []) if i.get("league") == "nba"]
    if not items:
        raise ESPNError(f"No NBA player found matching '{name}'")
    return {"id": items[0]["id"], "name": items[0]["displayName"],
            "others": [i["displayName"] for i in items[1:4]]}


def athlete(athlete_id):
    a = get_json(f"{WEB}/sports/basketball/nba/athletes/{athlete_id}", ttl=DAILY)["athlete"]
    team = a.get("team") or {}
    return {
        "id": a["id"],
        "name": a["displayName"],
        "team_id": team.get("id"),
        "team_abbr": team.get("abbreviation"),
        "team_name": team.get("displayName"),
        "position": (a.get("position") or {}).get("abbreviation", ""),
        "headshot": (a.get("headshot") or {}).get("href"),
    }


def gamelog(athlete_id, season):
    """Regular season + playoff games the player appeared in, newest first."""
    d = get_json(f"{WEB}/sports/basketball/nba/athletes/{athlete_id}/gamelog?season={season}",
                 ttl=LIVE if season == current_season() else DAILY)
    names = d.get("names", [])
    meta = d.get("events", {})
    games = []
    for st in d.get("seasonTypes", []):
        label = st.get("displayName", "")
        if "Preseason" in label:
            continue
        playoff = "Postseason" in label
        for cat in st.get("categories", []):
            for ev in cat.get("events", []):
                m = meta.get(ev["eventId"])
                if not m:
                    continue
                s = dict(zip(names, ev["stats"]))
                minutes = _num(s.get("minutes"))
                if minutes <= 0:
                    continue
                fgm, fga = _made_att(s.get("fieldGoalsMade-fieldGoalsAttempted"))
                tpm, tpa = _made_att(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                ftm, fta = _made_att(s.get("freeThrowsMade-freeThrowsAttempted"))
                opp_id = m["opponent"]["id"]
                team_id = m["awayTeamId"] if m["homeTeamId"] == opp_id else m["homeTeamId"]
                games.append({
                    "event_id": ev["eventId"],
                    "date": parse_date(m["gameDate"]),
                    "season": season,
                    "playoff": playoff,
                    "home": m.get("atVs") == "vs",
                    "team_id": team_id,
                    "opp_id": opp_id,
                    "opp_abbr": m["opponent"].get("abbreviation"),
                    "min": minutes,
                    "pts": _num(s.get("points")),
                    "fgm": fgm, "fga": fga, "tpm": tpm, "tpa": tpa, "ftm": ftm, "fta": fta,
                    "reb": _num(s.get("totalRebounds")),
                    "ast": _num(s.get("assists")),
                })
    games.sort(key=lambda g: g["date"], reverse=True)
    return games


def league_players(season):
    """Season averages for every player in the league, keyed by athlete id."""
    url = (f"{WEB}/sports/basketball/nba/statistics/byathlete?region=us&lang=en&contentorigin=espn"
           f"&isqualified=false&limit=1000&page=1&season={season}&seasontype=2&sort=offensive.avgPoints:desc")
    d = get_json(url, ttl=DAILY)
    cats = {c["name"]: c["names"] for c in d.get("categories", [])}
    out = {}
    for row in d.get("athletes", []):
        a = row["athlete"]
        s = {}
        for c in row["categories"]:
            s.update(zip(cats.get(c["name"], []), (_num(v) for v in c.get("totals", []))))
        out[a["id"]] = {
            "name": a["displayName"],
            "team_id": a.get("teamId"),
            "pos": pos_bucket((a.get("position") or {}).get("abbreviation")),
            "gp": s.get("gamesPlayed", 0),
            "min": s.get("avgMinutes", 0),
            "pts": s.get("avgPoints", 0),
            "fga": s.get("avgFieldGoalsAttempted", 0),
            "stl": s.get("avgSteals", 0),
            "blk": s.get("avgBlocks", 0),
            "reb": s.get("avgRebounds", 0),
            "pf": s.get("avgFouls", 0),
        }
    return out


# ---------------------------------------------------------------- teams

def team_schedule(team_id, season):
    d = get_json(f"{SITE}/teams/{team_id}/schedule?season={season}",
                 ttl=LIVE if season == current_season() else DAILY)
    games = []
    for e in d.get("events", []):
        c = e["competitions"][0]
        me = next(x for x in c["competitors"] if x["id"] == str(team_id))
        opp = next(x for x in c["competitors"] if x["id"] != str(team_id))
        final = c.get("status", {}).get("type", {}).get("completed", False)
        games.append({
            "event_id": e["id"],
            "date": parse_date(e["date"]),
            "home": me["homeAway"] == "home",
            "opp_id": opp["id"],
            "opp_abbr": opp["team"]["abbreviation"],
            "opp_name": opp["team"]["displayName"],
            "final": final,
            "pts": (me.get("score") or {}).get("value") if final else None,
            "opp_pts": (opp.get("score") or {}).get("value") if final else None,
        })
    games.sort(key=lambda g: g["date"])
    return games


def team_stats(team_id, season):
    d = get_json(f"{SITE}/teams/{team_id}/statistics?season={season}", ttl=DAILY)
    out = {}
    for c in d.get("results", {}).get("stats", {}).get("categories", []):
        for s in c.get("stats", []):
            out[s["name"]] = s.get("value") or 0.0
    out["possessions"] = (out.get("avgFieldGoalsAttempted", 0) - out.get("avgOffensiveRebounds", 0)
                          + out.get("avgTurnovers", 0) + 0.44 * out.get("avgFreeThrowsAttempted", 0))
    return out


def standings(season):
    d = get_json(f"https://site.api.espn.com/apis/v2/sports/basketball/nba/standings?season={season}", ttl=DAILY)
    out = {}
    for conf in d.get("children", []):
        for e in conf["standings"]["entries"]:
            s = {x["name"]: x.get("value") for x in e["stats"]}
            gp = (s.get("wins") or 0) + (s.get("losses") or 0)
            out[e["team"]["id"]] = {
                "abbr": e["team"]["abbreviation"],
                "gp": gp,
                "ppg": s.get("avgPointsFor") or 0.0,
                "opp_ppg": s.get("avgPointsAgainst") or 0.0,
            }
    return out


def roster(team_id):
    d = get_json(f"{SITE}/teams/{team_id}/roster", ttl=DAILY)
    return [{"id": a["id"], "name": a["displayName"],
             "pos": pos_bucket((a.get("position") or {}).get("abbreviation"))}
            for a in d.get("athletes", [])]


def injuries():
    """{team_id: [{id, name, status, comment}]}"""
    d = get_json(f"{SITE}/injuries", ttl=LIVE)
    out = {}
    for t in d.get("injuries", []):
        rows = []
        for i in t.get("injuries", []):
            a = i.get("athlete", {})
            href = next((l["href"] for l in a.get("links", []) if "/id/" in l.get("href", "")), "")
            m = re.search(r"/id/(\d+)", href)
            rows.append({"id": m.group(1) if m else None, "name": a.get("displayName"),
                         "status": i.get("status", ""), "comment": i.get("shortComment", "")})
        out[t["id"]] = rows
    return out


# ---------------------------------------------------------------- games

def box_score(event_id):
    """{team_id: {"pts", "poss", "players": [{id, name, pos, min, pts, starter}]}}"""
    return _parse_box(get_json(f"{SITE}/summary?event={event_id}", ttl=FOREVER))


def final_box(event_id):
    """Box score of a finished game, or None while it hasn't finished. Never cached
    before the final whistle, so grading can't read a half-time box score."""
    url = f"{SITE}/summary?event={event_id}"
    d = get_json(url + "&_=live", ttl=60)
    status = (((d.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
    if not status.get("completed"):
        return None
    return _parse_box(get_json(url, ttl=FOREVER))


def scoreboard(date):
    """Games on a US date ('YYYYMMDD'): [{event_id, date, home_id, away_id, short, state}]"""
    d = get_json(f"{SITE}/scoreboard?dates={date}", ttl=LIVE)
    out = []
    for e in d.get("events", []):
        c = e["competitions"][0]
        teams = {x["homeAway"]: x["id"] for x in c["competitors"]}
        out.append({"event_id": e["id"], "date": parse_date(e["date"]), "home_id": teams.get("home"),
                    "away_id": teams.get("away"), "short": e.get("shortName"),
                    "state": c.get("status", {}).get("type", {}).get("state")})
    return out


def _parse_box(d):
    box = d.get("boxscore", {})
    out = {}
    for t in box.get("teams", []):
        s = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        fgm, fga = _made_att(s.get("fieldGoalsMade-fieldGoalsAttempted"))
        ftm, fta = _made_att(s.get("freeThrowsMade-freeThrowsAttempted"))
        oreb = _num(s.get("offensiveRebounds"))
        tov = _num(s.get("totalTurnovers") or s.get("turnovers"))
        out[t["team"]["id"]] = {"poss": fga - oreb + tov + 0.44 * fta, "players": [], "pts": 0.0}
    for t in box.get("players", []):
        tid = t["team"]["id"]
        st = t["statistics"][0]
        keys = st.get("keys", [])
        entry = out.setdefault(tid, {"poss": 0.0, "players": [], "pts": 0.0})
        for a in st.get("athletes", []):
            if a.get("didNotPlay") or not a.get("stats"):
                continue
            s = dict(zip(keys, a["stats"]))
            p = {"id": a["athlete"]["id"], "name": a["athlete"]["displayName"],
                 "pos": pos_bucket((a["athlete"].get("position") or {}).get("abbreviation")),
                 "min": _num(s.get("minutes")), "pts": _num(s.get("points")),
                 "starter": a.get("starter", False)}
            entry["players"].append(p)
            entry["pts"] += p["pts"]
    return out


def game_odds(event_id):
    """Vegas spread/total for a game. spread is from the home team's perspective."""
    try:
        d = get_json(f"{CORE}/events/{event_id}/competitions/{event_id}/odds", ttl=LIVE)
    except ESPNError:
        return None
    for it in d.get("items", []):
        if it.get("overUnder") is not None:
            return {"provider": it.get("provider", {}).get("name"), "total": it["overUnder"],
                    "spread": it.get("spread"), "details": it.get("details")}
    return None


def points_lines(event_id):
    """Every posted player points line for a game: {athlete_id: {line, prices, provider, updated}}."""
    base = f"{CORE}/events/{event_id}/competitions/{event_id}/odds/100/propBets?lang=en&region=us&limit=1000"
    out = {}
    page = 1
    while True:
        try:
            d = get_json(f"{base}&page={page}", ttl=LIVE)
        except ESPNError:
            return out
        for it in d.get("items", []):
            if it.get("type", {}).get("name") != "Total Points":
                continue
            m = re.search(r"/athletes/(\d+)", it.get("athlete", {}).get("$ref", ""))
            line = (it.get("current") or {}).get("target", {}).get("value")
            if not m or line is None:
                continue
            row = out.setdefault(m.group(1), {"line": float(line), "prices": [], "provider": "DraftKings (via ESPN)"})
            row["line"] = float(line)
            row["prices"].append(it.get("odds", {}).get("american", {}).get("value"))
            row["updated"] = it.get("lastUpdated")
        if page >= d.get("pageCount", 1):
            break
        page += 1
    return out


def player_points_line(event_id, athlete_id):
    """Sportsbook player points line from ESPN's prop feed (DraftKings)."""
    return points_lines(event_id).get(str(athlete_id))
