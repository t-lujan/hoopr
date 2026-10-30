// Page behavior shared by the live site (index.html) and the snapshot preview.
import { predict, parseQuery, ESPNError } from "./hoopr.js";

const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// source: the data source predict() reads from; board: optional [{label, players: [{name, line}]}]
export function mount({ source, board = [], example = null, params = {}, record = null } = {}) {
  if (record) renderRecord(record);
  if (board.length) {
    $("#board").hidden = false;
    $("#board-games").innerHTML = board.map(g =>
      `<div class="matchup"><em>${esc(g.label)}</em>${g.players.slice(0, 6).map(p =>
        `<button type="button" class="chip" data-name="${esc(p.name)}">${esc(p.name)} <b>${p.line}</b></button>`).join("")}</div>`).join("");
    $("#board-games").addEventListener("click", e => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      $("#name").value = chip.dataset.name;
      $("#line").value = "";
      run(chip.dataset.name, null);
    });
  }

  $("#f").addEventListener("submit", e => {
    e.preventDefault();
    let name = $("#name").value.trim();
    if (!name) return;
    const lineTxt = $("#line").value.trim();
    let line = null;
    if (lineTxt) {
      line = parseFloat(lineTxt);
      if (!Number.isFinite(line)) {
        $("#out").innerHTML = `<div class="card err">"${esc(lineTxt)}" isn't a number. Enter a line like 24.5, or leave it blank.</div>`;
        return;
      }
    } else {
      [name, line] = parseQuery(name);  // allow "brunson 27.5" in the name box
    }
    run(name, line);
  });

  async function run(name, line, isExample = false) {
    $("#go").disabled = true;
    const step = s => { $("#out").innerHTML = `<div class="card meta"><span class="spin"></span>${esc(s)}...</div>`; };
    try {
      render(await predict(name, line, new Date(), step, source, params), isExample);
    } catch (err) {
      console.error(err);
      const msg = err instanceof ESPNError ? err.message : `Something went wrong: ${err.message}`;
      $("#out").innerHTML = `<div class="card err">${esc(msg)}</div>`;
    } finally {
      $("#go").disabled = false;
    }
  }

  if (example) run(example, null, true);
}

