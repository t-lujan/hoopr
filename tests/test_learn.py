import math
import random
import sys
import unittest
from pathlib import Path

from hoopr import learn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import daily  # noqa: E402


def synthetic(n, true_weight, seed):
    """Picks where the base model is a coin flip but 'edge' really predicts the result."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x = {k: 0.0 for k in learn.FEATURES}
        x["edge"] = rng.gauss(0, 1)
        p = 1 / (1 + math.exp(-true_weight * x["edge"]))
        rows.append({"p_base": 0.5, "x": x, "y": 1 if rng.random() < p else 0})
    return rows


class Learning(unittest.TestCase):
    def test_features_sum_factor_impacts_in_sd_units(self):
        rows = [{"key": "home", "impact": 1.0}, {"key": "rest", "impact": -0.5}, {"key": "market", "impact": 2.0}]
        x = learn.features(26.0, 27.0, 24.5, 5.0, 0.5, rows, False, True, False)
        self.assertTrue(set(learn.FEATURES) <= set(x))
        self.assertAlmostEqual(x["edge"], 0.3)
        self.assertAlmostEqual(x["stats_edge"], 0.5)
        self.assertAlmostEqual(x["f_home"], 0.2)
        self.assertAlmostEqual(x["f_rest"], -0.1)
        self.assertEqual(x["early_season"], 1.0)
        self.assertAlmostEqual(x["hit_rate"], 0.0)

    def test_inactive_params_leave_probability_alone(self):
        x = {k: 1.0 for k in learn.FEATURES}
        self.assertEqual(learn.apply({}, 0.61, x), 0.61)
        self.assertEqual(learn.apply({"active": False, "weights": {"edge": 5}}, 0.61, x), 0.61)
        self.assertAlmostEqual(learn.apply({"active": True, "bias": 0, "weights": {}}, 0.61, x), 0.61)

    def test_fit_recovers_a_real_signal(self):
        bias, w = learn.fit(synthetic(3000, 0.8, 1))
        self.assertGreater(w["edge"], 0.6)
        self.assertLess(abs(bias), 0.15)
        self.assertLess(max(abs(v) for k, v in w.items() if k != "edge"), 1e-6)
        self.assertEqual(set(w), {"edge", "stats_edge", "hit_rate", "listed"})

    def test_too_few_picks_never_learns(self):
        out = learn.train(synthetic(learn.MIN_GRADED - 1, 2.0, 2))
        self.assertFalse(out["active"])
        self.assertIn("Needs", out["reason"])

    def test_real_signal_goes_live(self):
        out = learn.train(synthetic(1000, 1.0, 3))
        self.assertTrue(out["active"])
        self.assertEqual(out["gate"], "passed")
        self.assertLess(out["learned_log_loss"], out["coin_log_loss"])
        self.assertLess(out["learned_log_loss"], out["base_log_loss"])
        self.assertEqual(set(out["weights"]), set(learn.FEATURES))

    def test_gate_refuses_pure_noise(self):
        for seed in range(5, 10):
            out = learn.train(synthetic(800, 0.0, seed))
            self.assertFalse(out["active"], seed)
            self.assertEqual(out["gate"], "failed")
            self.assertIn("margin_vs_coin", out)

    def test_failed_gate_keeps_old_live_params(self):
        live = {"active": True, "bias": 0.01, "weights": {"edge": 0.02, "role_change": -0.1}, "features": ["edge", "role_change"]}
        out = learn.train(synthetic(800, 0.0, 5), live_params=live)
        self.assertEqual(out["gate"], "failed")
        self.assertTrue(out["active"])
        self.assertEqual(out["bias"], 0.01)
        self.assertEqual(out["weights"], live["weights"])
        self.assertEqual(out["features"], live["features"])
        self.assertIn("live_log_loss", out)

    def test_apply_uses_the_params_own_keys(self):
        old = {"active": True, "bias": 0.0, "weights": {k: 0.0 for k in
               ["edge", "stats_edge", "hit_rate", "role_change", "early_season", "listed"] + [f"f_{k}" for k in learn.FACTOR_KEYS]}}
        old["weights"]["role_change"] = 0.5
        old["weights"]["f_home"] = -0.5
        x = {"role_change": 1.0, "f_home": 1.0}
        self.assertAlmostEqual(learn.apply(old, 0.6, x), 0.6)
        old["weights"]["f_home"] = 0.0
        self.assertGreater(learn.apply(old, 0.6, x), 0.6)


class Grading(unittest.TestCase):
    BOX = {"1": {"players": [{"id": "p1", "pts": 25.0, "min": 34.0}, {"id": "p2", "pts": 0.0, "min": 0.0}]}}

    def row(self, pid="p1", line=24.5, pick="OVER", p_base=0.6):
        return {"player_id": pid, "line": line, "pick": pick, "p_base": p_base}

    def test_win_loss_push_and_void(self):
        self.assertTrue(daily.grade_row(self.row(), self.BOX)["won"])
        self.assertFalse(daily.grade_row(self.row(pick="UNDER"), self.BOX)["won"])
        push = daily.grade_row(self.row(line=25.0), self.BOX)
        self.assertEqual(push["outcome"], "push")
        self.assertIsNone(push["won"])
        self.assertEqual(daily.grade_row(self.row(pid="p2"), self.BOX)["status"], "void")
        self.assertEqual(daily.grade_row(self.row(pid="nobody"), self.BOX)["status"], "void")
        self.assertIsNone(daily.grade_row(self.row(), None))

    def test_base_model_graded_separately(self):
        res = daily.grade_row(self.row(pick="UNDER", p_base=0.6), self.BOX)
        self.assertFalse(res["won"])
        self.assertTrue(res["base_won"])

    def test_units_at_minus_110(self):
        rows = [{"result": {"won": True}}] * 11 + [{"result": {"won": False}}] * 10
        t = daily.tally(rows)
        self.assertEqual((t["w"], t["l"]), (11, 10))
        self.assertAlmostEqual(t["units"], 0.0, places=2)

    def test_units_use_the_picked_sides_price(self):
        win = {"pick": "UNDER", "prices": {"over": -150, "under": 130}, "result": {"won": True}}
        self.assertAlmostEqual(daily.tally([win])["units"], 1.3)
        win["pick"], win["prices"] = "OVER", {"over": -150, "under": 130}
        self.assertAlmostEqual(daily.tally([win])["units"], 100 / 150, places=2)
        old = {"pick": "OVER", "prices": None, "result": {"won": True}}
        self.assertAlmostEqual(daily.tally([old])["units"], 0.91, places=2)


if __name__ == "__main__":
    unittest.main()
