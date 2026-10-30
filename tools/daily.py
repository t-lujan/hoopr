"""The bot's learning loop. Run by .github/workflows/learn.yml.

    python3 tools/daily.py record   # before tip-off: predict every posted points line, save the picks
    python3 tools/daily.py grade    # after the games: fill in actual points and win/loss
    python3 tools/daily.py learn    # refit the learning layer, write site/params.json + site/record.json

Picks live in data/picks/<game date, US Eastern>.json, one record per player per
game. Recording again before tip-off replaces the record with the newer line, so
the saved pick is the last one made before the game started.
"""

import glob
import json
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from hoopr import espn, learn  # noqa: E402
from hoopr.predict import BREAK_EVEN, predict, price_pair  # noqa: E402

PICKS = os.path.join(ROOT, "data", "picks")
RECORD_PATH = os.path.join(ROOT, "site", "record.json")
ET = ZoneInfo("America/New_York")
TIERS = ["Coin flip", "Lean", "Moderate", "Strong"]
WIN_UNITS = 100 / 110  # profit on a winning -110 bet of 1 unit


def game_day(dt):
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def load_day(day):
    path = os.path.join(PICKS, f"{day}.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save_day(day, rows):
    os.makedirs(PICKS, exist_ok=True)
    rows.sort(key=lambda r: (r["tipoff"], r["player"]))
    with open(os.path.join(PICKS, f"{day}.json"), "w") as f:
        json.dump(rows, f, indent=1)
        f.write("\n")


def all_rows():
    rows = []
    for path in sorted(glob.glob(os.path.join(PICKS, "*.json"))):
        with open(path) as f:
            rows.extend(json.load(f))
    return sorted(rows, key=lambda r: (r["tipoff"], r["id"]))


def make_record(r, game, recorded_at, source="live"):
    """Trim a prediction to what grading, learning and the record page need."""
    sim = r["simulation"]
    return {
        "id": f"{game['event_id']}|{r['player']['id']}",
        "event_id": game["event_id"], "tipoff": game["tipoff"], "player_id": r["player"]["id"],
        "player": r["player"]["name"], "team": r["player"]["team"], "opponent": r["game"]["opponent"],
        "home": r["game"]["home"], "line": r["line"], "prices": r["prices"], "p_market": r["p_market"], "break_even": r["break_even"],
        "pick": r["pick"], "confidence": r["confidence"], "p_over": r["p_over"], "p_base": r["p_base"],
        "projection": r["projection"], "stats_projection": r["stats_projection"],
        "sim": {k: sim[k] for k in ("over", "under", "push", "median", "p10", "p90")},
        "features": r["features"], "learned": r["learned"],
        "recorded_at": recorded_at, "source": source, "result": None,
    }


# ---------------------------------------------------------------- record

def record(now=None):
    now = now or datetime.now(timezone.utc)
    params = learn.load_params()
    games = []
    for day in (now.astimezone(ET), now.astimezone(ET) + timedelta(days=1)):
        for g in espn.scoreboard(day.strftime("%Y%m%d")):
            # not started yet, and close enough that lines are posted
            if g["state"] == "pre" and now + timedelta(minutes=5) < g["date"] < now + timedelta(hours=20):
                games.append(g)
    jobs = []
    for g in games:
        game = {"event_id": g["event_id"], "tipoff": g["date"].isoformat()}
        for pid, li in espn.points_lines(g["event_id"]).items():
            jobs.append((game, pid, li))
    print(f"{len(games)} upcoming games, {len(jobs)} posted points lines", flush=True)

    def run(job):
        game, pid, li = job
        try:
            r = predict(None, li["line"], now=now, athlete_id=pid, params=params, line_info=li)
        except Exception as e:  # one bad player must not sink the run
            return None, f"{pid} {game['event_id']}: {e}"
        if r["game"]["event_id"] != game["event_id"]:
            return None, f"{r['player']['name']}: next game isn't {game['event_id']}"
        return make_record(r, game, now.isoformat(timespec="seconds")), None

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(run, jobs))
    by_day = {}
    for rec, err in results:
        if err:
            print("skip:", err, flush=True)
        elif rec:
            by_day.setdefault(game_day(datetime.fromisoformat(rec["tipoff"])), []).append(rec)
    for day, recs in by_day.items():
        existing = {r["id"]: r for r in load_day(day)}
        for rec in recs:
            old = existing.get(rec["id"])
            if old and old.get("result"):
                continue  # already graded, never rewrite history
            existing[rec["id"]] = rec
        save_day(day, list(existing.values()))
        print(f"{day}: {len(recs)} picks recorded ({len(existing)} on file)", flush=True)


# ---------------------------------------------------------------- grade

