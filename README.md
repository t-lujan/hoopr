# hoopr

Type an NBA player's name (and optionally a points line), hit Enter, and the bot
predicts **OVER** or **UNDER** with a probability and a full breakdown of why.

Leave the line out and hoopr pulls the current DraftKings player-points line
from ESPN's odds feed.

No API keys and no dependencies: just Python 3.9+.

## Use it

Terminal:

```
$ python3 -m hoopr
player [line]> jalen brunson 27.5
player [line]> curry              # line pulled automatically
```

or a single shot: `python3 -m hoopr "tyrese maxey 21.5"`

Web page:

```
$ python3 -m hoopr --web          # http://localhost:8000
```

`site/` is a
JavaScript port of the same engine that runs entirely client-side (ESPN's API allows
cross-origin requests). Vercel deploys it on every push: `vercel.json` points the
output at `site/` with no build step.

It can also be served from GitHub Pages via the `gh-pages` branch. To update that
after changing `site/`:

```
git push -f origin "$(git subtree split --prefix site)":refs/heads/gh-pages
```

### Snapshot preview (no network)

Some hosts, like claude.ai artifacts, block requests to outside sites. For those,
bundle the engine with a data snapshot into one self-contained page:

```
python3 tools/build_snapshot.py build/snapshot.json     # ~2 min, every rostered player
python3 tools/build_preview.py build/snapshot.json build/preview.html
```

The preview runs the same engine on data frozen at build time, so rebuild it to
pick up new lines and injuries.

## What the bot looks at

| Factor | What it uses |
|---|---|
| Baseline | Weighted average of the player's last 5 / 10 / 30 games |
| Recent form | Minutes trend, shot volume (FGA/FTA), shooting efficiency vs normal (hot/cold streaks) |
| Home / away | The player's own home/road split, shrunk by sample size |
| Rest | Back-to-backs and extra rest days |
| Opponent defense | Points allowed per possession vs league average |
| Pace | Both teams' possessions per game |
| Vegas | Game total and spread → the team's implied point total |
| Defense vs position | How opposing guards/forwards/centers scored vs their usual output in the opponent's last 12 games, using box scores |
| Opposing players | Likely primary defenders (minutes, steals, blocks, fouls), opponent team steals/blocks/rebounding, opponents ruled out |
| Head-to-head | The player's scoring vs this opponent since last season |
| Teammates out | Usage bump when a regular scorer is out (skipped if that teammate already missed recent games, since the averages already reflect it) |
| Blowout risk | Big spreads mean starters may sit in the 4th quarter |
| Injury status | The player's own status (OUT = no play) |
| Market prior | The projection is pulled 35% toward the line, because sportsbook lines carry news box scores can't see yet |
| Hit rate | How often the player has cleared this exact line in the last 5 / 10 / 20 games and the season |

Each adjustment is a multiplier shrunk toward 1.0, so a small sample can't swing
the call much.

Then the bot **simulates the game 100 times**. Each simulated game starts from one
of the player's real recent games (so minutes and scoring stay linked), shifts it
to tonight's expected minutes, draws a final score margin around the Vegas spread
(blowouts trim a starter's minutes), allows a 3% chance of an early exit, and adds
a little extra noise. The 100 results are centered on the projection, and widened
when the role is new or the line strongly disagrees with the stats. The share of
simulated games that clear the line is blended 75/25 with the player's recent hit
rate against the line. Simulations are seeded by player, game and line, so the same
question always gets the same answer, and the Python and browser engines produce
identical simulated games.

## How it learns

A GitHub Actions workflow (`.github/workflows/learn.yml`) runs the loop in
`tools/daily.py` every day:

1. **Record** (11:40 and 17:40 ET): predict every posted DraftKings points line for
   upcoming games and save the picks to `data/picks/<date>.json`. The later run
   replaces picks for games that haven't started, so the saved pick uses the last
   line before tip-off.
2. **Grade** (06:10 ET): fill in each player's actual points from the final box
   score. Players who don't play are voided.
3. **Learn**: fit a correction layer (`hoopr/learn.py`) on all graded picks. It
   starts from the base model's probability and learns how much to trust each
   signal (each factor, the stats-only edge, the hit rate, new-team and
   early-season flags). L2 regularization keeps it near the base model until the
   data says otherwise. New weights go live (`site/params.json`) only if they beat
   the base model on the newest 20% of picks they weren't trained on, and never
   before 500 graded picks (about three full nights).

The site's track record (`site/record.json`) shows wins, losses and units at
−110 overall, by confidence tier and by side, plus whether its stated
probabilities match how often it actually wins.

Run any step by hand: `python3 tools/daily.py record|grade|learn`.

Confidence: **Coin flip** (<53%), **Lean** (53–57%), **Moderate** (57–62%), **Strong** (62%+).
At standard -110 odds you need 52.4% just to break even.

## Data

All data comes from ESPN's public JSON endpoints (game logs, schedules, box scores,
standings, team stats, injuries, odds, player props). Responses are cached in
`~/.cache/hoopr` (set `HOOPR_CACHE_DIR` to change) for 10 minutes for lines and
injuries, 12 hours for season aggregates, and permanently for finished box scores.

Early in a season the bot uses last season's games and team numbers and says so in
its notes. Last season's playoff games are shown but kept out of the baseline.

## Tests

```
python3 -m unittest discover -s tests -t .
```

## Disclaimer

This is a statistical model, not a guarantee. It hasn't been backtested against
historical lines. Bet responsibly.
