"""Bundle the site and a data snapshot into one self-contained HTML page.

For hosts that block outside requests (claude.ai artifacts). The output has no
doctype/html/head/body tags because the artifact host adds its own skeleton.

    python3 tools/build_snapshot.py build/snapshot.json
    python3 tools/build_preview.py build/snapshot.json build/preview.html
"""

import json
import os
import re
import sys

SITE = os.path.join(os.path.dirname(__file__), "..", "site")


def read(name):
    with open(os.path.join(SITE, name)) as f:
        return f.read()


def as_inline(js):
    """Strip ES module imports/exports so the files can share one inline script."""
    js = re.sub(r"^import .*?;\n", "", js, flags=re.M)
    return re.sub(r"^export (?=(const|function|async|class) )", "", js, flags=re.M)


def main(snap_path, out_path):
    with open(snap_path) as f:
        snap = json.load(f)
    page = read("index.html")
    markup = page.split("<!-- page -->")[1].split("<!-- /page -->")[0]
    fonts = re.search(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com[^"]+)">', page).group(1)
    data = json.dumps(snap, separators=(",", ":")).replace("</", "<\\/")
    script = "\n".join(as_inline(read(n)) for n in ("hoopr.js", "snapshot.js", "ui.js"))
    html = f"""<title>hoopr</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="{fonts}">
<style>
{read("style.css")}
</style>
{markup}
<script type="application/json" id="snapshot">{data}</script>
<script type="module">
{script}

const snap = JSON.parse(document.getElementById("snapshot").textContent);
const taken = new Date(snap.generated);
const hours = (Date.now() - taken) / 3600000;
document.getElementById("stamp").innerHTML =
  `Data snapshot from <b>${{taken.toLocaleString([], {{ weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }})}}</b>` +
  (hours > 24 ? ". Lines and injuries may have changed since then." : ". Lines and injuries are as of that time.");
const board = lineBoard(snap);
mount({{ source: snapshotSource(snap), board, example: board.length ? board[0].players[0].name : null }});
</script>
"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    print(f"wrote {out_path}: {os.path.getsize(out_path) / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
