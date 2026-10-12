"""
CineMatch — Flask application.

Serves the Obsidian UI and a small JSON API over the hybrid engine.

Run:
    python prepare_data.py   (first time only)
    python app.py            (dev)  or  python serve.py  (waitress, prod)
"""

from flask import Flask, jsonify, render_template, request

from engine import Engine, MOODS
from posters import PosterResolver

app = Flask(__name__)
engine = Engine()
posters = PosterResolver()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,DELETE,OPTIONS"
    return response


def clean_ratings(raw, limit=500):
    """Accept {id: stars}; silently drop anything malformed instead of 500-ing."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in list(raw.items())[:limit]:
        try:
            out[int(k)] = min(5.0, max(0.5, float(v)))
        except (TypeError, ValueError):
            continue
    return out


def clean_n(raw, default=24, hi=60):
    try:
        return max(1, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/init")
def api_init():
    return jsonify({
        "meta": engine.meta,
        "genres": engine.meta["genres"],
        "moods": list(MOODS.keys()),
        "onboarding": engine.onboarding(36),
        "popular": engine.popular(18),
    })


@app.post("/api/recommend")
def api_recommend():
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    out = engine.recommend(
        clean_ratings(body.get("ratings")),
        n=clean_n(body.get("n")),
        genre=body.get("genre") or None,
        mood=body.get("mood") or None,
    )
    return jsonify(out)


@app.get("/api/similar/<int:movie_id>")
def api_similar(movie_id):
    return jsonify({"results": engine.similar(movie_id)})


@app.get("/api/search")
def api_search():
    return jsonify({"results": engine.search(request.args.get("q", ""))})


@app.post("/api/posters")
def api_posters():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") if isinstance(body.get("ids"), list) else []
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, (int, str))))[:60]
    items = []
    for mid in ids:
        try:
            m = engine.by_id.get(int(mid))
        except (TypeError, ValueError):
            continue
        if m:
            items.append((m["id"], m["title"], m["year"]))
    return jsonify({"posters": posters.get_many(items)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5210, debug=False, threaded=True)
