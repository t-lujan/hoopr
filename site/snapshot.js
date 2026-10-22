// Data source that answers predict()'s calls from a snapshot built by
// tools/build_snapshot.py, for hosts that block requests to ESPN.
import { ESPNError, posBucket } from "./hoopr.js";

const NICKNAMES = {
  wemby: "victor wembanyama", sga: "shai gilgeous-alexander", kd: "kevin durant", joker: "nikola jokic",
  steph: "stephen curry", chef: "stephen curry", giannis: "giannis antetokounmpo", greek: "giannis antetokounmpo",
  ant: "anthony edwards", ad: "anthony davis", dame: "damian lillard", pg: "paul george", kat: "karl-anthony towns",
  jjj: "jaren jackson jr.", spida: "donovan mitchell", book: "devin booker", cp3: "chris paul", lbj: "lebron james",
  bron: "lebron james", "the king": "lebron james", jb: "jaylen brown", jt: "jayson tatum", jimmy: "jimmy butler iii",
  "jimmy butler": "jimmy butler iii", zion: "zion williamson", ja: "ja morant", trae: "trae young", embiid: "joel embiid",
  luka: "luka doncic", "cade": "cade cunningham", "paolo": "paolo banchero", "chet": "chet holmgren",
};

const norm = s => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();

function editDistance(a, b) {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
  }
  return d[a.length][b.length];
}

function matchScore(q, name) {
  if (q === name) return 100;
  const qt = q.split(" "), nt = name.split(" ");
  if (qt.every(t => nt.some(n => n.startsWith(t)))) return 80;
  if (name.replace(/ /g, "").includes(q.replace(/ /g, ""))) return 60;
  // typo tolerance: every query word is within a small edit distance of some name word
  const close = qt.every(t => nt.some(n => editDistance(t, n) <= (t.length > 6 ? 2 : 1)));
  return close && q.length >= 4 ? 40 : 0;
}

export function snapshotSource(snap) {
  const memo = new Map();
  const once = (key, fn) => { if (!memo.has(key)) memo.set(key, fn()); return memo.get(key); };
  const teamAbbr = id => (snap.teams[id] || [])[0];
  const teamName = id => (snap.teams[id] || [])[1];
  const latestMinutes = id => {
    for (const s of [snap.season, snap.season - 1]) {
      const p = (snap.league[s] || {})[id];
      if (p && p[0] >= 5) return p[1];
    }
    return 0;
  };
  const index = Object.entries(snap.athletes).map(([id, a]) => ({ id, name: a[0], key: norm(a[0]) }));

  return {
    async searchPlayer(query) {
      let q = norm(query);
      q = NICKNAMES[q] ? norm(NICKNAMES[q]) : q;
      const hits = index.map(p => ({ ...p, score: matchScore(q, p.key) })).filter(p => p.score > 0)
        .sort((a, b) => b.score - a.score || latestMinutes(b.id) - latestMinutes(a.id));
      if (!hits.length) throw new ESPNError(`No current NBA player found matching '${query}'. Check the spelling, or try a last name.`);
      return { id: hits[0].id, name: hits[0].name, others: hits.slice(1, 4).map(h => h.name) };
    },
    async athlete(id) {
      const a = snap.athletes[id];
      return { id, name: a[0], team_id: a[1], team_abbr: teamAbbr(a[1]), position: a[2], headshot: a[3] };
    },
    async gamelog(id, season) {
      return once(`log|${id}|${season}`, () => ((snap.logs[id] || {})[season] || []).map(r => ({
        event_id: r[0], date: new Date(r[1]), season, playoff: !!r[2], home: !!r[3], team_id: r[4], opp_id: r[5],
        opp_abbr: teamAbbr(r[5]), min: r[6], pts: r[7], fgm: r[8], fga: r[9], fta: r[10],
      })));
    },
    async leaguePlayers(season) {
      return once(`league|${season}`, () => Object.fromEntries(Object.entries(snap.league[season] || {}).map(([id, p]) =>
        [id, { gp: p[0], min: p[1], pts: p[2], stl: p[3], blk: p[4], pf: p[5] }])));
    },
    async teamSchedule(teamId, season) {
      return once(`sched|${teamId}|${season}`, () => (((snap.schedules[season] || {})[teamId]) || []).map(r => ({
        event_id: r[0], date: new Date(r[1]), home: !!r[2], opp_id: r[3], opp_abbr: teamAbbr(r[3]), opp_name: teamName(r[3]), final: !!r[4],
      })));
    },
    async teamStats(teamId, season) {
      const t = (snap.teamStats[season] || {})[teamId];
      if (!t) throw new ESPNError("Team stats aren't in this snapshot.");
      return { possessions: t[0], avgSteals: t[1], avgBlocks: t[2], avgDefensiveRebounds: t[3] };
    },
    async standings(season) {
      return once(`stand|${season}`, () => Object.fromEntries(Object.entries(snap.standings[season] || {}).map(([t, s]) =>
        [t, { gp: s[0], ppg: s[1], opp_ppg: s[2] }])));
    },
    async roster(teamId) {
      return (snap.rosters[teamId] || []).map(id => ({ id, name: snap.athletes[id][0], pos: posBucket(snap.athletes[id][2]) }));
    },
    async injuries() {
      return once("inj", () => Object.fromEntries(Object.entries(snap.injuries).map(([t, rows]) =>
        [t, rows.map(r => ({ id: r[0], name: r[1], status: r[2], comment: r[3] }))])));
    },
    async boxScore(eventId) {
      return Object.fromEntries(Object.entries(snap.boxes[eventId] || {}).map(([t, ps]) =>
        [t, { players: ps.map(p => ({ id: p[0], pos: p[1], min: p[2], pts: p[3] })) }]));
    },
    async gameOdds(eventId) {
      const o = snap.odds[eventId];
      return o ? { provider: o[0], total: o[1], spread: o[2], details: o[3] } : null;
    },
    async playerPointsLine(eventId, athleteId) {
      const l = snap.lines[`${eventId}|${athleteId}`];
      return l ? { line: l[0], prices: [], provider: "DraftKings (via ESPN)", updated: l[1] } : null;
    },
  };
}

// The "lines posted" board: every player with a line, grouped by game, soonest first.
export function lineBoard(snap) {
  const now = Date.now();
  const games = new Map();
  for (const [key, [line]] of Object.entries(snap.lines)) {
    const [eid, pid] = key.split("|");
    const a = snap.athletes[pid];
    if (!a) continue;
    const row = ((snap.schedules[snap.season] || {})[a[1]] || []).find(r => r[0] === eid);
    if (!row || new Date(row[1]) < now - 4 * 3600000) continue;
    if (!games.has(eid)) {
      const [home, away] = row[2] ? [a[1], row[3]] : [row[3], a[1]];
      games.set(eid, { date: new Date(row[1]), label: `${snap.teams[away][0]} @ ${snap.teams[home][0]}`, players: [] });
    }
    games.get(eid).players.push({ name: a[0], line });
  }
  return [...games.values()].sort((x, y) => x.date - y.date)
    .map(g => ({ ...g, players: g.players.sort((x, y) => y.line - x.line) }));
}
