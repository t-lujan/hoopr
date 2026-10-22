"""Download everything the bot needs into one compact JSON snapshot.

The snapshot lets the browser engine run with no network access (for hosts like
claude.ai artifacts that block outside requests). Usage:

    python3 tools/build_snapshot.py build/snapshot.json
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hoopr import espn  # noqa: E402


def optional(fn, *args):
    """Data that doesn't exist yet (e.g. this season's team stats before opening night)."""
    try:
        return fn(*args)
    except espn.ESPNError:
        return None


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%MZ")


def main(out_path):
    now = datetime.now(timezone.utc)
    season = espn.current_season(now)
    prev = season - 1
    seasons = (season, prev)

    print("standings, league stats, injuries, rosters...", flush=True)
    stand = {s: espn.standings(s) for s in seasons}
    team_ids = sorted(stand[prev], key=int)
    league = {s: espn.league_players(s) for s in seasons}
    injuries = espn.injuries()
    rosters = dict(zip(team_ids, espn.parallel(espn.roster, team_ids)))
    team_stats = {s: {t: x for t, x in zip(team_ids, espn.parallel(lambda t, s=s: optional(espn.team_stats, t, s), team_ids))
                      if x} for s in seasons}

    print("schedules...", flush=True)
    schedules = {s: dict(zip(team_ids, espn.parallel(lambda t, s=s: espn.team_schedule(t, s), team_ids)))
                 for s in seasons}
    teams = {}
    for s in seasons:
        for rows in schedules[s].values():
            for g in rows:
                teams[g["opp_id"]] = [g["opp_abbr"], g["opp_name"]]

    player_ids = sorted({p["id"] for r in rosters.values() for p in r}, key=int)
    print(f"{len(player_ids)} players: profiles and game logs...", flush=True)
    athletes = dict(zip(player_ids, espn.parallel(espn.athlete, player_ids, workers=16)))
    logs = {}
    for s in seasons:
        for pid, rows in zip(player_ids, espn.parallel(lambda p, s=s: espn.gamelog(p, s), player_ids, workers=16)):
            if rows:
                logs.setdefault(pid, {})[s] = [
                    [g["event_id"], iso(g["date"]), int(g["playoff"]), int(g["home"]), g["team_id"], g["opp_id"],
                     g["min"], g["pts"], g["fgm"], g["fga"], g["fta"]] for g in rows]

    # box scores for each team's last 12 finished games (opponent defense vs position)
    box_events = set()
    for t in team_ids:
        finals = [g for g in schedules[season][t] if g["final"]]
        if len(finals) < 12:
            finals = [g for g in schedules[prev][t] if g["final"]] + finals
        box_events.update(g["event_id"] for g in finals[-12:])
    print(f"{len(box_events)} box scores...", flush=True)
    box_events = sorted(box_events)
    boxes = {}
    for eid, box in zip(box_events, espn.parallel(espn.box_score, box_events, workers=16)):
        boxes[eid] = {tid: [[p["id"], p["pos"], p["min"], p["pts"]] for p in side["players"]]
                      for tid, side in box.items()}

    # each team's next game: Vegas odds and every posted player points line
    next_games = {}
    for t in team_ids:
        upcoming = [g for g in schedules[season][t] if not g["final"] and g["date"] > now]
        if upcoming:
            next_games.setdefault(upcoming[0]["event_id"], set()).update({t, upcoming[0]["opp_id"]})
    print(f"{len(next_games)} upcoming games: odds and player lines...", flush=True)
    odds, lines = {}, {}
    for eid, tids in next_games.items():
        o = espn.game_odds(eid)
        if o:
            odds[eid] = [o["provider"], o["total"], o["spread"], o["details"]]
        for t in tids:
            for p in rosters.get(t, []):
                li = espn.player_points_line(eid, p["id"])
                if li:
                    lines[f"{eid}|{p['id']}"] = [li["line"], li["updated"]]

    snap = {
        "generated": now.isoformat(timespec="seconds"),
        "season": season,
        "teams": teams,
        "athletes": {pid: [a["name"], a["team_id"], a["position"], a["headshot"]] for pid, a in athletes.items()},
        "logs": logs,
        "league": {s: {pid: [round(p["gp"]), round(p["min"], 1), round(p["pts"], 1), round(p["stl"], 1),
                             round(p["blk"], 1), round(p["pf"], 1)] for pid, p in league[s].items()} for s in seasons},
        "schedules": {s: {t: [[g["event_id"], iso(g["date"]), int(g["home"]), g["opp_id"], int(g["final"])]
                              for g in rows] for t, rows in schedules[s].items()} for s in seasons},
        "teamStats": {s: {t: [round(x.get(k, 0), 3) for k in ("possessions", "avgSteals", "avgBlocks", "avgDefensiveRebounds")]
                          for t, x in team_stats[s].items()} for s in seasons},
        "standings": {s: {t: [x["gp"], round(x["ppg"], 3), round(x["opp_ppg"], 3)] for t, x in stand[s].items()}
                      for s in seasons},
        "rosters": {t: [p["id"] for p in r] for t, r in rosters.items()},
        "injuries": {t: [[i["id"], i["name"], i["status"], i["comment"]] for i in rows] for t, rows in injuries.items()},
        "boxes": boxes,
        "odds": odds,
        "lines": lines,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    print(f"wrote {out_path}: {os.path.getsize(out_path) / 1e6:.1f} MB, {len(lines)} player lines", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "build/snapshot.json")
