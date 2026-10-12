"""
CineMatch — offline evaluation.

Leave-one-out protocol: for each user the most recent rating of >= 4 stars is
held out. The item-item collaborative model is REBUILT from the remaining
ratings (so the held-out film cannot leak into the neighbours), the user's
other ratings are fed to the engine as their profile, and we ask whether the
held-out film appears in the top-10 of the full catalogue.

Metrics: HR@10 (hit rate), NDCG@10, and catalogue coverage (share of the
catalogue that is ever recommended) — accuracy alone rewards popularity bias.

    python evaluate.py            # 400 sampled users, prints a markdown table
    python evaluate.py 610        # every user
"""

import math
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.preprocessing import normalize

import engine as eng

ROOT = Path(__file__).parent
RATINGS = ROOT / "data" / "raw" / "ml-latest-small" / "ratings.csv"
K = 10
TOP_K = 30
MIN_RATINGS_CF = 5
BAYES_M = 20


def build_cf(train):
    """Item-item cosine over mean-centred ratings — same recipe as prepare_data."""
    counts = train.groupby("movieId")["rating"].count()
    r = train[train["movieId"].isin(counts.index[counts >= MIN_RATINGS_CF])].copy()
    r["c"] = r["rating"] - r.groupby("userId")["rating"].transform("mean")
    uid = {u: i for i, u in enumerate(r["userId"].unique())}
    mid = {m: i for i, m in enumerate(r["movieId"].unique())}
    rid = {i: m for m, i in mid.items()}
    X = csr_matrix((r["c"], (r["movieId"].map(mid), r["userId"].map(uid))),
                   shape=(len(mid), len(uid)))
    Xn = normalize(X)
    out = {}
    for start in range(0, Xn.shape[0], 500):
        sims = (Xn[start:start + 500] @ Xn.T).toarray()
        for off in range(sims.shape[0]):
            row, i = sims[off], start + off
            row[i] = -1.0
            idx = np.argpartition(row, -TOP_K)[-TOP_K:]
            idx = idx[np.argsort(row[idx])[::-1]]
            out[rid[i]] = [[int(rid[j]), float(row[j])] for j in idx if row[j] > 0.05]
    return out


def main():
    n_users = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    df = pd.read_csv(RATINGS).sort_values(["userId", "timestamp"])

    held = {}
    for uid, g in df.groupby("userId"):
        if len(g) < 12:
            continue
        liked = g[g["rating"] >= 4.0]
        if len(liked):
            held[int(uid)] = int(liked.iloc[-1]["movieId"])
    users = sorted(held)
    random.Random(7).shuffle(users)
    users = users[:n_users]
    print(f"{len(held)} eligible users, evaluating {len(users)}")

    drop = {(u, m) for u, m in held.items()}
    mask = [(u, m) in drop for u, m in zip(df["userId"], df["movieId"])]
    train = df[~pd.Series(mask, index=df.index)]
    profiles = {u: g.set_index("movieId")["rating"].to_dict()
                for u, g in train[train["userId"].isin(users)].groupby("userId")}

    print("rebuilding the collaborative model from training ratings only ...")
    cf = build_cf(train)

    e = eng.Engine()
    shipped = e.neighbors
    e.neighbors = {mv: {"cf": cf.get(mv, []), "cb": shipped.get(mv, {}).get("cb", [])}
                   for mv in e.by_id}
    # popularity prior from training data only
    stats = train.groupby("movieId")["rating"].agg(["mean", "count"])
    C = train["rating"].mean()
    for mv, m in e.by_id.items():
        if mv in stats.index:
            v, R = stats.loc[mv, "count"], stats.loc[mv, "mean"]
            m["pop"] = (v / (v + BAYES_M)) * R + (BAYES_M / (v + BAYES_M)) * C
            m["n"] = int(v)
        else:
            m["pop"], m["n"] = C, 0
    e.by_pop = sorted(e.by_id.values(), key=lambda m: m["pop"], reverse=True)
    pops = [m["pop"] for m in e.by_id.values() if m["n"] > 0]
    e._pop_min, e._pop_max = min(pops), max(pops)

    def run(label, fn):
        hits = ndcg = 0.0
        seen = set()
        for u in users:
            ids = [c["id"] for c in fn(profiles[u])][:K]
            seen.update(ids)
            if held[u] in ids:
                hits += 1
                ndcg += 1.0 / math.log2(ids.index(held[u]) + 2)
        n = len(users)
        return label, hits / n, ndcg / n, len(seen) / len(e.by_id)

    rows = [run("Popularity only", lambda p: e.popular(K, exclude=set(p)))]
    for name, w_cf, w_cb in [("Collaborative only", 1.0, 0.0),
                             ("Content only", 0.0, 1.0),
                             ("Hybrid 0.9 / 0.1", 0.9, 0.1),
                             ("Hybrid 0.8 / 0.2", 0.8, 0.2),
                             ("Hybrid 0.7 / 0.3", 0.7, 0.3),
                             ("Hybrid 0.65 / 0.35 (shipped)", 0.65, 0.35),
                             ("Hybrid 0.5 / 0.5", 0.5, 0.5)]:
        eng.W_CF, eng.W_CB = w_cf, w_cb
        rows.append(run(name, lambda p: e.recommend(p, n=K)["results"]))

    print(f"\n| Model | HR@{K} | NDCG@{K} | Coverage |\n|---|---|---|---|")
    for label, hr, nd, cov in rows:
        print(f"| {label} | {hr:.3f} | {nd:.3f} | {cov:.1%} |")


if __name__ == "__main__":
    main()
