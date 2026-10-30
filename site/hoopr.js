// Browser port of hoopr/espn.py + hoopr/predict.py so the bot runs on a static
// host (GitHub Pages). ESPN's public endpoints send Access-Control-Allow-Origin: *,
// so the page calls them directly. Keep the logic in step with the Python engine.

const SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba";
const WEB = "https://site.web.api.espn.com/apis/common/v3";
const CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba";
const TTL = 10 * 60 * 1000;
const BREAK_EVEN = 0.5238; // win rate needed to profit at standard -110 juice

export class ESPNError extends Error {}

// ---------------------------------------------------------------- http

const cache = new Map();

async function fetchRetry(url) {
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      lastErr = e;
      await new Promise(res => setTimeout(res, 500 * 2 ** attempt));
    }
  }
  throw new ESPNError(`Couldn't reach ESPN (${lastErr.message}). Try again in a moment.`);
}

function getJSON(url) {
  const hit = cache.get(url);
  if (hit && Date.now() - hit.t < TTL) return hit.p;
  const p = fetchRetry(url);
  cache.set(url, { t: Date.now(), p });
  p.catch(() => cache.delete(url));
  return p;
}

// ---------------------------------------------------------------- helpers

const avg = xs => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
const sum = xs => xs.reduce((a, b) => a + b, 0);
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const num = s => { const v = parseFloat(s); return Number.isFinite(v) ? v : 0; };
const f1 = x => x.toFixed(1);
const signed = (x, d = 1) => (x >= 0 ? "+" : "") + x.toFixed(d);
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const DAY = 86400000;

function madeAtt(s) {
  const parts = String(s || "").split("-");
  return parts.length === 2 ? [num(parts[0]), num(parts[1])] : [0, 0];
}

function ordinal(n) {
  const s = n % 100 >= 10 && n % 100 <= 20 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" })[n % 10] || "th";
  return n + s;
}

function pstdev(xs) {
  const m = avg(xs);
  return Math.sqrt(avg(xs.map(x => (x - m) ** 2)));
}

export function currentSeason(d = new Date()) {
  // ESPN labels a season by the year it ends (2026-27 -> 2027)
  return d.getUTCMonth() >= 8 ? d.getUTCFullYear() + 1 : d.getUTCFullYear();
}

export function posBucket(abbr) {
  abbr = (abbr || "").toUpperCase();
  if (abbr.endsWith("C")) return "C";
  if (abbr.endsWith("F")) return "F";
  return "G";
}

export function parseQuery(text) {
  // 'lebron james 24.5' -> ['lebron james', 24.5]; 'lebron' -> ['lebron', null]
  text = text.trim();
  const m = text.match(/^(.*?)[\s,]+(?:o\/?u\s*)?(\d+(?:\.\d+)?)$/i);
  if (m && m[1].trim()) return [m[1].trim(), parseFloat(m[2])];
  return [text, null];
}

// ---------------------------------------------------------------- ESPN data

async function searchPlayer(name) {
  const d = await getJSON(`${WEB}/search?query=${encodeURIComponent(name.trim())}&limit=10&type=player&sport=basketball&league=nba`);
  const items = (d.items || []).filter(i => i.league === "nba");
  if (!items.length) throw new ESPNError(`No NBA player found matching '${name}'`);
  return { id: items[0].id, name: items[0].displayName, others: items.slice(1, 4).map(i => i.displayName) };
}

async function athlete(id) {
  const a = (await getJSON(`${WEB}/sports/basketball/nba/athletes/${id}`)).athlete;
  const team = a.team || {};
  return {
    id: a.id, name: a.displayName, team_id: team.id, team_abbr: team.abbreviation,
    position: (a.position || {}).abbreviation || "", headshot: (a.headshot || {}).href,
  };
}

async function gamelog(id, season) {
  const d = await getJSON(`${WEB}/sports/basketball/nba/athletes/${id}/gamelog?season=${season}`);
  const names = d.names || [];
  const meta = d.events || {};
  const games = [];
  for (const st of d.seasonTypes || []) {
    const label = st.displayName || "";
    if (label.includes("Preseason")) continue;
    const playoff = label.includes("Postseason");
    for (const cat of st.categories || []) {
      for (const ev of cat.events || []) {
        const m = meta[ev.eventId];
        if (!m) continue;
        const s = Object.fromEntries(names.map((n, i) => [n, ev.stats[i]]));
        const min = num(s.minutes);
        if (min <= 0) continue;
        const [fgm, fga] = madeAtt(s["fieldGoalsMade-fieldGoalsAttempted"]);
        const [, fta] = madeAtt(s["freeThrowsMade-freeThrowsAttempted"]);
        const oppId = m.opponent.id;
        games.push({
          event_id: ev.eventId, date: new Date(m.gameDate), season, playoff,
          home: m.atVs === "vs",
          team_id: m.homeTeamId === oppId ? m.awayTeamId : m.homeTeamId,
          opp_id: oppId, opp_abbr: m.opponent.abbreviation,
          min, pts: num(s.points), fgm, fga, fta,
        });
      }
    }
  }
  return games.sort((a, b) => b.date - a.date);
}

