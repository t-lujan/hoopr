"""Learning layer: correct the hand-built model with what graded picks show.

The base model's probability stays the starting point. On top of it sits a
logistic correction,

    logit(p_final) = logit(p_base) + bias + sum(weight[k] * feature[k])

where the features are the model's own signals in standard-deviation units
(each factor's push, the stats-only edge, the hit rate, role-change flags).
With every weight at zero the correction does nothing, and L2 regularization
pulls weights toward zero, so with little data the bot behaves like the
hand-built model and only drifts as graded results justify it.

New weights only go live when they beat the base model on the most recent
picks they were not trained on. site/params.json carries the live weights to
both engines; the browser engine computes the same features (site/hoopr.js).
"""

import json
import math
import os

PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "params.json")

FACTOR_KEYS = ["home", "rest", "defense", "pace", "vegas", "position", "opp_injuries", "h2h", "teammates", "blowout"]
FEATURES = ["edge", "stats_edge", "hit_rate", "role_change", "early_season", "listed"] + [f"f_{k}" for k in FACTOR_KEYS]

MIN_GRADED = 500     # graded picks needed before any learning is attempted (holdout of 100+)
MIN_GAIN = 0.002     # held-out log-loss improvement required to switch learned weights on
HOLDOUT = 0.2        # newest share of picks used to test new weights
L2 = 10.0            # regularization strength, in picks


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(z):
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def features(projection, stats_projection, line, sd, p_hit, factor_rows, role_change, early_season, listed):
    sd = max(sd, 1.0)
    x = {
        "edge": (projection - line) / sd,
        "stats_edge": (stats_projection - line) / sd,
        "hit_rate": _logit(p_hit),
        "role_change": 1.0 if role_change else 0.0,
        "early_season": 1.0 if early_season else 0.0,
        "listed": 1.0 if listed else 0.0,
    }
    for k in FACTOR_KEYS:
        x[f"f_{k}"] = 0.0
    for row in factor_rows:
        if row.get("key") in FACTOR_KEYS:
            x[f"f_{row['key']}"] += row["impact"] / sd
    return {k: round(v, 5) for k, v in x.items()}


def load_params(path=PARAMS_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def apply(params, p_base, x):
    """Corrected P(over). Returns p_base unchanged when no learned weights are live."""
    w = (params or {}).get("weights") or {}
    if not (params or {}).get("active") or not w:
        return p_base
    z = _logit(p_base) + params.get("bias", 0.0) + sum(w.get(k, 0.0) * x.get(k, 0.0) for k in FEATURES)
    return _sigmoid(z)


# ---------------------------------------------------------------- fitting

def _solve(a, b):
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit(rows, l2=L2, iters=25):
    """L2-regularized logistic regression with the base model's logit as offset.

    rows: [{"p_base": float, "x": {feature: value}, "y": 0 or 1}]
    Returns (bias, {feature: weight}).
    """
    names = ["bias"] + FEATURES
    beta = [0.0] * len(names)
    data = [([1.0] + [r["x"].get(k, 0.0) for k in FEATURES], _logit(r["p_base"]), r["y"]) for r in rows]
    for _ in range(iters):
        grad = [0.0] * len(names)
        hess = [[0.0] * len(names) for _ in names]
        for xs, off, y in data:
            p = _sigmoid(off + sum(b * v for b, v in zip(beta, xs)))
            w = p * (1 - p)
            for i, xi in enumerate(xs):
                grad[i] += (p - y) * xi
                for j in range(i, len(xs)):
                    hess[i][j] += w * xi * xs[j]
        for i in range(len(names)):
            grad[i] += l2 * beta[i]
            hess[i][i] += l2
            for j in range(i):
                hess[i][j] = hess[j][i]
        step = _solve(hess, grad)
        beta = [b - s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return beta[0], dict(zip(FEATURES, beta[1:]))


def log_loss(rows, params):
    total = 0.0
    for r in rows:
        p = apply(params, r["p_base"], r["x"])
        total -= math.log(max(1e-9, p if r["y"] else 1 - p))
    return total / len(rows) if rows else float("nan")


def train(rows):
    """Fit new weights and decide whether they go live. rows must be in time order."""
    n = len(rows)
    base = {"active": False}
    out = {"trained_on": n, "active": False, "bias": 0.0, "weights": {k: 0.0 for k in FEATURES}}
    if n < MIN_GRADED:
        out["reason"] = f"Needs {MIN_GRADED} graded picks before learning; has {n}."
        return out
    cut = int(n * (1 - HOLDOUT))
    train_rows, test_rows = rows[:cut], rows[cut:]
    bias, weights = fit(train_rows)
    trial = {"active": True, "bias": bias, "weights": weights}
    base_ll, new_ll = log_loss(test_rows, base), log_loss(test_rows, trial)
    out.update(holdout=len(test_rows), base_log_loss=round(base_ll, 5), learned_log_loss=round(new_ll, 5))
    if new_ll < base_ll - MIN_GAIN:
        bias, weights = fit(rows)  # it generalizes: refit on everything
        out.update(active=True, bias=round(bias, 5), weights={k: round(v, 5) for k, v in weights.items()},
                   reason=f"Learned weights beat the base model on the newest {len(test_rows)} picks.")
    else:
        out["reason"] = f"Learned weights didn't clearly beat the base model on the newest {len(test_rows)} picks, so they stay off."
    return out
