"""Export the dashboard as a static site (for GitHub Pages): HTML/CSS/JS + JSON snapshots of the API."""
import json
import shutil
import sys
from pathlib import Path

from . import main, model
from .db import init_db

ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def export(out: Path):
    init_db()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "static", out / "static")
    html = (ROOT / "static" / "index.html").read_text()
    html = html.replace('<script src="static/app.js">', '<script>window.STATIC_DATA = true;</script>\n<script src="static/app.js">')
    (out / "index.html").write_text(html)
    (out / ".nojekyll").write_text("")

    data = out / "data"
    series = main.list_series()
    write(data / "series.json", series)
    for s in series:
        write(data / "obs" / f"{s['id']}.json", main.observations(s["id"]).get(s["id"], []))
    write(data / "vn-latest.json", main.vn_latest())
    write(data / "fetch-log.json", main.fetch_log())
    write(data / "forecast.json", model.load())


if __name__ == "__main__":
    export(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
