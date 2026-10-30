import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from hoopr import espn, learn, predict as engine
from hoopr.cli import parse_query

NOW = datetime(2027, 1, 15, 12, tzinfo=timezone.utc)
TEAM, OPP = "1", "2"
TEAMS = [str(i) for i in range(1, 31)]
HIST = [(34, 25), (36, 31), (30, 18), (35, 27), (33, 22), (38, 33), (12, 4), (34, 24)]  # (minutes, points) of recent games


def game(i, pts, home=True, opp=OPP, season=2027, playoff=False):
    return {"event_id": f"g{i}", "date": NOW - timedelta(days=2 * i + 1), "season": season, "playoff": playoff,
            "home": home, "team_id": TEAM, "opp_id": opp, "opp_abbr": "OPP", "min": 34.0, "pts": float(pts),
            "fgm": 9.0, "fga": 19.0, "tpm": 2.0, "tpa": 6.0, "ftm": 4.0, "fta": 5.0, "reb": 5.0, "ast": 5.0}


def fake_espn(games, prev_games=(), injuries=None, line=None, odds=None, last_game_days_ago=2):
    """Patch every network call with a small, fully controlled league."""
    sched = [{"event_id": "next", "date": NOW + timedelta(hours=8), "home": True, "opp_id": OPP,
              "opp_abbr": "OPP", "opp_name": "Opponents", "final": False, "pts": None, "opp_pts": None},
             {"event_id": "last", "date": NOW - timedelta(days=last_game_days_ago), "home": True, "opp_id": "3",
              "opp_abbr": "T3", "opp_name": "Team 3", "final": True, "pts": 110.0, "opp_pts": 105.0}]
    sched.sort(key=lambda g: g["date"])
    stand = {t: {"abbr": f"T{t}", "gp": 40, "ppg": 112.0, "opp_ppg": 112.0} for t in TEAMS}
    stats = {"possessions": 100.0, "avgSteals": 8.0, "avgBlocks": 5.0, "avgDefensiveRebounds": 33.0}
    players = {"p1": {"name": "Star", "team_id": TEAM, "pos": "G", "gp": 40, "min": 34, "pts": 25,
                      "fga": 19, "stl": 1, "blk": 0.5, "reb": 5, "pf": 2}}
    patches = {
        "search_player": lambda q: {"id": "p1", "name": "Star", "others": []},
        "athlete": lambda i: {"id": "p1", "name": "Star", "team_id": TEAM, "team_abbr": "T1",
                              "team_name": "Team 1", "position": "PG", "headshot": None},
        "gamelog": lambda i, s: list(games) if s == 2027 else list(prev_games) if s == 2026 else [],
        "team_schedule": lambda t, s: sched if s == 2027 else [],
        "injuries": lambda: injuries or {},
        "league_players": lambda s: players,
        "standings": lambda s: stand,
        "team_stats": lambda t, s: dict(stats),
        "game_odds": lambda e: odds,
        "player_points_line": lambda e, a: line,
        "box_score": lambda e: {},
        "roster": lambda t: [],
    }
    return _Both(mock.patch.multiple(espn, **patches), mock.patch.object(learn, "load_params", lambda: {}))


class _Both:
    """Patch ESPN and keep the repo's learned weights out of the engine tests."""

    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.__enter__()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)


