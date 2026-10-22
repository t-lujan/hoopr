"""Tiny local web UI: python -m hoopr --web  ->  http://localhost:8000"""

import json
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import espn
from .cli import parse_query
from .predict import predict

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hoopr</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --text:#e8eaf0; --dim:#8a90a0; --line:#2a2f3a;
          --over:#4f8fe6; --under:#e0544c; --warn:#f5a524; --accent:#4c8dff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  main { max-width:860px; margin:0 auto; padding:32px 16px 64px; }
  h1 { margin:0 0 4px; font-size:28px; letter-spacing:-.5px; }
  .sub { color:var(--dim); margin:0 0 20px; }
  form { display:flex; gap:8px; flex-wrap:wrap; }
  input { background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:10px;
          padding:12px 14px; font-size:16px; }
  #name { flex:1 1 260px; } #line { flex:0 0 130px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:10px; padding:12px 18px;
           font-size:16px; font-weight:600; cursor:pointer; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; margin-top:18px; }
  .head { display:flex; gap:14px; align-items:center; }
  .head img { width:72px; height:52px; object-fit:cover; border-radius:8px; background:#222; }
  .pick { font-size:34px; font-weight:800; letter-spacing:-.5px; }
  .OVER { color:var(--over); } .UNDER { color:var(--under); } .NO.PLAY { color:var(--warn); }
  .meta { color:var(--dim); font-size:14px; }
  .bar { height:10px; border-radius:6px; background:var(--under); overflow:hidden; margin:10px 0 4px; }
  .bar > div { height:100%; background:var(--over); }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td { padding:7px 6px; border-top:1px solid var(--line); vertical-align:top; }
  td.n { white-space:nowrap; font-weight:600; }
  td.i { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .pos { color:var(--over); } .neg { color:var(--under); } .info { color:var(--dim); }
  .note { color:var(--warn); font-size:14px; margin:6px 0 0; }
  .err { color:var(--under); }
  .games { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
  .games span { padding:4px 8px; border-radius:6px; font-size:13px; background:#222733; }
  .games span.o { outline:1px solid var(--over); } .games span.u { outline:1px solid var(--under); }
  @media (max-width:560px) {
    table, tbody { display:block; }
    tr { display:grid; grid-template-columns:1fr auto; border-top:1px solid var(--line); }
    td { border-top:0; }
    td.d { grid-column:1 / -1; padding-top:0; }
  }
</style>
</head>
<body>
<main>
  <h1>hoopr</h1>
  <p class="sub">NBA points over/under predictor. Type a player, optionally a line, and hit Enter.
     Leave the line blank to pull it from the sportsbook.</p>
  <form id="f">
    <input id="name" placeholder="Player name, e.g. Jalen Brunson" autofocus autocomplete="off">
    <input id="line" placeholder="Line (optional)" inputmode="decimal" autocomplete="off">
    <button>Predict</button>
  </form>
  <div id="out"></div>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
$("#f").addEventListener("submit", async e => {
  e.preventDefault();
  const name = $("#name").value.trim(); if (!name) return;
  const line = $("#line").value.trim();
  $("#out").innerHTML = '<div class="card meta">Pulling stats, injuries, odds and matchup data...</div>';
  try {
    const r = await fetch(`/api/predict?player=${encodeURIComponent(name)}&line=${encodeURIComponent(line)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    render(d);
  } catch (err) {
    $("#out").innerHTML = `<div class="card err">${esc(err.message)}</div>`;
  }
});
function render(d) {
  const g = d.game, p = d.player;
  const when = new Date(g.date).toLocaleString([], {weekday:"short", month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
  const src = d.line_source ? `pulled from ${esc(d.line_source.provider)}` : "entered by you";
  const pickTxt = d.pick === "NO PLAY" ? "NO PLAY" : `${d.pick} ${d.line}`;
  const rows = d.factors.map(f => {
    const cls = f.kind === "info" ? "info" : f.impact > 0.05 ? "pos" : f.impact < -0.05 ? "neg" : "";
    const imp = f.kind === "info" ? (f.lean > 0 ? "&#9650; info" : f.lean < 0 ? "&#9660; info" : "info")
                                  : (f.impact >= 0 ? "+" : "") + f.impact.toFixed(2);
    return `<tr><td class="n">${esc(f.name)}</td><td class="i ${cls}">${imp}</td><td class="d">${esc(f.detail)}</td></tr>`;
  }).join("");
  const games = d.recent_games.map(x => `<span class="${x.pts > d.line ? "o" : x.pts < d.line ? "u" : ""}">${x.pts} ${esc(x.opp)}</span>`).join("");
  $("#out").innerHTML = `
    <div class="card">
      <div class="head">
        ${p.headshot ? `<img src="${esc(p.headshot)}" alt="" onerror="this.remove()">` : ""}
        <div><div style="font-size:20px;font-weight:700">${esc(p.name)} <span class="meta">${esc(p.team)} &middot; ${esc(p.position)}</span></div>
        <div class="meta">${g.home ? "vs" : "@"} ${esc(g.opponent_name)} &middot; ${esc(when)}</div>
        <div class="meta">Points line <b style="color:var(--text)">${d.line}</b> (${src})</div></div>
      </div>
      <div style="margin-top:14px" class="pick ${d.pick}">${esc(pickTxt)}</div>
      ${d.pick === "NO PLAY" ? `<div class="meta">${esc(d.confidence)}</div>` : `
      <div><b>${Math.round(d.probability * 100)}%</b> chance &middot; ${esc(d.confidence)} &middot; projected <b>${d.projection}</b> pts (&plusmn;${d.std_dev})</div>
      <div class="bar"><div style="width:${(d.p_over * 100).toFixed(1)}%"></div></div>
      <div class="meta">Over ${Math.round(d.p_over * 100)}% / Under ${Math.round((1 - d.p_over) * 100)}% &middot; ${d.simulation.runs} simulated games: ${d.simulation.over} over, ${d.simulation.under} under</div>`}
      ${d.notes.map(n => `<p class="note">&#9888; ${esc(n)}</p>`).join("")}
    </div>
    <div class="card">
      <table><tr><td class="n">Baseline</td><td class="i">${d.baseline}</td><td class="d meta">Weighted average of last 5 / 10 / 30 games</td></tr>
      ${rows}
      <tr><td class="n">Projection</td><td class="i"><b>${d.projection}</b></td><td></td></tr></table>
    </div>
    <div class="card"><div class="meta">Last 10 games (outlined green = over ${d.line}, red = under)</div><div class="games">${games}</div></div>`;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path != "/api/predict":
            return self._send(404, json.dumps({"error": "not found"}), "application/json")
        qs = urllib.parse.parse_qs(url.query)
        name = (qs.get("player") or [""])[0]
        line_txt = (qs.get("line") or [""])[0].strip()
        if not line_txt:
            name, line = parse_query(name)  # allow "brunson 27.5" in the name box
        else:
            try:
                line = float(line_txt)
            except ValueError:
                return self._send(400, json.dumps({"error": f"'{line_txt}' is not a number"}), "application/json")
        if not name.strip():
            return self._send(400, json.dumps({"error": "Enter a player name"}), "application/json")
        try:
            result = predict(name, line)
        except espn.ESPNError as e:
            return self._send(422, json.dumps({"error": str(e)}), "application/json")
        except Exception as e:
            traceback.print_exc()
            return self._send(500, json.dumps({"error": f"Something went wrong: {e}"}), "application/json")
        self._send(200, json.dumps(result, default=str), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


def main(argv=None):
    argv = argv or []
    port = int(argv[0]) if argv else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"hoopr running at http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
