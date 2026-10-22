"""Points over/under prediction engine.

The bot builds a baseline from the player's own recent scoring, then applies a
series of adjustments (each one a multiplier, shrunk toward 1.0 so small samples
can't swing the result much):

  home/away split, rest, opponent defense, game pace, Vegas implied team total,
  opponent defense vs the player's position, head-to-head history, injured
  teammates (usage bump), injured opponents, and blowout risk.

The adjusted projection then drives a simulation: the game is played out SIMS
times, drawing the final margin (blowouts cut a starter's minutes), the player's
minutes and scoring rate from the player's own game-to-game variation. The share
of simulated games that clear the line is blended with the player's recent hit
rate against that same line.
"""

import math
import statistics
from datetime import datetime, timedelta, timezone

from . import espn, learn

BREAK_EVEN = 0.5238  # win rate needed to profit at standard -110 juice
SIMS = 100           # simulated games per prediction
MARGIN_SD = 12.5     # spread of NBA final margins around the Vegas line
EARLY_EXIT = 0.03    # chance a simulated game is cut short (injury, foul-out, ejection)


def _avg(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class Prediction(dict):
    """Plain dict so it serializes straight to JSON for the web UI."""


def seed_for(*parts):
    """32-bit FNV-1a hash, so the same question always replays the same simulated games."""
    h = 0x811C9DC5
    for b in "|".join(str(p) for p in parts).encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def mulberry32(seed):
    """Small seeded RNG; bit-for-bit the same as the browser engine's."""
    a = seed & 0xFFFFFFFF

    def imul(x, y):
        return (x * y) & 0xFFFFFFFF

    def rnd():
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = imul(a ^ (a >> 15), 1 | a)
        t = ((t + imul(t ^ (t >> 7), 61 | t)) & 0xFFFFFFFF) ^ t
        return (t ^ (t >> 14)) / 4294967296

    return rnd


def _normal(rnd):
    u1, u2 = 1 - rnd(), rnd()
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def simulate(projection, line, history, exp_min, margin_mean, starter, widen, rnd, runs=SIMS):
    """Play the game out `runs` times and count how often the player clears the line.

    Each run starts from one of the player's real recent games, `history` as
    (minutes, points) pairs, so minutes and scoring stay linked the way they
    really are (an early exit is also a low-scoring night). That game's minutes
    are shifted to tonight's expected minutes, a simulated final margin trims a
    starter's minutes in a blowout, and a little extra noise covers what history
    hasn't shown. The results are scaled so their average equals `projection`:
    the simulation supplies the shape of the outcomes, the factor table supplies
    where they center. `widen` > 1 spreads them out when the role is uncertain.
    """
    # Games cut short (injury, rest, ejection) are rare events a 25-game window can
    # over- or under-sample, so they're replaced by a fixed early-exit rate.
    usual = sorted(m for m, _ in history)[len(history) // 2]
    full = [(m, p) for m, p in history if m >= usual / 2] or history
    hist_min = sum(m for m, _ in full) / len(full)
    raw, cut_short, blowouts = [], [], 0
    for _ in range(runs):
        g_min, g_pts = full[int(rnd() * len(full))]
        margin = margin_mean + MARGIN_SD * _normal(rnd)
        minutes = min(48.0, max(0.0, g_min + exp_min - hist_min))
        if starter and abs(margin) > 15:
            minutes *= max(0.8, 1 - 0.012 * (abs(margin) - 15))
            blowouts += 1
        exited = rnd() < EARLY_EXIT
        if exited:
            minutes *= 0.15 + 0.35 * rnd()
        cut_short.append(exited)
        raw.append(g_pts / g_min * minutes * max(0.0, 1 + 0.1 * _normal(rnd)))
    mean_raw = sum(raw) / runs
    scale = projection / mean_raw if mean_raw > 0 else 0.0
    # widen normal games only: an early exit is already as far from the center as it gets
    pts = sorted(math.floor(max(0.0, x * scale if short else projection + widen * (x * scale - projection)) + 0.5)
                 for x, short in zip(raw, cut_short))
    over = sum(1 for p in pts if p > line)
    under = sum(1 for p in pts if p < line)
    return {
        "runs": runs, "over": over, "under": under, "push": runs - over - under,
        "p_over": over / (over + under) if over + under else 0.5,
        "p10": pts[int(runs * 0.1)], "median": pts[int(runs * 0.5)], "p90": pts[int(runs * 0.9)],
        "sd": statistics.pstdev(pts), "blowouts": blowouts, "early_exits": sum(cut_short), "outcomes": pts,
    }


def hit_rate(games, line):
    over = sum(1 for g in games if g["pts"] > line)
    under = sum(1 for g in games if g["pts"] < line)
    return over, under


def confidence_label(p):
    edge = abs(p - 0.5)
    if edge < 0.03:
        return "Coin flip"
    if edge < 0.07:
        return "Lean"
    if edge < 0.12:
        return "Moderate"
    return "Strong"


def baseline(games):
    """Weighted blend of last-5, last-10 and last-30 scoring averages."""
    pts = [g["pts"] for g in games]
    l5, l10, l30 = _avg(pts[:5]), _avg(pts[:10]), _avg(pts[:30])
    return 0.25 * l5 + 0.35 * l10 + 0.40 * l30, l5, l10, l30


def split_factor(subset, overall_avg, prior_games, cap):
    """Ratio of a split's average to the overall average, shrunk by sample size."""
    if not subset or overall_avg <= 0:
        return 1.0
    raw = _avg(g["pts"] for g in subset) / overall_avg
    n = len(subset)
    return _clamp(1 + (raw - 1) * n / (n + prior_games), 1 - cap, 1 + cap)


class Factors:
    def __init__(self, base):
        self.base = base
        self.value = base
        self.rows = []

    def add(self, key, name, mult, detail, kind="adjust"):
        before = self.value
        self.value *= mult
        self.rows.append({"key": key, "name": name, "mult": round(mult, 4), "impact": round(self.value - before, 2),
                          "detail": detail, "kind": kind})

    def info(self, name, detail, lean=0):
        """A stat shown to the user that is already priced into another factor."""
        self.rows.append({"name": name, "mult": 1.0, "impact": 0.0, "detail": detail,
                          "kind": "info", "lean": lean})


def _latest_player_stats(pid, cur_players, prev_players):
    p = cur_players.get(pid)
    if p and p["gp"] >= 5:
        return p
    return prev_players.get(pid) or p


def _recent_finals(team_id, season, n):
    finals = [g for g in espn.team_schedule(team_id, season) if g["final"]]
    if len(finals) < n:
        finals = [g for g in espn.team_schedule(team_id, season - 1) if g["final"]] + finals
    return finals[-n:]


def predict(player_query, line=None, now=None, athlete_id=None, params=None):
    """athlete_id skips the name search; params are the learned weights (default: site/params.json)."""
    now = now or datetime.now(timezone.utc)
    season = espn.current_season(now)
    prev = season - 1
    notes = []

    found = {"id": athlete_id, "others": []} if athlete_id else espn.search_player(player_query)
    ath = espn.athlete(found["id"])
    if not ath["team_id"]:
        raise espn.ESPNError(f"{ath['name']} is not on an NBA roster right now.")
    pid, team_id = ath["id"], ath["team_id"]
    pos = espn.pos_bucket(ath["position"])

    (cur_games, prev_games, schedule, injuries, cur_players, prev_players,
     cur_stand, prev_stand) = espn.parallel(lambda f: f(), [
        lambda: espn.gamelog(pid, season),
        lambda: espn.gamelog(pid, prev),
        lambda: espn.team_schedule(team_id, season),
        espn.injuries,
        lambda: espn.league_players(season),
        lambda: espn.league_players(prev),
        lambda: espn.standings(season),
        lambda: espn.standings(prev),
    ])
    # Last season's playoff games are months old and against a playoff-only slate of
    # opponents, so they would skew the baseline; they're shown as info instead.
    old_playoffs = [g for g in prev_games if g["playoff"]]
    prev_games = [g for g in prev_games if not g["playoff"]]
    games = cur_games + prev_games
    if len(games) < 10:
        games += [g for g in espn.gamelog(pid, prev - 1) if not g["playoff"]]
    if len(games) < 3:
        raise espn.ESPNError(f"Not enough NBA games on record for {ath['name']} to make a call.")
    if not cur_games:
        notes.append("No games played yet this season, so the numbers come from last season.")
    elif len(cur_games) < 5:
        notes.append(f"Only {len(cur_games)} game(s) played this season, so last season's games carry most of the weight.")

    # ------------------------------------------------------------ next game
    upcoming = [g for g in schedule if not g["final"] and g["date"] > now - timedelta(hours=4)]
    if not upcoming:
        raise espn.ESPNError(f"No upcoming game found for {ath['team_abbr']}.")
    game = upcoming[0]
    opp_id = game["opp_id"]
    last_final = [g for g in schedule if g["final"] and g["date"] < game["date"]]
    rest_days = (game["date"] - last_final[-1]["date"]).days if last_final else None

    if games[0]["team_id"] != team_id:  # first game on a new team
        notes.append(f"{ath['name']} has changed teams since the last game played; the role on {ath['team_abbr']} may differ.")

    # ------------------------------------------------------------ the line
    line_info = None
    if line is None:
        line_info = espn.player_points_line(game["event_id"], pid)
        if not line_info:
            raise espn.ESPNError(
                f"Couldn't find a posted points line for {ath['name']} vs {game['opp_abbr']}. "
                f"Enter the line yourself, e.g. \"{ath['name']} 22.5\".")
        line = line_info["line"]
    line = float(line)

    # ------------------------------------------------------------ own status
    status = next((i for i in injuries.get(team_id, []) if i["id"] == pid), None)

    # ------------------------------------------------------------ baseline
    base, l5, l10, l30 = baseline(games)
    f = Factors(base)
    mins = [g["min"] for g in games]
    pts = [g["pts"] for g in games]

    f.info("Recent scoring", f"Last 5: {l5:.1f} | Last 10: {l10:.1f} | Last 30: {l30:.1f} ppg "
           f"(baseline {base:.1f})", lean=1 if l5 > line else -1)
    if old_playoffs:
        f.info("Last postseason (not in baseline)",
               f"{_avg(g['pts'] for g in old_playoffs):.1f} ppg in {len(old_playoffs)} playoff games, "
               f"{_avg(g['min'] for g in old_playoffs):.1f} min")
    min5, min30 = _avg(mins[:5]), _avg(mins[:30])
    f.info("Minutes trend", f"{min5:.1f} min over last 5 vs {min30:.1f} over last 30",
           lean=1 if min5 > min30 + 1.5 else -1 if min5 < min30 - 1.5 else 0)
    fga5, fga30 = _avg(g["fga"] for g in games[:5]), _avg(g["fga"] for g in games[:30])
    fta5, fta30 = _avg(g["fta"] for g in games[:5]), _avg(g["fta"] for g in games[:30])
    f.info("Shot volume", f"{fga5:.1f} FGA / {fta5:.1f} FTA last 5 vs {fga30:.1f} / {fta30:.1f} last 30",
           lean=1 if fga5 > fga30 + 1.5 else -1 if fga5 < fga30 - 1.5 else 0)
    fg5 = sum(g["fgm"] for g in games[:5]) / max(1, sum(g["fga"] for g in games[:5]))
    fg30 = sum(g["fgm"] for g in games[:30]) / max(1, sum(g["fga"] for g in games[:30]))
    f.info("Shooting efficiency", f"{fg5:.1%} FG last 5 vs {fg30:.1%} last 30"
           + (" (running hot, regression risk)" if fg5 > fg30 + 0.05 else
              " (running cold, bounce-back candidate)" if fg5 < fg30 - 0.05 else ""),
           lean=-1 if fg5 > fg30 + 0.05 else 1 if fg5 < fg30 - 0.05 else 0)

    # ------------------------------------------------------------ home / away
    split_games = games[:82]
    overall = _avg(g["pts"] for g in split_games)
    side = [g for g in split_games if g["home"] == game["home"]]
    where = "home" if game["home"] else "on the road"
    f.add("home", "Home/away", split_factor(side, overall, 25, 0.05),
          f"Averages {_avg(g['pts'] for g in side):.1f} {where} ({len(side)} games) vs {overall:.1f} overall")

    # ------------------------------------------------------------ rest
    if rest_days is None:
        f.add("rest", "Rest", 1.0, "Season opener, fully rested (no adjustment)")
    elif rest_days <= 1:
        f.add("rest", "Rest", 0.97, "Second night of a back-to-back")
    elif rest_days >= 3:
        f.add("rest", "Rest", 1.01, f"{rest_days} days of rest")
    else:
        f.add("rest", "Rest", 1.0, "Normal rest (1 day off)")

    # ------------------------------------------------------------ team environment
    tseason = season if cur_stand.get(team_id, {}).get("gp", 0) >= 10 else prev
    stand = cur_stand if tseason == season else prev_stand
    if tseason == prev:
        notes.append("Team pace and defense numbers come from last season until teams have 10 games played.")
    team_ids = list(stand.keys())
    tstats = dict(zip(team_ids, espn.parallel(lambda t: espn.team_stats(t, tseason), team_ids)))
    lg_pace = _avg(s["possessions"] for s in tstats.values())
    lg_ppg = _avg(s["ppg"] for s in stand.values())
    team_pace, opp_pace = tstats[team_id]["possessions"], tstats[opp_id]["possessions"]
    team_ppg = stand[team_id]["ppg"]
    opp_allowed = stand[opp_id]["opp_ppg"]
    def_eff = (opp_allowed / opp_pace) / (lg_ppg / lg_pace)
    pace_ratio = (team_pace + opp_pace - lg_pace) / team_pace
    def_rank = sorted(stand, key=lambda t: stand[t]["opp_ppg"] / tstats[t]["possessions"]).index(opp_id) + 1
    pace_rank = sorted(tstats, key=lambda t: -tstats[t]["possessions"]).index(opp_id) + 1

    odds = espn.game_odds(game["event_id"])
    elasticity = 0.85  # a player's points move a bit less than 1:1 with team points
    stat_w = 0.35 if odds else 1.0
    f.add("defense", "Opponent defense", 1 + (def_eff - 1) * stat_w * elasticity,
          f"{game['opp_abbr']} allow {opp_allowed:.1f} ppg, {_ordinal(def_rank)} best per possession "
          f"({def_eff * 100 - 100:+.1f}% vs league)")
    f.add("pace", "Pace", 1 + (pace_ratio - 1) * stat_w * elasticity,
          f"{game['opp_abbr']} play at {opp_pace:.1f} possessions/game ({_ordinal(pace_rank)} fastest); "
          f"projected game pace {pace_ratio * 100 - 100:+.1f}% vs {ath['team_abbr']}'s norm")
    if odds:
        spread = odds["spread"] or 0.0
        implied = odds["total"] / 2 - spread / 2 if game["home"] else odds["total"] / 2 + spread / 2
        vegas = implied / team_ppg
        f.add("vegas", "Vegas implied team total", 1 + (vegas - 1) * 0.65 * elasticity,
              f"Total {odds['total']}, {odds['details']} -> {ath['team_abbr']} implied {implied:.1f} pts "
              f"vs {team_ppg:.1f} avg ({odds['provider']})")
    else:
        spread = None

    # ------------------------------------------------------------ positional defense / opposing players
    opp_recent = _recent_finals(opp_id, season, 12)
    opp_boxes = espn.parallel(lambda g: espn.box_score(g["event_id"]), opp_recent)
    players_by_season = {season: cur_players, prev: prev_players}
    act = {"G": 0.0, "F": 0.0, "C": 0.0}
    exp = {"G": 0.0, "F": 0.0, "C": 0.0}
    for g, box in zip(opp_recent, opp_boxes):
        lp = players_by_season.get(espn.current_season(g["date"]), prev_players)
        for tid, side_box in box.items():
            if tid == opp_id:
                continue
            for p in side_box["players"]:
                s = lp.get(p["id"])
                if p["min"] < 10 or not s or s["min"] <= 0:
                    continue
                act[p["pos"]] += p["pts"]
                exp[p["pos"]] += s["pts"] / s["min"] * p["min"]
    if exp[pos] >= 50 and sum(exp.values()) > 0:
        pos_ratio = act[pos] / exp[pos]
        all_ratio = sum(act.values()) / sum(exp.values())
        rel = pos_ratio / all_ratio
        label = {"G": "guards", "F": "forwards", "C": "centers"}[pos]
        f.add("position", f"Defense vs {label}", _clamp(1 + (rel - 1) * 0.5, 0.94, 1.06),
              f"Last {len(opp_recent)} games: opposing {label} scored {pos_ratio * 100 - 100:+.0f}% vs their "
              f"usual output ({rel * 100 - 100:+.0f}% relative to {game['opp_abbr']}'s overall defense)")

    opp_inj = {i["id"]: i for i in injuries.get(opp_id, [])}
    opp_roster = espn.roster(opp_id)
    defenders = []
    for r in opp_roster:
        if r["pos"] != pos or opp_inj.get(r["id"], {}).get("status") == "Out":
            continue
        s = _latest_player_stats(r["id"], cur_players, prev_players)
        if s and s["min"] >= 15:
            defenders.append((s["min"], r["name"], s))
    defenders.sort(reverse=True)
    if defenders:
        f.info("Likely primary defenders", "; ".join(
            f"{name} ({s['min']:.0f} min, {s['stl']:.1f} stl, {s['blk']:.1f} blk, {s['pf']:.1f} fouls)"
            for _, name, s in defenders[:2]))
    os_ = tstats[opp_id]
    stl_rank = sorted(tstats, key=lambda t: -tstats[t].get("avgSteals", 0)).index(opp_id) + 1
    blk_rank = sorted(tstats, key=lambda t: -tstats[t].get("avgBlocks", 0)).index(opp_id) + 1
    f.info("Opponent team defense", f"{os_.get('avgSteals', 0):.1f} steals ({_ordinal(stl_rank)}), "
           f"{os_.get('avgBlocks', 0):.1f} blocks ({_ordinal(blk_rank)}), "
           f"{os_.get('avgDefensiveRebounds', 0):.1f} def. rebounds per game")

    # opponents who are out, weighted by how much they've actually been playing
    recent_opp_ids = [{p["id"] for p in b.get(opp_id, {}).get("players", [])} for b in opp_boxes[-5:]]
    missing_opp = []
    for i in opp_inj.values():
        if i["status"] != "Out":
            continue
        s = _latest_player_stats(i["id"], cur_players, prev_players)
        played = sum(1 for ids in recent_opp_ids if i["id"] in ids)
        if s and s["min"] >= 24 and played >= 3:
            missing_opp.append(f"{i['name']} ({s['min']:.0f} min)")
    if missing_opp:
        f.add("opp_injuries", "Opponent injuries", 1 + min(0.04, 0.015 * len(missing_opp)), "Out: " + ", ".join(missing_opp))

    # ------------------------------------------------------------ head to head
    h2h = [g for g in games if g["opp_id"] == opp_id and g["season"] >= prev]
    h2h_pool = [g for g in games if g["season"] >= prev]
    if h2h:
        f.add("h2h", f"History vs {game['opp_abbr']}", split_factor(h2h, _avg(g["pts"] for g in h2h_pool), 8, 0.06),
              f"{_avg(g['pts'] for g in h2h):.1f} ppg in {len(h2h)} game(s) vs {game['opp_abbr']} "
              f"since last season: " + ", ".join(str(int(g["pts"])) for g in h2h[:6]))

    # ------------------------------------------------------------ teammates out
    team_inj = [i for i in injuries.get(team_id, []) if i["id"] != pid and i["status"] in ("Out", "Doubtful")]
    if team_inj:
        recent_events = {g["event_id"] for g in games[:5]}
        player_ppg = _avg(pts[:30])
        share = player_ppg / team_ppg if team_ppg else 0
        missing_pts, missing_names, baked_in = 0.0, [], []
        for i in team_inj:
            s = _latest_player_stats(i["id"], cur_players, prev_players)
            if not s or s["pts"] < 5:
                continue
            weight = 1.0 if i["status"] == "Out" else 0.75
            # did this teammate play in the player's last 5 games?
            teammate_games = espn.gamelog(i["id"], season) + espn.gamelog(i["id"], prev)
            played = sum(1 for g in teammate_games if g["event_id"] in recent_events)
            if played >= 3:
                missing_pts += s["pts"] * weight
                missing_names.append(f"{i['name']} ({s['pts']:.1f} ppg, {i['status']})")
            else:
                baked_in.append(i["name"])
        if missing_pts:
            boost = missing_pts * share * 0.6
            f.add("teammates", "Teammates out (usage bump)", min(1.15, 1 + boost / f.value),
                  ", ".join(missing_names) + f". {ath['name']}'s {share:.0%} share of team scoring absorbs some of it")
        if baked_in:
            f.info("Teammates out (already priced in)",
                   ", ".join(baked_in) + " also missed recent games, so the baseline already reflects it")

    # ------------------------------------------------------------ blowout risk
    if spread is not None and min30 >= 28 and abs(spread) > 8:
        f.add("blowout", "Blowout risk", 1 - min(0.06, 0.01 * (abs(spread) - 8)),
              f"{abs(spread):.1f}-point spread: starters can sit late in lopsided games")

    # ------------------------------------------------------------ market prior
    # Sportsbook lines react to news the box scores can't see yet (role changes,
    # minutes limits, lineup news), so the projection is pulled partway toward it.
    stats_projection = f.value
    f.add("market", "Sportsbook line (market prior)", (0.65 * stats_projection + 0.35 * line) / stats_projection,
          f"Stats alone project {stats_projection:.1f}; the line is {line:g}. Projection is pulled 35% toward the line")
    gap = abs(stats_projection - line) / line
    role_change = games[0]["team_id"] != team_id

    # ------------------------------------------------------------ probability
    projection = f.value
    exp_min = 0.25 * min5 + 0.35 * _avg(mins[:10]) + 0.40 * min30
    history = [(g["min"], g["pts"]) for g in games[:25]]
    margin_mean = 0.0 if spread is None else (-spread if game["home"] else spread)
    # less sure of the outcome when the role is new or the book strongly disagrees
    widen = 1.25 if gap > 0.25 or role_change else 1.0
    sim = simulate(projection, line, history, exp_min, margin_mean, min30 >= 28, widen,
                   mulberry32(seed_for(pid, game["event_id"], math.floor(line * 10 + 0.5))))
    window = games[:20]
    over_n, under_n = hit_rate(window, line)
    p_emp = (over_n + 5) / (over_n + under_n + 10)
    p_base = 0.75 * sim["p_over"] + 0.25 * p_emp
    x = learn.features(projection, stats_projection, line, sim["sd"], p_emp, f.rows, role_change,
                       len(cur_games) < 5, bool(status))
    params = learn.load_params() if params is None else params
    p_over = learn.apply(params, p_base, x)
    if params.get("active"):
        f.info("Learned adjustment", f"{(p_over - p_base) * 100:+.1f} points of over-probability from "
               f"{params['trained_on']} graded picks", lean=1 if p_over > p_base else -1 if p_over < p_base else 0)

    hit_rates = {}
    for label, sample in (("Last 5", games[:5]), ("Last 10", games[:10]), ("Last 20", games[:20]),
                          ("This season" if len(cur_games) >= 5 else "Last season",
                           cur_games if len(cur_games) >= 5 else prev_games)):
        o, u = hit_rate(sample, line)
        hit_rates[label] = {"over": o, "under": u, "games": len(sample)}
    f.info("Hit rate vs this line", " | ".join(
        f"{k}: {v['over']}/{v['games']} over" for k, v in hit_rates.items()),
        lean=1 if over_n > under_n else -1 if under_n > over_n else 0)

    pick = "OVER" if p_over >= 0.5 else "UNDER"
    p_pick = p_over if pick == "OVER" else 1 - p_over
    confidence = confidence_label(p_over)
    if status and status["status"] == "Out":
        pick, confidence = "NO PLAY", "Player is listed OUT"
    elif status:
        notes.append(f"{ath['name']} is listed {status['status']}: {status['comment']}")
        if confidence == "Strong":
            confidence = "Moderate"
    if gap > 0.25 and pick != "NO PLAY":
        if confidence == "Strong":
            confidence = "Moderate"
        notes.append(f"The stats and the line disagree by {gap:.0%}. If this is a real sportsbook line, a gap that big "
                     "usually means the book knows something (new role, minutes limit, injury) that past games can't show, "
                     "so the bot widened its uncertainty.")
    if pick != "NO PLAY" and p_pick < BREAK_EVEN:
        notes.append(f"{p_pick:.1%} is below the {BREAK_EVEN:.1%} needed to profit at -110 odds; treat it as a lean, not a bet.")

    return Prediction(
        player={"id": pid, "name": ath["name"], "team": ath["team_abbr"], "position": ath["position"],
                "headshot": ath["headshot"], "matched_from": player_query, "other_matches": found["others"]},
        game={"event_id": game["event_id"], "date": game["date"].isoformat(), "home": game["home"],
              "opponent": game["opp_abbr"], "opponent_name": game["opp_name"],
              "rest_days": rest_days},
        line=line,
        line_source=line_info,
        injury_status=status,
        pick=pick,
        confidence=confidence,
        probability=round(p_pick, 4),
        p_over=round(p_over, 4),
        p_base=round(p_base, 4),
        stats_projection=round(stats_projection, 2),
        features=x,
        learned=bool(params.get("active")),
        p_sim=round(sim["p_over"], 4),
        simulation=sim,
        p_hit_rate=round(p_emp, 4),
        projection=round(projection, 1),
        baseline=round(base, 1),
        std_dev=round(sim["sd"], 1),
        factors=f.rows,
        hit_rates=hit_rates,
        recent_games=[{"date": g["date"].date().isoformat(), "opp": ("vs " if g["home"] else "@ ") + (g["opp_abbr"] or ""),
                       "min": g["min"], "pts": g["pts"]} for g in games[:10]],
        notes=notes,
    )