class PureFunctions(unittest.TestCase):
    def test_parse_query(self):
        self.assertEqual(parse_query("lebron james 24.5"), ("lebron james", 24.5))
        self.assertEqual(parse_query("  brunson  "), ("brunson", None))
        self.assertEqual(parse_query("Jokic o/u 27"), ("Jokic", 27.0))
        self.assertEqual(parse_query("Shai, 31.5"), ("Shai", 31.5))

    def test_simulation_runs_and_centers_on_projection(self):
        sim = engine.simulate(25.0, 24.5, HIST, 34, 0.0, True, 1.0, engine.mulberry32(1))
        self.assertEqual(sim["runs"], engine.SIMS)
        self.assertEqual(len(sim["outcomes"]), engine.SIMS)
        self.assertEqual(sim["over"] + sim["under"] + sim["push"], engine.SIMS)
        self.assertAlmostEqual(sum(sim["outcomes"]) / engine.SIMS, 25.0, delta=0.5)
        self.assertLessEqual(sim["p10"], sim["median"])
        self.assertLessEqual(sim["median"], sim["p90"])

    def test_simulation_is_repeatable(self):
        args = (25.0, 24.5, HIST, 34, -6.0, True, 1.0)
        a = engine.simulate(*args, engine.mulberry32(engine.seed_for("p1", "g1", 245)))
        b = engine.simulate(*args, engine.mulberry32(engine.seed_for("p1", "g1", 245)))
        self.assertEqual(a["outcomes"], b["outcomes"])

    def test_simulation_follows_the_projection(self):
        low = engine.simulate(15.0, 24.5, HIST, 34, 0.0, True, 1.0, engine.mulberry32(7))
        high = engine.simulate(35.0, 24.5, HIST, 34, 0.0, True, 1.0, engine.mulberry32(7))
        self.assertLess(low["p_over"], 0.2)
        self.assertGreater(high["p_over"], 0.8)

    def test_widen_spreads_outcomes_without_moving_center(self):
        tight = engine.simulate(25.0, 24.5, HIST, 34, 0.0, True, 1.0, engine.mulberry32(5))
        wide = engine.simulate(25.0, 24.5, HIST, 34, 0.0, True, 1.25, engine.mulberry32(5))
        self.assertGreater(wide["sd"], tight["sd"])
        self.assertAlmostEqual(sum(wide["outcomes"]) / engine.SIMS, 25.0, delta=0.6)

    def test_blowouts_only_trim_starters(self):
        bench = engine.simulate(10.0, 9.5, HIST, 18, 20.0, False, 1.0, engine.mulberry32(3))
        starter = engine.simulate(25.0, 24.5, HIST, 34, 20.0, True, 1.0, engine.mulberry32(3))
        self.assertEqual(bench["blowouts"], 0)
        self.assertGreater(starter["blowouts"], 40)

    def test_rng_matches_reference_values(self):
        # mulberry32(1) reference output, shared with the browser engine
        rnd = engine.mulberry32(1)
        self.assertEqual([round(rnd(), 10) for _ in range(3)], [0.6270739406, 0.0027357212, 0.52744704])

    def test_hit_rate_ignores_pushes(self):
        gs = [{"pts": p} for p in (10, 20, 20, 30)]
        self.assertEqual(engine.hit_rate(gs, 20), (1, 1))

    def test_split_factor_shrinks_small_samples(self):
        one = engine.split_factor([{"pts": 40}], 20, 25, 0.05)
        many = engine.split_factor([{"pts": 22}] * 200, 20, 25, 0.5)
        self.assertLess(one, 1.05)
        self.assertGreater(one, 1.0)
        self.assertAlmostEqual(many, 1 + 0.1 * 200 / 225)

    def test_confidence_labels(self):
        self.assertEqual(engine.confidence_label(0.51), "Coin flip")
        self.assertEqual(engine.confidence_label(0.45), "Lean")
        self.assertEqual(engine.confidence_label(0.60), "Moderate")
        self.assertEqual(engine.confidence_label(0.20), "Strong")

    def test_season_and_positions(self):
        self.assertEqual(espn.current_season(datetime(2026, 10, 19)), 2027)
        self.assertEqual(espn.current_season(datetime(2027, 3, 1)), 2027)
        self.assertEqual([espn.pos_bucket(p) for p in ("PG", "SF", "C", "F-C", "", None)],
                         ["G", "F", "C", "C", "G", "G"])