async function leaguePlayers(season) {
  const d = await getJSON(`${WEB}/sports/basketball/nba/statistics/byathlete?region=us&lang=en&contentorigin=espn` +
    `&isqualified=false&limit=1000&page=1&season=${season}&seasontype=2&sort=offensive.avgPoints:desc`);
  const cats = Object.fromEntries((d.categories || []).map(c => [c.name, c.names]));
  const out = {};
  for (const row of d.athletes || []) {
    const a = row.athlete;
    const s = {};
    for (const c of row.categories) (cats[c.name] || []).forEach((n, i) => { s[n] = num((c.totals || [])[i]); });
    out[a.id] = {
      name: a.displayName, gp: s.gamesPlayed || 0, min: s.avgMinutes || 0, pts: s.avgPoints || 0,
      stl: s.avgSteals || 0, blk: s.avgBlocks || 0, pf: s.avgFouls || 0,
    };
  }
  return out;
}

async function teamSchedule(teamId, season) {
  const d = await getJSON(`${SITE}/teams/${teamId}/schedule?season=${season}`);
  const games = (d.events || []).map(e => {
    const c = e.competitions[0];
    const me = c.competitors.find(x => x.id === String(teamId));
    const opp = c.competitors.find(x => x.id !== String(teamId));
    return {
      event_id: e.id, date: new Date(e.date), home: me.homeAway === "home",
      opp_id: opp.id, opp_abbr: opp.team.abbreviation, opp_name: opp.team.displayName,
      final: !!(((c.status || {}).type || {}).completed),
    };
  });
  return games.sort((a, b) => a.date - b.date);
}

async function teamStats(teamId, season) {
  const d = await getJSON(`${SITE}/teams/${teamId}/statistics?season=${season}`);
  const out = {};
  for (const c of ((d.results || {}).stats || {}).categories || []) {
    for (const s of c.stats || []) out[s.name] = s.value || 0;
  }
  out.possessions = (out.avgFieldGoalsAttempted || 0) - (out.avgOffensiveRebounds || 0) +
    (out.avgTurnovers || 0) + 0.44 * (out.avgFreeThrowsAttempted || 0);
  return out;
}

async function standings(season) {
  const d = await getJSON(`https://site.api.espn.com/apis/v2/sports/basketball/nba/standings?season=${season}`);
  const out = {};
  for (const conf of d.children || []) {
    for (const e of conf.standings.entries) {
      const s = Object.fromEntries(e.stats.map(x => [x.name, x.value]));
      out[e.team.id] = { gp: (s.wins || 0) + (s.losses || 0), ppg: s.avgPointsFor || 0, opp_ppg: s.avgPointsAgainst || 0 };
    }
  }
  return out;
}

async function roster(teamId) {
  const d = await getJSON(`${SITE}/teams/${teamId}/roster`);
  return (d.athletes || []).map(a => ({ id: a.id, name: a.displayName, pos: posBucket((a.position || {}).abbreviation) }));
}

async function injuries() {
  const d = await getJSON(`${SITE}/injuries`);
  const out = {};
  for (const t of d.injuries || []) {
    out[t.id] = (t.injuries || []).map(i => {
      const a = i.athlete || {};
      const href = ((a.links || []).find(l => (l.href || "").includes("/id/")) || {}).href || "";
      const m = href.match(/\/id\/(\d+)/);
      return { id: m ? m[1] : null, name: a.displayName, status: i.status || "", comment: i.shortComment || "" };
    });
  }
  return out;
}

async function boxScore(eventId) {
  const d = await getJSON(`${SITE}/summary?event=${eventId}`);
  const out = {};
  for (const t of (d.boxscore || {}).players || []) {
    const st = t.statistics[0];
    const keys = st.keys || [];
    const players = [];
    for (const a of st.athletes || []) {
      if (a.didNotPlay || !a.stats || !a.stats.length) continue;
      const s = Object.fromEntries(keys.map((k, i) => [k, a.stats[i]]));
      players.push({ id: a.athlete.id, pos: posBucket((a.athlete.position || {}).abbreviation), min: num(s.minutes), pts: num(s.points) });
    }
    out[t.team.id] = { players };
  }
  return out;
}

async function gameOdds(eventId) {
  try {
    const d = await getJSON(`${CORE}/events/${eventId}/competitions/${eventId}/odds`);
    const it = (d.items || []).find(x => x.overUnder != null);
    return it ? { provider: (it.provider || {}).name, total: it.overUnder, spread: it.spread, details: it.details } : null;
  } catch (e) {
    return null;
  }
}

