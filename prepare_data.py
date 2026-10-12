"""
CineMatch — data preparation pipeline.

Downloads the MovieLens `ml-latest-small` dataset (100k ratings, ~9.7k movies),
then precomputes everything the recommendation engine needs at runtime:

  data/movies.json     catalog: id, title, year, genres, tags, avg rating, popularity
  data/neighbors.json  per-movie top-K neighbors from two models:
                         cf — item-item collaborative filtering (cosine over
                              mean-centered user ratings)
                         cb — content-based (TF-IDF over genres + user tags)
  data/meta.json       dataset stats for the UI

Run once before starting the app:  python prepare_data.py
"""

import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize

DATA_URL = "https://files.grouplens.org/datasets/movielens/ml-latest-small.zip"
ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"

TOP_K = 30          # neighbors kept per movie per model
MIN_RATINGS_CF = 5  # movies need this many ratings to enter the CF model
BAYES_M = 20        # prior weight for the Bayesian popularity average


def download():
    RAW.mkdir(parents=True, exist_ok=True)
    marker_movies = RAW / "ml-latest-small" / "movies.csv"
    marker_ratings = RAW / "ml-latest-small" / "ratings.csv"
    if marker_movies.exists() and marker_ratings.exists():
        print("raw data already present, skipping download")
        return
    print(f"downloading {DATA_URL} ...")
    with urllib.request.urlopen(DATA_URL, timeout=60) as resp:
        buf = io.BytesIO(resp.read())
    with zipfile.ZipFile(buf) as zf:
        zf.extractall(RAW)
    print("extracted to", RAW)


def parse_title(raw_title: str):
    """'Heat (1995)' -> ('Heat', 1995); also fixes ', The' suffixes."""
    m = re.match(r"^(.*)\s\((\d{4})\)\s*$", raw_title)
    title, year = (m.group(1), int(m.group(2))) if m else (raw_title.strip(), None)
    for art in (", The", ", A", ", An"):
        if title.endswith(art):
            title = art[2:] + " " + title[: -len(art)]
    return title, year