def grade_row(r, box):
    if box is None:
        return None
    player = next((p for side in box.values() for p in side["players"] if p["id"] == r["player_id"]), None)
    if not player or player["min"] <= 0:
        return {"status": "void", "reason": "did not play"}
    pts = player["pts"]
    outcome = "over" if pts > r["line"] else "under" if pts < r["line"] else "push"
    won = None if outcome == "push" or r["pick"] not in ("OVER", "UNDER") else r["pick"].lower() == outcome
    base_pick = "over" if r["p_base"] >= 0.5 else "under"
    base_won = None if outcome == "push" else base_pick == outcome
    return {"status": "graded", "pts": pts, "min": player["min"], "outcome": outcome, "won": won, "base_won": base_won}


def grade(now=None):
    now = now or datetime.now(timezone.utc)
    for path in sorted(glob.glob(os.path.join(PICKS, "*.json"))):
        day = os.path.basename(path)[:-5]
        rows = load_day(day)
        todo = [r for r in rows if not r.get("result")
                and datetime.fromisoformat(r["tipoff"]) < now - timedelta(hours=2, minutes=30)]
        if not todo:
            continue
        boxes = {eid: espn.final_box(eid) for eid in {r["event_id"] for r in todo}}
        n = 0
        for r in todo:
            res = grade_row(r, boxes[r["event_id"]])
            if res:
                r["result"] = res
                n += 1
        save_day(day, rows)
        print(f"{day}: graded {n} of {len(todo)} waiting picks", flush=True)


# ---------------------------------------------------------------- learn + report

def win_units(r):
    """Profit on a winning 1-unit bet at the picked side's own price; -110 when the record has none."""
    prices = r.get("prices")
    a = price_pair(prices).get(r["pick"].lower()) if isinstance(prices, dict) else None
    if a is None:
        return WIN_UNITS
    return a / 100 if a > 0 else 100 / -a


def tally(rows, key="won"):
    w = sum(1 for r in rows if r["result"].get(key) is True)
    l = sum(1 for r in rows if r["result"].get(key) is False)
    units = sum(win_units(r) for r in rows if r["result"].get(key) is True) - l
    return {"w": w, "l": l, "p": len(rows) - w - l, "win_pct": round(w / (w + l), 4) if w + l else None,
            "units": round(units, 2)}


def report(rows, params):
    graded = [r for r in rows if (r.get("result") or {}).get("status") == "graded" and r["pick"] in ("OVER", "UNDER")]
    p_pick = lambda r: r["p_over"] if r["pick"] == "OVER" else 1 - r["p_over"]  # noqa: E731
    days = sorted({game_day(datetime.fromisoformat(r["tipoff"])) for r in graded})
    calib = []
    for lo, hi in ((0.5, 0.53), (0.53, 0.57), (0.57, 0.62), (0.62, 1.01)):
        b = [r for r in graded if lo <= p_pick(r) < hi and r["result"]["won"] is not None]
        if b:
            calib.append({"lo": lo, "hi": min(hi, 1.0), "n": len(b), "predicted": round(sum(map(p_pick, b)) / len(b), 4),
                          "actual": round(sum(1 for r in b if r["result"]["won"]) / len(b), 4)})
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "break_even": BREAK_EVEN,
        "since": days[0] if days else None,
        "overall": tally(graded),
        "base_model": tally(graded, "base_won"),
        "by_confidence": {t: tally([r for r in graded if r["confidence"] == t]) for t in TIERS},
        "by_side": {s: tally([r for r in graded if r["pick"] == s]) for s in ("OVER", "UNDER")},
        "by_day": [{"day": d, **tally([r for r in graded if game_day(datetime.fromisoformat(r["tipoff"])) == d])}
                   for d in days[-14:]],
        "calibration": calib,
        "pending": sum(1 for r in rows if not r.get("result")),
        "learning": {k: params.get(k) for k in ("active", "trained_on", "holdout", "base_log_loss", "learned_log_loss",
                                                 "coin_log_loss", "live_log_loss", "gate", "reason", "weights", "bias", "updated")},
    }


def learn_step():
    rows = all_rows()
    train_rows = [{"p_base": r["p_base"], "x": r["features"], "y": 1 if r["result"]["outcome"] == "over" else 0}
                  for r in rows if (r.get("result") or {}).get("status") == "graded" and r["result"]["outcome"] != "push"]
    params = learn.train(train_rows, live_params=learn.load_params())
    params["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(learn.PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=1)
        f.write("\n")
    with open(RECORD_PATH, "w") as f:
        json.dump(report(rows, params), f, indent=1)
        f.write("\n")
    print(params["reason"], flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    steps = {"record": record, "grade": grade, "learn": learn_step}
    if cmd not in steps:
        sys.exit(__doc__)
    try:
        steps[cmd]()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