async function playerPointsLine(eventId, athleteId) {
  const base = `${CORE}/events/${eventId}/competitions/${eventId}/odds/100/propBets?lang=en&region=us&limit=1000`;
  let line = null, updated = null;
  const prices = [];
  for (let page = 1; ; page++) {
    let d;
    try { d = await getJSON(`${base}&page=${page}`); } catch (e) { return null; }
    for (const it of d.items || []) {
      if ((it.type || {}).name !== "Total Points") continue;
      const ref = (it.athlete || {}).$ref || "";
      if (!ref.includes(`/athletes/${athleteId}?`) && !ref.endsWith(`/athletes/${athleteId}`)) continue;
      line = ((it.current || {}).target || {}).value;
      prices.push((((it.odds || {}).american) || {}).value);
      updated = it.lastUpdated;
    }
    if (page >= (d.pageCount || 1)) break;
  }
  return line == null ? null : { line: Number(line), prices, provider: "DraftKings (via ESPN)", updated };
}

// Live ESPN data source. predict() takes any object with these same methods,
// which is how the snapshot build (site/snapshot.js) runs without network access.
export const live = {
  searchPlayer, athlete, gamelog, leaguePlayers, teamSchedule, teamStats, standings,
  roster, injuries, boxScore, gameOdds, playerPointsLine,
};

// ---------------------------------------------------------------- model

const SIMS = 2000;        // simulated games per prediction
const MARGIN_SD = 12.5;   // spread of NBA final margins around the Vegas line
const EARLY_EXIT = 0.03;  // chance a simulated game is cut short (injury, foul-out, ejection)

export function seedFor(...parts) {
  // 32-bit FNV-1a hash, so the same question always replays the same simulated games
  let h = 0x811c9dc5;
  for (const b of new TextEncoder().encode(parts.join("|"))) h = Math.imul(h ^ b, 0x01000193) >>> 0;
  return h;
}