def main():
    t0 = time.time()
    download()
    base = RAW / "ml-latest-small"

    movies = pd.read_csv(base / "movies.csv")
    ratings = pd.read_csv(base / "ratings.csv")
    tags = pd.read_csv(base / "tags.csv")

    # ---- catalog -----------------------------------------------------------
    parsed = movies["title"].map(parse_title)
    movies["clean_title"] = parsed.map(lambda t: t[0])
    movies["year"] = parsed.map(lambda t: t[1])
    movies["genres"] = movies["genres"].str.replace("(no genres listed)", "", regex=False)

    stats = ratings.groupby("movieId")["rating"].agg(["mean", "count"])
    movies = movies.merge(stats, left_on="movieId", right_index=True, how="left")
    movies["mean"] = movies["mean"].fillna(0.0)
    movies["count"] = movies["count"].fillna(0).astype(int)

    # Bayesian popularity: shrink sparse averages toward the global mean
    C = ratings["rating"].mean()
    v, R = movies["count"], movies["mean"]
    movies["pop"] = (v / (v + BAYES_M)) * R + (BAYES_M / (v + BAYES_M)) * C

    # top user tags per movie (for content model + UI chips)
    tags["tag"] = tags["tag"].astype(str).str.lower().str.strip()
    tag_counts = (
        tags.groupby(["movieId", "tag"]).size().reset_index(name="n")
        .sort_values(["movieId", "n"], ascending=[True, False])
    )
    top_tags = tag_counts.groupby("movieId")["tag"].apply(lambda s: list(s.head(8)))
    movies["tags"] = movies["movieId"].map(top_tags)
    movies["tags"] = movies["tags"].apply(lambda x: x if isinstance(x, list) else [])

    # ---- collaborative model (item-item cosine) ----------------------------
    cf_ids = stats.index[stats["count"] >= MIN_RATINGS_CF]
    r = ratings[ratings["movieId"].isin(cf_ids)].copy()
    # mean-center per user so cosine reflects taste, not rating habits
    r["centered"] = r["rating"] - r.groupby("userId")["rating"].transform("mean")

    uid = {u: i for i, u in enumerate(r["userId"].unique())}
    mid = {m: i for i, m in enumerate(r["movieId"].unique())}
    rid = {i: m for m, i in mid.items()}
    X = csr_matrix(
        (r["centered"], (r["movieId"].map(mid), r["userId"].map(uid))),
        shape=(len(mid), len(uid)),
    )
    Xn = normalize(X)  # rows to unit length -> dot product == cosine

    print(f"CF matrix: {X.shape[0]} movies x {X.shape[1]} users")
    cf_neighbors = {}
    CHUNK = 500
    for start in range(0, Xn.shape[0], CHUNK):
        sims = (Xn[start : start + CHUNK] @ Xn.T).toarray()
        for row_off in range(sims.shape[0]):
            row = sims[row_off]
            i = start + row_off
            row[i] = -1.0  # drop self
            idx = np.argpartition(row, -TOP_K)[-TOP_K:]
            idx = idx[np.argsort(row[idx])[::-1]]
            cf_neighbors[rid[i]] = [
                [int(rid[j]), round(float(row[j]), 4)] for j in idx if row[j] > 0.05
            ]

    # ---- content model (TF-IDF over genres + tags) -------------------------
    def doc(row):
        genres = row["genres"].replace("|", " ").lower()
        tag_text = " ".join(t.replace(" ", "_") for t in row["tags"])
        return f"{genres} {genres} {genres} {tag_text}"  # genres weighted 3x

    docs = movies.apply(doc, axis=1)
    keep = docs.str.strip().ne("")
    cb_movies = movies[keep]
    tfidf = TfidfVectorizer(min_df=2)
    M = tfidf.fit_transform(docs[keep])
    print(f"CB matrix: {M.shape[0]} movies x {M.shape[1]} terms")

    cb_ids = cb_movies["movieId"].to_numpy()
    cb_neighbors = {}
    for start in range(0, M.shape[0], CHUNK):
        sims = cosine_similarity(M[start : start + CHUNK], M)
        for row_off in range(sims.shape[0]):
            row = sims[row_off]
            i = start + row_off
            row[i] = -1.0
            idx = np.argpartition(row, -TOP_K)[-TOP_K:]
            idx = idx[np.argsort(row[idx])[::-1]]
            cb_neighbors[int(cb_ids[i])] = [
                [int(cb_ids[j]), round(float(row[j]), 4)] for j in idx if row[j] > 0.05
            ]

    # ---- write artifacts ----------------------------------------------------
    catalog = [
        {
            "id": int(m.movieId),
            "title": m.clean_title,
            "year": int(m.year) if pd.notna(m.year) else None,
            "genres": [g for g in m.genres.split("|") if g],
            "tags": m.tags[:5],
            "avg": round(float(m.mean), 2),
            "n": int(m.count),
            "pop": round(float(m.pop), 3),
        }
        for m in movies.itertuples()
    ]
    neighbors = {
        str(mv): {
            "cf": cf_neighbors.get(mv, []),
            "cb": cb_neighbors.get(mv, []),
        }
        for mv in movies["movieId"]
        if cf_neighbors.get(mv) or cb_neighbors.get(mv)
    }
    genres = sorted({g for m in catalog for g in m["genres"]})
    meta = {
        "n_movies": len(catalog),
        "n_ratings": int(len(ratings)),
        "n_users": int(ratings["userId"].nunique()),
        "genres": genres,
        "dataset": "MovieLens ml-latest-small",
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "movies.json").write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
    (OUT / "neighbors.json").write_text(json.dumps(neighbors, separators=(",", ":")), encoding="utf-8")
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"wrote {len(catalog)} movies, {len(neighbors)} neighbor lists "
          f"in {time.time()-t0:.1f}s -> {OUT}")


if __name__ == "__main__":
    sys.exit(main())