class EndToEnd(unittest.TestCase):
    def test_scorer_well_above_line_is_over(self):
        games = [game(i, 30 + (i % 3)) for i in range(30)]
        with fake_espn(games):
            r = engine.predict("star", 20.5, now=NOW)
        self.assertEqual(r["pick"], "OVER")
        self.assertGreater(r["probability"], 0.7)
        self.assertEqual(r["game"]["opponent"], "OPP")

    def test_scorer_well_below_line_is_under(self):
        games = [game(i, 12 + (i % 3)) for i in range(30)]
        with fake_espn(games):
            r = engine.predict("star", 20.5, now=NOW)
        self.assertEqual(r["pick"], "UNDER")

    def test_line_is_pulled_when_not_given(self):
        games = [game(i, 25) for i in range(30)]
        line = {"line": 24.5, "prices": ["-110", "-110"], "provider": "Book", "updated": "x"}
        with fake_espn(games, line=line):
            r = engine.predict("star", None, now=NOW)
        self.assertEqual(r["line"], 24.5)
        self.assertEqual(r["line_source"]["provider"], "Book")

    def test_missing_line_asks_user(self):
        with fake_espn([game(i, 25) for i in range(30)]):
            with self.assertRaises(espn.ESPNError):
                engine.predict("star", None, now=NOW)

    def test_player_out_is_no_play(self):
        inj = {TEAM: [{"id": "p1", "name": "Star", "status": "Out", "comment": "knee"}]}
        with fake_espn([game(i, 25) for i in range(30)], injuries=inj):
            r = engine.predict("star", 20.5, now=NOW)
        self.assertEqual(r["pick"], "NO PLAY")

    def test_back_to_back_lowers_projection(self):
        games = [game(i, 25) for i in range(30)]
        with fake_espn(games):
            rested = engine.predict("star", 24.5, now=NOW)
        with fake_espn(games, last_game_days_ago=1):
            tired = engine.predict("star", 24.5, now=NOW)
        rest_row = next(f for f in tired["factors"] if f["name"] == "Rest")
        self.assertLess(rest_row["mult"], 1)
        self.assertLess(tired["projection"], rested["projection"])

    def test_vegas_total_moves_projection(self):
        games = [game(i, 25) for i in range(30)]
        with fake_espn(games, odds={"provider": "Book", "total": 250.0, "spread": 0.0, "details": "EVEN"}):
            high = engine.predict("star", 24.5, now=NOW)
        with fake_espn(games, odds={"provider": "Book", "total": 200.0, "spread": 0.0, "details": "EVEN"}):
            low = engine.predict("star", 24.5, now=NOW)
        self.assertGreater(high["projection"], low["projection"])

    def test_old_playoff_games_excluded_from_baseline(self):
        cur = [game(i, 20) for i in range(20)]
        prev = [game(100 + i, 50, season=2026, playoff=True) for i in range(10)]
        with fake_espn(cur, prev_games=prev):
            r = engine.predict("star", 24.5, now=NOW)
        self.assertAlmostEqual(r["baseline"], 20.0)


class Market(unittest.TestCase):
    def test_market_p_over(self):
        self.assertAlmostEqual(engine.market_p_over([-110, -110]), 0.5)
        self.assertAlmostEqual(engine.market_p_over(["-110", "-110"]), 0.5)
        io, iu = 150 / 250, 100 / 230
        self.assertAlmostEqual(engine.market_p_over([-150, 130]), io / (io + iu))
        self.assertGreater(engine.market_p_over([-150, 130]), 0.5)
        for bad in (None, [], [-120], [-120, None], [None, 105], ["x", "y"], {"over": -120}):
            self.assertEqual(engine.market_p_over(bad), 0.5)

    def test_break_even_uses_picked_side(self):
        prices = {"over": -150, "under": 130}
        self.assertAlmostEqual(engine.break_even_for(prices, "OVER"), 0.6)
        self.assertAlmostEqual(engine.break_even_for(prices, "UNDER"), 100 / 230)
        self.assertEqual(engine.break_even_for(None, "OVER"), engine.BREAK_EVEN)

    def test_line_info_supplies_line_and_prices(self):
        info = {"line": 24.5, "prices": ["-120", "100"], "provider": "Book"}
        with fake_espn([game(i, 25) for i in range(30)]):
            r = engine.predict("star", now=NOW, line_info=info)
        self.assertEqual(r["line"], 24.5)
        self.assertEqual(r["prices"], {"over": -120, "under": 100})
        self.assertIn(r["break_even"], (round(120 / 220, 4), 0.5))
        self.assertGreater(r["p_market"], 0.5)

    def test_market_prior_is_80_percent(self):
        with fake_espn([game(i, 25) for i in range(30)]):
            r = engine.predict("star", 30.5, now=NOW)
        row = next(f for f in r["factors"] if f.get("key") == "market")
        self.assertIn("80%", row["detail"])


if __name__ == "__main__":
    unittest.main()