function render(d, isExample) {
  const g = d.game, p = d.player;
  const when = new Date(g.date).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const src = d.line_source ? `from ${esc(d.line_source.provider)}` : "entered by you";
  const pickTxt = d.pick === "NO PLAY" ? "No play" : `${d.pick} ${d.line}`;
  const rows = d.factors.map(f => {
    const cls = f.kind === "info" ? "info" : f.impact > 0.05 ? "pos" : f.impact < -0.05 ? "neg" : "";
    const imp = f.kind === "info" ? (f.lean > 0 ? "&#9650; info" : f.lean < 0 ? "&#9660; info" : "info")
      : (f.impact >= 0 ? "+" : "") + f.impact.toFixed(2);
    return `<tr><td class="n">${esc(f.name)}</td><td class="i ${cls}">${imp}</td><td class="d">${esc(f.detail)}</td></tr>`;
  }).join("");
  const games = d.recent_games.map(x =>
    `<span class="${x.pts > d.line ? "o" : x.pts < d.line ? "u" : ""}">${x.pts} ${esc(x.opp)}</span>`).join("");
  const others = p.other_matches.length ? `<div class="meta">Not who you meant? Also matched: ${p.other_matches.map(esc).join(", ")}</div>` : "";
  const overPct = Math.round(d.p_over * 100);
  $("#out").innerHTML = `
    <div class="card">
      ${isExample ? `<div class="example">Example</div>` : ""}
      <div class="head">
        ${p.headshot ? `<img src="${esc(p.headshot)}" alt="" onerror="this.remove()">` : ""}
        <div><div class="who">${esc(p.name)} <span class="meta">${esc(p.team)} &middot; ${esc(p.position)}</span></div>
        <div class="meta">${g.home ? "vs" : "@"} ${esc(g.opponent_name)} &middot; ${esc(when)}</div>
        <div class="meta">Points line <b>${d.line}</b> (${src})</div></div>
      </div>
      ${others}
      <div class="pick ${d.pick}">${esc(pickTxt)}</div>
      ${d.pick === "NO PLAY" ? `<div class="meta">${esc(d.confidence)}</div>` : `
      <div class="odds"><b>${Math.round(d.probability * 100)}%</b> chance &middot; ${esc(d.confidence)} &middot; projected <b>${d.projection.toFixed(1)}</b> pts</div>
      <div class="bar"><div style="width:${(d.p_over * 100).toFixed(1)}%"></div></div>
      <div class="meta">Over ${overPct}% / Under ${100 - overPct}% &middot; simulation ${Math.round(d.p_sim * 100)}% over, recent hit rate ${Math.round(d.p_hit_rate * 100)}% over</div>`}
      ${d.notes.map(n => `<p class="note">&#9888; ${esc(n)}</p>`).join("")}
    </div>
    ${d.pick === "NO PLAY" ? "" : simCard(d.simulation, d.line)}
    <div class="card">
      <table>
        <tr><td class="n">Baseline</td><td class="i">${d.baseline.toFixed(1)}</td><td class="d meta">Weighted average of last 5 / 10 / 30 games</td></tr>
        ${rows}
        <tr class="total"><td class="n">Projection</td><td class="i">${d.projection.toFixed(1)}</td><td></td></tr>
      </table>
    </div>
    <div class="card"><div class="label">Last 10 games</div><div class="meta">Outlined green = over ${d.line}, red = under</div><div class="games">${games}</div></div>`;
}

// Dot plot of the simulated games: one dot per game, stacked by final points.
function simCard(sim, line) {
  const xs = sim.outcomes;
  const lo = Math.min(xs[0], Math.floor(line) - 2), hi = Math.max(xs[xs.length - 1], Math.ceil(line) + 2);
  const counts = new Map();
  for (const v of xs) counts.set(v, (counts.get(v) || 0) + 1);
  const stack = Math.max(...counts.values());
  // size to the card so labels stay at true pixel size and nothing scrolls sideways
  const top = 24, bottom = 26, side = 10;
  const avail = Math.max(260, (document.getElementById("out").clientWidth || 600) - 38);
  const col = Math.max(6, Math.min(16, (avail - side * 2) / (hi - lo + 1)));
  const r = Math.min(5, col * 0.38), row = r * 2 + 2.5;
  const W = side * 2 + (hi - lo + 1) * col, H = top + stack * row + bottom, base = H - bottom;
  const x = v => side + (v - lo) * col + col / 2;
  const cls = v => (v > line ? "d-over" : v < line ? "d-under" : "d-push");
  let cols = "", dots = "", ticks = "";
  for (let v = lo; v <= hi; v++) {
    const c = counts.get(v) || 0;
    const side_ = v > line ? "over" : v < line ? "under" : "push";
    cols += `<rect class="col" x="${x(v) - col / 2}" y="${top}" width="${col}" height="${base - top}"><title>${v} pts: ${c} of ${sim.runs} games (${side_})</title></rect>`;
    for (let k = 0; k < c; k++) dots += `<circle class="${cls(v)}" cx="${x(v)}" cy="${base - row / 2 - k * row}" r="${r}"/>`;
    if (v % (col < 9 ? 10 : 5) === 0) ticks += `<line class="axis" x1="${x(v)}" x2="${x(v)}" y1="${base}" y2="${base + 4}"/><text x="${x(v)}" y="${base + 16}" text-anchor="middle">${v}</text>`;
  }
  const lx = x(line);
  const anchor = lx < 40 ? "start" : lx > W - 40 ? "end" : "middle";
  const svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="${sim.runs} simulated games: ${sim.over} over the line of ${line}, ${sim.under} under. Median ${sim.median} points.">
    ${cols}<g>${dots}</g>
    <line class="axis" x1="${side}" x2="${W - side}" y1="${base}" y2="${base}"/>${ticks}
    <line class="mark" x1="${lx}" x2="${lx}" y1="${top - 6}" y2="${base}"/>
    <text class="mark-label" x="${lx}" y="${top - 10}" text-anchor="${anchor}">Line ${line}</text>
  </svg>`;
  return `<div class="card">
    <div class="sim-head">
      <div class="label">${sim.runs} simulated games</div>
      <div class="keys"><span><i class="under"></i>Under ${sim.under}</span><span><i class="over"></i>Over ${sim.over}</span>${sim.push ? `<span><i class="push"></i>Push ${sim.push}</span>` : ""}</div>
    </div>
    <div class="simchart">${svg}</div>
    <div class="meta">Each dot is one simulated game. Median ${sim.median} pts &middot; middle 80% of games ${sim.p10}&ndash;${sim.p90} pts &middot; ${sim.blowouts} blowouts &middot; ${sim.early_exits} early exits</div>
  </div>`;
}

