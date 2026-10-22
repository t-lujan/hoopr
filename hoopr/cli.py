"""Terminal interface: type a player (and optionally a line), hit Enter."""

import re
import sys
from datetime import datetime

from . import espn
from .predict import predict

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = RED = YELLOW = RESET = ""


def parse_query(text):
    """'lebron james 24.5' -> ('lebron james', 24.5); 'lebron' -> ('lebron', None)."""
    text = text.strip()
    m = re.match(r"^(.*?)[\s,]+(?:o/?u\s*)?(\d+(?:\.\d+)?)$", text, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip(), float(m.group(2))
    return text, None


def render(r):
    p, g = r["player"], r["game"]
    when = datetime.fromisoformat(g["date"]).astimezone().strftime("%a %b %d, %I:%M %p")
    where = "vs" if g["home"] else "@"
    out = [
        "",
        f"{BOLD}{p['name']}{RESET} ({p['team']}, {p['position']})  {where} {g['opponent_name']}  {DIM}{when}{RESET}",
    ]
    src = r["line_source"]
    out.append(f"Points line: {BOLD}{r['line']:g}{RESET}" +
               (f"  {DIM}pulled from {src['provider']}, updated {src['updated']}{RESET}" if src else "  (entered)"))
    out.append("")
    out.append(f"{'Factor':32} {'Impact':>8}   Detail")
    out.append(f"{'-' * 32} {'-' * 8}   {'-' * 40}")
    out.append(f"{'Baseline (weighted recent avg)':32} {r['baseline']:8.1f}")
    for row in r["factors"]:
        if row["kind"] == "info":
            arrow = {1: f"{GREEN}^{RESET}", -1: f"{RED}v{RESET}"}.get(row.get("lean", 0), " ")
            out.append(f"{row['name']:32} {'info':>7}{arrow}   {DIM}{row['detail']}{RESET}")
        else:
            color = GREEN if row["impact"] > 0.05 else RED if row["impact"] < -0.05 else ""
            out.append(f"{row['name']:32} {color}{row['impact']:+8.2f}{RESET}   {row['detail']}")
    out.append(f"{'-' * 32} {'-' * 8}")
    out.append(f"{BOLD}{'Projected points':32} {r['projection']:8.1f}{RESET}   {DIM}(simulated swing +/- {r['std_dev']:.1f}){RESET}")
    out.append("")
    out.append(DIM + "Last 10: " + ", ".join(f"{int(x['pts'])} {x['opp']}" for x in r["recent_games"]) + RESET)
    out.append("")

    pick = r["pick"]
    color = GREEN if pick == "OVER" else RED if pick == "UNDER" else YELLOW
    if pick == "NO PLAY":
        out.append(f"{BOLD}{color}>>> NO PLAY: {r['confidence']}{RESET}")
    else:
        out.append(f"{BOLD}{color}>>> PREDICTION: {pick} {r['line']:g}{RESET}  "
                   f"{BOLD}{r['probability']:.0%}{RESET} chance  ({r['confidence']})")
        sim = r["simulation"]
        out.append(f"{DIM}    {sim['runs']} simulated games: {sim['over']} over, {sim['under']} under"
                   + (f", {sim['push']} push" if sim["push"] else "")
                   + f" | middle 80% of outcomes {sim['p10']}-{sim['p90']} pts | recent hit rate {r['p_hit_rate']:.0%} over{RESET}")
    for n in r["notes"]:
        out.append(f"{YELLOW}  ! {n}{RESET}")
    out.append("")
    return "\n".join(out)


def run_once(text):
    name, line = parse_query(text)
    if not name:
        return
    try:
        print(f"{DIM}Crunching numbers for '{name}'...{RESET}", flush=True)
        print(render(predict(name, line)))
    except espn.ESPNError as e:
        print(f"{RED}{e}{RESET}\n")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        run_once(" ".join(argv))
        return
    print(f"{BOLD}hoopr{RESET} - NBA points over/under predictor")
    print("Type a player name and optionally the line, then hit Enter.")
    print(f"{DIM}  e.g.  'jalen brunson 27.5'   or just   'brunson'   (line is pulled from the sportsbook){RESET}")
    print(f"{DIM}  Ctrl+C or 'q' to quit{RESET}\n")
    while True:
        try:
            text = input("player [line]> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text.strip().lower() in ("q", "quit", "exit"):
            return
        run_once(text)


if __name__ == "__main__":
    main()