export function mulberry32(a) {
  // Small seeded RNG; bit-for-bit the same as the Python engine's
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

function normal(rnd) {
  const u1 = 1 - rnd(), u2 = rnd();
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}

export function simulate(projection, line, history, expMin, marginMean, starter, widen, rnd, runs = SIMS) {
  // Play the game out `runs` times and count how often the player clears the line.
  // Each run starts from one of the player's real recent games ([minutes, points]),
  // so minutes and scoring stay linked; minutes shift to tonight's expectation, a
  // simulated final margin trims a starter's minutes in a blowout, and a little
  // noise covers what history hasn't shown. Results are scaled so their average
  // equals `projection`; `widen` > 1 spreads them out when the role is uncertain.
  // Games cut short are rare events a 25-game window can over- or under-sample,
  // so they're replaced by a fixed early-exit rate.
  const usual = [...history.map(h => h[0])].sort((a, b) => a - b)[Math.floor(history.length / 2)];
  let full = history.filter(h => h[0] >= usual / 2);
  if (!full.length) full = history;
  const histMin = sum(full.map(h => h[0])) / full.length;
  const raw = [], cutShort = [];
  let blowouts = 0;
  for (let i = 0; i < runs; i++) {
    const [gMin, gPts] = full[Math.floor(rnd() * full.length)];
    const margin = marginMean + MARGIN_SD * normal(rnd);
    let minutes = Math.min(48, Math.max(0, gMin + expMin - histMin));
    if (starter && Math.abs(margin) > 15) {
      minutes *= Math.max(0.8, 1 - 0.012 * (Math.abs(margin) - 15));
      blowouts++;
    }
    const exited = rnd() < EARLY_EXIT;
    if (exited) minutes *= 0.15 + 0.35 * rnd();
    cutShort.push(exited);
    raw.push(gPts / gMin * minutes * Math.max(0, 1 + 0.1 * normal(rnd)));
  }
  const meanRaw = sum(raw) / runs;
  const scale = meanRaw > 0 ? projection / meanRaw : 0;
  // widen normal games only: an early exit is already as far from the center as it gets
  const pts = raw.map((x, i) => Math.floor(Math.max(0, cutShort[i] ? x * scale : projection + widen * (x * scale - projection)) + 0.5))
    .sort((a, b) => a - b);
  const over = pts.filter(p => p > line).length, under = pts.filter(p => p < line).length;
  return {
    runs, over, under, push: runs - over - under,
    p_over: over + under ? over / (over + under) : 0.5,
    p10: pts[Math.floor(runs * 0.1)], median: pts[Math.floor(runs * 0.5)], p90: pts[Math.floor(runs * 0.9)],
    sd: pstdev(pts), blowouts, early_exits: cutShort.filter(Boolean).length, outcomes: pts,
  };
}

// ---------------------------------------------------------------- learning layer
// Mirrors hoopr/learn.py: logit(p_final) = logit(p_base) + bias + sum(weight * feature).

const FACTOR_KEYS = ["home", "rest", "defense", "pace", "vegas", "position", "opp_injuries", "h2h", "teammates", "blowout"];
// ESPN's "Total Points" items carry no side label; the documented order is over first, under second.
const american = v => { const a = Math.trunc(Number(v)); return v != null && v !== "" && Math.abs(a) >= 100 ? a : null; };
const implied = a => (a > 0 ? 100 / (a + 100) : -a / (-a + 100));
export function pricePair(prices) {
  const [o, u] = Array.isArray(prices) ? prices : [(prices || {}).over, (prices || {}).under];
  return { over: american(o), under: american(u) };
}
// No-vig implied P(over) from the two prices; 0.5 (-110/-110) when either is missing.
export function marketPOver(prices) {
  const { over, under } = pricePair(prices);
  if (over == null || under == null) return 0.5;
  const io = implied(over), iu = implied(under);
  return io / (io + iu);
}
export function breakEvenFor(prices, pick) {
  const a = pick === "OVER" || pick === "UNDER" ? pricePair(prices)[pick.toLowerCase()] : null;
  return a == null ? BREAK_EVEN : implied(a);
}
const logit = p => { p = Math.min(Math.max(p, 1e-4), 1 - 1e-4); return Math.log(p / (1 - p)); };
const sigmoid = z => (z >= 0 ? 1 / (1 + Math.exp(-z)) : Math.exp(z) / (1 + Math.exp(z)));

export function features(projection, statsProjection, line, sd, pHit, rows, roleChange, earlySeason, listed) {
  sd = Math.max(sd, 1);
  const x = {
    edge: (projection - line) / sd, stats_edge: (statsProjection - line) / sd, hit_rate: logit(pHit),
    role_change: roleChange ? 1 : 0, early_season: earlySeason ? 1 : 0, listed: listed ? 1 : 0,
  };
  for (const k of FACTOR_KEYS) x[`f_${k}`] = 0;
  for (const r of rows) if (FACTOR_KEYS.includes(r.key)) x[`f_${r.key}`] += Math.round(r.impact * 100) / 100 / sd;
  for (const k of Object.keys(x)) x[k] = Math.round(x[k] * 1e5) / 1e5;
  return x;
}

export function applyParams(params, pBase, x) {
  const w = (params && params.weights) || {};
  if (!params || !params.active) return pBase;
  return sigmoid(logit(pBase) + (params.bias || 0) + Object.entries(w).reduce((t, [k, wk]) => t + (wk || 0) * (x[k] || 0), 0));
}

const hitRate = (games, line) => [games.filter(g => g.pts > line).length, games.filter(g => g.pts < line).length];

export function confidenceLabel(p) {
  const edge = Math.abs(p - 0.5);
  if (edge < 0.03) return "Coin flip";
  if (edge < 0.07) return "Lean";
  if (edge < 0.12) return "Moderate";
  return "Strong";
}

function splitFactor(subset, overallAvg, priorGames, cap) {
  // Ratio of a split's average to the overall average, shrunk by sample size
  if (!subset.length || overallAvg <= 0) return 1;
  const raw = avg(subset.map(g => g.pts)) / overallAvg;
  const n = subset.length;
  return clamp(1 + (raw - 1) * n / (n + priorGames), 1 - cap, 1 + cap);
}

class Factors {
  constructor(base) { this.value = base; this.rows = []; }
  add(key, name, mult, detail) {
    const before = this.value;
    this.value *= mult;
    this.rows.push({ key, name, mult, impact: this.value - before, detail, kind: "adjust" });
  }
  info(name, detail, lean = 0) {
    // A stat shown to the user that is already priced into another factor
    this.rows.push({ name, mult: 1, impact: 0, detail, kind: "info", lean });
  }
}

function latestPlayerStats(pid, cur, prev) {
  const p = cur[pid];
  if (p && p.gp >= 5) return p;
  return prev[pid] || p;
}

async function recentFinals(src, teamId, season, n) {
  let finals = (await src.teamSchedule(teamId, season)).filter(g => g.final);
  if (finals.length < n) finals = (await src.teamSchedule(teamId, season - 1)).filter(g => g.final).concat(finals);
  return finals.slice(-n);
}

export async function predict(playerQuery, line = null, now = new Date(), onStep = () => {}, src = live, params = {}) {
  const season = currentSeason(now);
  const prev = season - 1;
  const notes = [];

  onStep("Finding player");
  const found = await src.searchPlayer(playerQuery);
  const ath = await src.athlete(found.id);
  if (!ath.team_id) throw new ESPNError(`${ath.name} is not on an NBA roster right now.`);
  const pid = ath.id, teamId = ath.team_id;
  const pos = posBucket(ath.position);

  onStep("Pulling game logs, schedule, injuries and league stats");
  let [curGames, prevGames, schedule, inj, curPlayers, prevPlayers, curStand, prevStand] = await Promise.all([
    src.gamelog(pid, season), src.gamelog(pid, prev), src.teamSchedule(teamId, season), src.injuries(),
    src.leaguePlayers(season), src.leaguePlayers(prev), src.standings(season), src.standings(prev),
  ]);
  // Last season's playoff games are months old and against a playoff-only slate of
  // opponents, so they would skew the baseline; they're shown as info instead.
  const oldPlayoffs = prevGames.filter(g => g.playoff);
  prevGames = prevGames.filter(g => !g.playoff);
  let games = curGames.concat(prevGames);
  if (games.length < 10) games = games.concat((await src.gamelog(pid, prev - 1)).filter(g => !g.playoff));
  if (games.length < 3) throw new ESPNError(`Not enough NBA games on record for ${ath.name} to make a call.`);
  if (!curGames.length) notes.push("No games played yet this season, so the numbers come from last season.");
  else if (curGames.length < 5) notes.push(`Only ${curGames.length} game(s) played this season, so last season's games carry most of the weight.`);

  // ------------------------------------------------------------ next game
  const upcoming = schedule.filter(g => !g.final && g.date > new Date(now - 4 * 3600000));
  if (!upcoming.length) throw new ESPNError(`No upcoming game found for ${ath.team_abbr}.`);
  const game = upcoming[0];
  const oppId = game.opp_id;
  const lastFinal = schedule.filter(g => g.final && g.date < game.date);
  const restDays = lastFinal.length ? Math.floor((game.date - lastFinal[lastFinal.length - 1].date) / DAY) : null;
  const roleChange = games[0].team_id !== teamId;
  if (roleChange) notes.push(`${ath.name} has changed teams since the last game played; the role on ${ath.team_abbr} may differ.`);

  // ------------------------------------------------------------ the line
  let lineInfo = null;
  if (line == null) {
    onStep("Pulling the sportsbook line");
    lineInfo = await src.playerPointsLine(game.event_id, pid);
    if (!lineInfo) {
      throw new ESPNError(`Couldn't find a posted points line for ${ath.name} vs ${game.opp_abbr}. ` +
        `Enter the line yourself in the line box.`);
    }
    line = lineInfo.line;
  }
  line = Number(line);

  const status = (inj[teamId] || []).find(i => i.id === pid) || null;

  // ------------------------------------------------------------ baseline
  const pts = games.map(g => g.pts);
  const mins = games.map(g => g.min);
  const l5 = avg(pts.slice(0, 5)), l10 = avg(pts.slice(0, 10)), l30 = avg(pts.slice(0, 30));
  const base = 0.25 * l5 + 0.35 * l10 + 0.40 * l30;
  const f = new Factors(base);

  f.info("Recent scoring", `Last 5: ${f1(l5)} | Last 10: ${f1(l10)} | Last 30: ${f1(l30)} ppg (baseline ${f1(base)})`, l5 > line ? 1 : -1);
  if (oldPlayoffs.length) {
    f.info("Last postseason (not in baseline)",
      `${f1(avg(oldPlayoffs.map(g => g.pts)))} ppg in ${oldPlayoffs.length} playoff games, ${f1(avg(oldPlayoffs.map(g => g.min)))} min`);
  }
  const min5 = avg(mins.slice(0, 5)), min30 = avg(mins.slice(0, 30));
  f.info("Minutes trend", `${f1(min5)} min over last 5 vs ${f1(min30)} over last 30`,
    min5 > min30 + 1.5 ? 1 : min5 < min30 - 1.5 ? -1 : 0);
  const fga5 = avg(games.slice(0, 5).map(g => g.fga)), fga30 = avg(games.slice(0, 30).map(g => g.fga));
  const fta5 = avg(games.slice(0, 5).map(g => g.fta)), fta30 = avg(games.slice(0, 30).map(g => g.fta));
  f.info("Shot volume", `${f1(fga5)} FGA / ${f1(fta5)} FTA last 5 vs ${f1(fga30)} / ${f1(fta30)} last 30`,
    fga5 > fga30 + 1.5 ? 1 : fga5 < fga30 - 1.5 ? -1 : 0);
  const fg5 = sum(games.slice(0, 5).map(g => g.fgm)) / Math.max(1, sum(games.slice(0, 5).map(g => g.fga)));
  const fg30 = sum(games.slice(0, 30).map(g => g.fgm)) / Math.max(1, sum(games.slice(0, 30).map(g => g.fga)));
  f.info("Shooting efficiency", `${pct(fg5, 1)} FG last 5 vs ${pct(fg30, 1)} last 30` +
    (fg5 > fg30 + 0.05 ? " (running hot, regression risk)" : fg5 < fg30 - 0.05 ? " (running cold, bounce-back candidate)" : ""),
    fg5 > fg30 + 0.05 ? -1 : fg5 < fg30 - 0.05 ? 1 : 0);

  // ------------------------------------------------------------ home / away
  const splitGames = games.slice(0, 82);
  const overall = avg(splitGames.map(g => g.pts));
  const side = splitGames.filter(g => g.home === game.home);
  f.add("home", "Home/away", splitFactor(side, overall, 25, 0.05),
    `Averages ${f1(avg(side.map(g => g.pts)))} ${game.home ? "home" : "on the road"} (${side.length} games) vs ${f1(overall)} overall`);

  // ------------------------------------------------------------ rest
  if (restDays == null) f.add("rest", "Rest", 1, "Season opener, fully rested (no adjustment)");
  else if (restDays <= 1) f.add("rest", "Rest", 0.97, "Second night of a back-to-back");
  else if (restDays >= 3) f.add("rest", "Rest", 1.01, `${restDays} days of rest`);
  else f.add("rest", "Rest", 1, "Normal rest (1 day off)");

  // ------------------------------------------------------------ team environment
  onStep("Crunching pace, defense and Vegas numbers");
  const tseason = ((curStand[teamId] || {}).gp || 0) >= 10 ? season : prev;
  const stand = tseason === season ? curStand : prevStand;
  if (tseason === prev) notes.push("Team pace and defense numbers come from last season until teams have 10 games played.");
  const teamIds = Object.keys(stand);
  const tsList = await Promise.all(teamIds.map(t => src.teamStats(t, tseason)));
  const tstats = Object.fromEntries(teamIds.map((t, i) => [t, tsList[i]]));
  const lgPace = avg(tsList.map(s => s.possessions));
  const lgPpg = avg(Object.values(stand).map(s => s.ppg));
  const teamPace = tstats[teamId].possessions, oppPace = tstats[oppId].possessions;
  const teamPpg = stand[teamId].ppg;
  const oppAllowed = stand[oppId].opp_ppg;
  const defEff = (oppAllowed / oppPace) / (lgPpg / lgPace);
  const paceRatio = (teamPace + oppPace - lgPace) / teamPace;
  const rank = (ids, key) => [...ids].sort((a, b) => key(a) - key(b)).indexOf(oppId) + 1;
  const defRank = rank(teamIds, t => stand[t].opp_ppg / tstats[t].possessions);
  const paceRank = rank(teamIds, t => -tstats[t].possessions);

  const odds = await src.gameOdds(game.event_id);
  const elasticity = 0.85; // a player's points move a bit less than 1:1 with team points
  const statW = odds ? 0.35 : 1.0;
  f.add("defense", "Opponent defense", 1 + (defEff - 1) * statW * elasticity,
    `${game.opp_abbr} allow ${f1(oppAllowed)} ppg, ${ordinal(defRank)} best per possession (${signed(defEff * 100 - 100)}% vs league)`);
  f.add("pace", "Pace", 1 + (paceRatio - 1) * statW * elasticity,
    `${game.opp_abbr} play at ${f1(oppPace)} possessions/game (${ordinal(paceRank)} fastest); ` +
    `projected game pace ${signed(paceRatio * 100 - 100)}% vs ${ath.team_abbr}'s norm`);
  let spread = null;
  if (odds) {
    spread = odds.spread || 0;
    const implied = game.home ? odds.total / 2 - spread / 2 : odds.total / 2 + spread / 2;
    const vegas = implied / teamPpg;
    f.add("vegas", "Vegas implied team total", 1 + (vegas - 1) * 0.65 * elasticity,
      `Total ${odds.total}, ${odds.details} → ${ath.team_abbr} implied ${f1(implied)} pts vs ${f1(teamPpg)} avg (${odds.provider})`);
  }

  // ------------------------------------------------------------ positional defense / opposing players
  onStep("Reading the opponent's recent box scores");
  const oppRecent = await recentFinals(src, oppId, season, 12);
  const oppBoxes = await Promise.all(oppRecent.map(g => src.boxScore(g.event_id)));
  const playersBySeason = { [season]: curPlayers, [prev]: prevPlayers };
  const act = { G: 0, F: 0, C: 0 }, exp = { G: 0, F: 0, C: 0 };
  oppRecent.forEach((g, i) => {
    const lp = playersBySeason[currentSeason(g.date)] || prevPlayers;
    for (const [tid, sideBox] of Object.entries(oppBoxes[i])) {
      if (tid === oppId) continue;
      for (const p of sideBox.players) {
        const s = lp[p.id];
        if (p.min < 10 || !s || s.min <= 0) continue;
        act[p.pos] += p.pts;
        exp[p.pos] += s.pts / s.min * p.min;
      }
    }
  });
  const expAll = exp.G + exp.F + exp.C;
  if (exp[pos] >= 50 && expAll > 0) {
    const posRatio = act[pos] / exp[pos];
    const allRatio = (act.G + act.F + act.C) / expAll;
    const rel = posRatio / allRatio;
    const label = { G: "guards", F: "forwards", C: "centers" }[pos];
    f.add("position", `Defense vs ${label}`, clamp(1 + (rel - 1) * 0.5, 0.94, 1.06),
      `Last ${oppRecent.length} games: opposing ${label} scored ${signed(posRatio * 100 - 100, 0)}% vs their usual output ` +
      `(${signed(rel * 100 - 100, 0)}% relative to ${game.opp_abbr}'s overall defense)`);
  }

  const oppInj = Object.fromEntries((inj[oppId] || []).map(i => [i.id, i]));
  const defenders = [];
  for (const r of await src.roster(oppId)) {
    if (r.pos !== pos || (oppInj[r.id] || {}).status === "Out") continue;
    const s = latestPlayerStats(r.id, curPlayers, prevPlayers);
    if (s && s.min >= 15) defenders.push([s.min, r.name, s]);
  }
  defenders.sort((a, b) => b[0] - a[0]);
  if (defenders.length) {
    f.info("Likely primary defenders", defenders.slice(0, 2).map(([, name, s]) =>
      `${name} (${s.min.toFixed(0)} min, ${f1(s.stl)} stl, ${f1(s.blk)} blk, ${f1(s.pf)} fouls)`).join("; "));
  }
  const os = tstats[oppId];
  f.info("Opponent team defense",
    `${f1(os.avgSteals || 0)} steals (${ordinal(rank(teamIds, t => -(tstats[t].avgSteals || 0)))}), ` +
    `${f1(os.avgBlocks || 0)} blocks (${ordinal(rank(teamIds, t => -(tstats[t].avgBlocks || 0)))}), ` +
    `${f1(os.avgDefensiveRebounds || 0)} def. rebounds per game`);

  // opponents who are out, weighted by how much they've actually been playing
  const recentOppIds = oppBoxes.slice(-5).map(b => new Set(((b[oppId] || {}).players || []).map(p => p.id)));
  const missingOpp = [];
  for (const i of Object.values(oppInj)) {
    if (i.status !== "Out") continue;
    const s = latestPlayerStats(i.id, curPlayers, prevPlayers);
    const played = recentOppIds.filter(ids => ids.has(i.id)).length;
    if (s && s.min >= 24 && played >= 3) missingOpp.push(`${i.name} (${s.min.toFixed(0)} min)`);
  }
  if (missingOpp.length) f.add("opp_injuries", "Opponent injuries", 1 + Math.min(0.04, 0.015 * missingOpp.length), "Out: " + missingOpp.join(", "));

  // ------------------------------------------------------------ head to head
  const h2h = games.filter(g => g.opp_id === oppId && g.season >= prev);
  const h2hPool = games.filter(g => g.season >= prev);
  if (h2h.length) {
    f.add("h2h", `History vs ${game.opp_abbr}`, splitFactor(h2h, avg(h2hPool.map(g => g.pts)), 8, 0.06),
      `${f1(avg(h2h.map(g => g.pts)))} ppg in ${h2h.length} game(s) vs ${game.opp_abbr} since last season: ` +
      h2h.slice(0, 6).map(g => g.pts).join(", "));
  }

  // ------------------------------------------------------------ teammates out
  const teamInj = (inj[teamId] || []).filter(i => i.id !== pid && (i.status === "Out" || i.status === "Doubtful"));
  if (teamInj.length) {
    onStep("Checking injured teammates");
    const recentEvents = new Set(games.slice(0, 5).map(g => g.event_id));
    const teammateLogs = await Promise.all(teamInj.map(i => Promise.all([src.gamelog(i.id, season), src.gamelog(i.id, prev)])));
    const share = teamPpg ? avg(pts.slice(0, 30)) / teamPpg : 0;
    let missingPts = 0;
    const missingNames = [], bakedIn = [];
    teamInj.forEach((i, k) => {
      const s = latestPlayerStats(i.id, curPlayers, prevPlayers);
      if (!s || s.pts < 5) return;
      const weight = i.status === "Out" ? 1 : 0.75;
      // did this teammate play in the player's last 5 games?
      const played = teammateLogs[k].flat().filter(g => recentEvents.has(g.event_id)).length;
      if (played >= 3) {
        missingPts += s.pts * weight;
        missingNames.push(`${i.name} (${f1(s.pts)} ppg, ${i.status})`);
      } else {
        bakedIn.push(i.name);
      }
    });
    if (missingPts) {
      const boost = missingPts * share * 0.6;
      f.add("teammates", "Teammates out (usage bump)", Math.min(1.15, 1 + boost / f.value),
        `${missingNames.join(", ")}. ${ath.name}'s ${pct(share)} share of team scoring absorbs some of it`);
    }
    if (bakedIn.length) {
      f.info("Teammates out (already priced in)", `${bakedIn.join(", ")} also missed recent games, so the baseline already reflects it`);
    }
  }

  // ------------------------------------------------------------ blowout risk
  if (spread != null && min30 >= 28 && Math.abs(spread) > 8) {
    f.add("blowout", "Blowout risk", 1 - Math.min(0.06, 0.01 * (Math.abs(spread) - 8)),
      `${f1(Math.abs(spread))}-point spread: starters can sit late in lopsided games`);
  }

  // ------------------------------------------------------------ market prior
  // Sportsbook lines react to news the box scores can't see yet (role changes,
  // minutes limits, lineup news), so the projection is pulled partway toward it.
  const statsProjection = f.value;
  f.add("market", "Sportsbook line (market prior)", (0.20 * statsProjection + 0.80 * line) / statsProjection,
    `Stats alone project ${f1(statsProjection)}; the line is ${line}. Projection is pulled 80% toward the line`);
  const gap = Math.abs(statsProjection - line) / line;

  // ------------------------------------------------------------ probability
  const projection = f.value;
  onStep(`Simulating the game ${SIMS} times`);
  const expMin = 0.25 * min5 + 0.35 * avg(mins.slice(0, 10)) + 0.40 * min30;
  const history = games.slice(0, 25).map(g => [g.min, g.pts]);
  const marginMean = spread == null ? 0 : (game.home ? -spread : spread);
  // less sure of the outcome when the role is new or the book strongly disagrees
  const widen = gap > 0.25 || roleChange ? 1.25 : 1;
  const sim = simulate(projection, line, history, expMin, marginMean, min30 >= 28, widen,
    mulberry32(seedFor(pid, game.event_id, Math.floor(line * 10 + 0.5))));
  const [overN, underN] = hitRate(games.slice(0, 20), line);
  const pEmp = (overN + 5) / (overN + underN + 10);
  const pBase = 0.75 * sim.p_over + 0.25 * pEmp;
  const x = features(projection, statsProjection, line, sim.sd, pEmp, f.rows, roleChange, curGames.length < 5, !!status);
  const pOver = applyParams(params, pBase, x);
  if (params && params.active) {
    f.info("Learned adjustment", `${signed((pOver - pBase) * 100)} points of over-probability from ${params.trained_on} graded picks`,
      pOver > pBase ? 1 : pOver < pBase ? -1 : 0);
  }

  const useCur = curGames.length >= 5;
  const hitRates = {};
  for (const [label, sample] of [["Last 5", games.slice(0, 5)], ["Last 10", games.slice(0, 10)], ["Last 20", games.slice(0, 20)],
    [useCur ? "This season" : "Last season", useCur ? curGames : prevGames]]) {
    const [o, u] = hitRate(sample, line);
    hitRates[label] = { over: o, under: u, games: sample.length };
  }
  f.info("Hit rate vs this line", Object.entries(hitRates).map(([k, v]) => `${k}: ${v.over}/${v.games} over`).join(" | "),
    overN > underN ? 1 : underN > overN ? -1 : 0);

  let pick = pOver >= 0.5 ? "OVER" : "UNDER";
  const pPick = pick === "OVER" ? pOver : 1 - pOver;
  let confidence = confidenceLabel(pOver);
  if (status && status.status === "Out") {
    pick = "NO PLAY";
    confidence = "Player is listed OUT";
  } else if (status) {
    notes.push(`${ath.name} is listed ${status.status}: ${status.comment}`);
    if (confidence === "Strong") confidence = "Moderate";
  }
  if (gap > 0.25 && pick !== "NO PLAY") {
    if (confidence === "Strong") confidence = "Moderate";
    notes.push(`The stats and the line disagree by ${pct(gap)}. If this is a real sportsbook line, a gap that big usually means ` +
      "the book knows something (new role, minutes limit, injury) that past games can't show, so the bot widened its uncertainty.");
  }
  const prices = pricePair(lineInfo && lineInfo.prices);
  const breakEven = breakEvenFor(prices, pick);
  if (pick !== "NO PLAY" && pPick < breakEven) {
    notes.push(`${pct(pPick, 1)} is below the ${pct(breakEven, 1)} needed to profit at the posted odds; treat it as a lean, not a bet.`);
  }

  return {
    player: { id: pid, name: ath.name, team: ath.team_abbr, position: ath.position, headshot: ath.headshot, other_matches: found.others },
    game: { event_id: game.event_id, date: game.date.toISOString(), home: game.home, opponent: game.opp_abbr, opponent_name: game.opp_name, rest_days: restDays },
    line, line_source: lineInfo, prices, p_market: marketPOver(prices), break_even: breakEven, injury_status: status, pick, confidence,
    probability: pPick, p_over: pOver, p_base: pBase, p_sim: sim.p_over, p_hit_rate: pEmp, simulation: sim,
    stats_projection: statsProjection, features: x, learned: !!(params && params.active),
    projection, baseline: base, std_dev: sim.sd,
    factors: f.rows, hit_rates: hitRates,
    recent_games: games.slice(0, 10).map(g => ({ date: g.date.toISOString().slice(0, 10), opp: (g.home ? "vs " : "@ ") + (g.opp_abbr || ""), min: g.min, pts: g.pts })),
    notes,
  };
}