// ---------------------------------------------------------------- track record

const FEATURE_NAMES = {
  edge: "Projection vs line", stats_edge: "Stats-only projection vs line", hit_rate: "Recent hit rate vs the line",
  role_change: "Player on a new team", early_season: "Early-season sample", listed: "Player on the injury report",
  f_home: "Home/away split", f_rest: "Rest", f_defense: "Opponent defense", f_pace: "Pace", f_vegas: "Vegas team total",
  f_position: "Defense vs position", f_opp_injuries: "Opponent injuries", f_h2h: "History vs opponent",
  f_teammates: "Teammates out", f_blowout: "Blowout risk",
};
const FLAGS = new Set(["role_change", "early_season", "listed"]);
const wl = t => `${t.w}&ndash;${t.l}${t.p ? `&ndash;${t.p}` : ""}`;
const winPct = t => (t.win_pct == null ? "&ndash;" : `${(t.win_pct * 100).toFixed(1)}%`);
const units = t => `${t.units >= 0 ? "+" : ""}${t.units.toFixed(2)}u`;

const shortDay = d => new Date(d + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" });

function renderRecord(rec) {
  const el = document.getElementById("record");
  if (!el) return;
  const old = (rec.previous || {})["1"];
  let view = "new";
  const draw = () => {
    el.hidden = false;
    const tabs = old ? `<div class="era-tabs" role="tablist">
      <button type="button" role="tab" data-era="new" aria-selected="${view === "new"}">New model</button>
      <button type="button" role="tab" data-era="old" aria-selected="${view === "old"}">Old model</button></div>` : "";
    el.innerHTML = tabs + (view === "old" ? eraCard(old, rec, true) : eraCard(rec, rec, false));
    el.querySelectorAll(".era-tabs button").forEach(b => b.addEventListener("click", () => { view = b.dataset.era; draw(); }));
  };
  draw();
}

function picksTable(picks) {
  if (!picks || !picks.length) return "";
  const row = p => `<tr><td>${shortDay(p.day)}</td><td>${esc(p.player)} <span class="meta">${esc(p.team)} v ${esc(p.opp)}</span></td><td class="i">${p.pick === "OVER" ? "O" : "U"} ${p.line}</td><td class="i">${p.pts}</td><td class="i ${p.won === true ? "pos" : p.won === false ? "neg" : ""}">${p.won === true ? "W" : p.won === false ? "L" : "P"}</td></tr>`;
  const head = `<tr><td class="meta">Day</td><td class="meta">Player</td><td class="i meta">Pick</td><td class="i meta">Pts</td><td class="i meta"></td></tr>`;
  const first = picks.slice(0, 25), rest = picks.slice(25);
  return `<div class="label" style="margin-top:16px">Pick history</div>
    <table class="calib picks">${head}${first.map(row).join("")}</table>
    ${rest.length ? `<details><summary>Show all ${picks.length} picks</summary><table class="calib picks">${rest.map(row).join("")}</table></details>` : ""}`;
}

function eraCard(rec, top, isOld) {
  const o = rec.overall;
  const since = rec.since ? shortDay(rec.since) : null;
  const beat = o.win_pct != null && o.win_pct > rec.break_even;
  const tiers = Object.entries(rec.by_confidence).map(([k, t]) =>
    `<div class="tile"><div class="label">${esc(k)}</div><div class="big">${wl(t)}</div><div class="meta">${winPct(t)} &middot; ${units(t)}</div></div>`).join("");
  const calib = rec.calibration.map(c =>
    `<tr><td>${Math.round(c.lo * 100)}&ndash;${Math.round(c.hi * 100)}%</td><td class="i">${(c.predicted * 100).toFixed(0)}%</td><td class="i">${(c.actual * 100).toFixed(0)}%</td><td class="i">${c.n}</td></tr>`).join("");
  const started = top.version_since ? new Date(top.version_since).toLocaleDateString([], { month: "short", day: "numeric" }) : null;
  const title = isOld ? `Old model's track record${since ? `, ${since}&ndash;${shortDay(rec.until)}` : ""}`
    : `Bot's track record${started ? ` since ${started}` : ""}`;
  const note = isOld
    ? `<p class="era-note">The first version of the model, kept for the record. It leaned 35% on the sportsbook line and ran 100 simulations per pick.</p>`
    : `<p class="era-note">New model since ${started || "Oct 29"}: it leans harder on the sportsbook line and runs 2,000 simulations per pick, so the record started over at 0&ndash;0. The old model's record is under <b>Old model</b>.</p>`;
  const L = rec.learning || {};
  let learning;
  if (L.active) {
    const top = Object.entries(L.weights || {}).filter(([, w]) => Math.abs(w) >= 0.02)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 4)
      .map(([k, w]) => `<li>${esc(FEATURE_NAMES[k] || k)}: ${FLAGS.has(k) ? (w > 0 ? "leans over" : "leans under") : (w > 0 ? "trusts it more" : "trusts it less")}</li>`).join("");
    learning = `<p>Learning is <b>on</b>, trained on ${L.trained_on} graded picks. On the newest ${L.holdout} picks it held out, the learned version scored ${L.learned_log_loss} vs ${L.base_log_loss} for the base model (lower is better).</p>${top ? `<ul>${top}</ul>` : ""}`;
  } else {
    learning = `<p>Learning is <b>off</b>. ${esc(L.reason || "")}</p>`;
  }
  return `<div class="card record">
    <h2 class="label">${title}</h2>${note}
    <div class="headline"><span class="big">${wl(o)}</span><span class="${beat ? "pos" : "neg"}">${winPct(o)}</span><span>${units(o)}</span></div>
    <div class="meta">Every posted points line, picked before tip-off and graded from the box score. Break-even at &minus;110 odds is ${(rec.break_even * 100).toFixed(1)}%. Units assume 1 unit per pick at &minus;110.${rec.pending ? ` ${rec.pending} picks waiting on results.` : ""}</div>
    <div class="tiles">${tiers}</div>
    <div class="cols">
      <div><div class="label">When it says&hellip;</div>
        <table class="calib"><tr><td class="meta">Confidence</td><td class="i meta">Predicted</td><td class="i meta">Actual</td><td class="i meta">Picks</td></tr>${calib}</table></div>
      <div><div class="label">By side</div>
        <table class="calib">${Object.entries(rec.by_side).map(([k, t]) => `<tr><td>${k}</td><td class="i">${wl(t)}</td><td class="i">${winPct(t)}</td></tr>`).join("")}</table>
        ${isOld ? "" : `<div class="label" style="margin-top:12px">What it's learning</div>${learning}`}</div>
    </div>${picksTable(rec.picks)}
  </div>`;
}
