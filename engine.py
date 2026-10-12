"""
CineMatch — hybrid recommendation engine.

Loads the artifacts produced by prepare_data.py and serves recommendations by
blending two precomputed neighbor models:

  cf  item-item collaborative filtering  (weight 0.65)
  cb  content-based TF-IDF similarity    (weight 0.35)

A user is just a dict of {movie_id: star_rating}. Scores are accumulated over
the neighbors of everything the user rated, weighted by how much they liked it,
then blended with a popularity prior (stronger when the profile is small).
Every recommendation carries a human-readable reason.
"""

import json
import re
import threading
import zlib
from pathlib import Path

DATA = Path(__file__).parent / "data"
LIVE_CACHE = DATA / "live_titles.json"   # films found via Wikipedia; survives restarts
LIVE_ID_BASE = 900_000

W_CF = 0.8   # tuned with evaluate.py (leave-one-out HR@10); was 0.65
W_CB = 0.2
NEUTRAL = 2.75  # ratings below this count as negative signal

MOODS = {
    "mind-bending": {"genres": {"Sci-Fi", "Mystery", "Thriller"},
                     "tags": {"twist", "surreal", "psychology", "mindfuck", "nonlinear", "time travel"}},
    "feel-good":    {"genres": {"Comedy", "Romance", "Animation", "Children", "Musical"}, "tags": {"funny", "feel-good", "heartwarming"}},
    "adrenaline":   {"genres": {"Action", "Thriller", "Adventure"}, "tags": {"action", "suspense", "car chase"}},
    "whodunit":     {"genres": {"Crime", "Mystery", "Film-Noir"}, "tags": {"murder", "detective", "investigation"}},
    "rainy-day":    {"genres": {"Drama", "Romance"}, "tags": {"emotional", "melancholy", "atmospheric"}},
    "classics":     {"genres": set(), "tags": set(), "max_year": 1990},
}


class Engine:
    def __init__(self):
        movies = json.loads((DATA / "movies.json").read_text(encoding="utf-8"))
        self.by_id = {m["id"]: m for m in movies}
        self.neighbors = {
            int(k): v
            for k, v in json.loads((DATA / "neighbors.json").read_text(encoding="utf-8")).items()
        }
        self.meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
        # ranked once for popularity fallbacks / onboarding
        self.by_pop = sorted(movies, key=lambda m: m["pop"], reverse=True)
        pops = [m["pop"] for m in movies if m["n"] > 0]
        self._pop_min, self._pop_max = min(pops), max(pops)
        self._lock = threading.Lock()
        self._load_live_cache()

    # -- live titles (found on Wikipedia, kept across restarts) ----------------

    def _load_live_cache(self):
        try:
            for m in json.loads(LIVE_CACHE.read_text(encoding="utf-8")):
                self._register_live(m, persist=False)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _register_live(self, m, persist=True):
        """Add a film to the in-memory catalogue with content neighbours."""
        with self._lock:
            if m["id"] in self.by_id:
                return False
            self.by_id[m["id"]] = m
            peers = [c["id"] for c in self.by_pop
                     if c["id"] < LIVE_ID_BASE and set(c["genres"]) & set(m["genres"])][:20]
            self.neighbors[m["id"]] = {"cf": [], "cb": [(p, 0.75) for p in peers]}
            if persist:
                live = [x for x in self.by_id.values() if x.get("src") == "live"]
                try:
                    LIVE_CACHE.write_text(json.dumps(live), encoding="utf-8")
                except OSError:
                    pass
            return True

    # -- helpers -------------------------------------------------------------

    def _pop_norm(self, m):
        span = self._pop_max - self._pop_min or 1.0
        return (m["pop"] - self._pop_min) / span

    def _matches_mood(self, m, mood):
        spec = MOODS.get(mood)
        if not spec:
            return True
        if "max_year" in spec:
            return (m["year"] or 3000) <= spec["max_year"] and m["n"] >= 20
        if spec["genres"] & set(m["genres"]):
            return True
        return bool(spec["tags"] & set(m["tags"]))

    def _card(self, m, match=None, reason=None):
        return {
            "id": m["id"], "title": m["title"], "year": m["year"],
            "genres": m["genres"][:3], "tags": m["tags"][:3],
            "avg": m["avg"], "n": m["n"],
            "match": match, "reason": reason,
        }

    # -- public API ------------------------------------------------------------

    def onboarding(self, n=36):
        """Popular, genre-diverse movies for the first-run rating picker."""
        picked, seen_genres, out = set(), {}, []
        for m in self.by_pop:
            if m["n"] < 50 or m["id"] in picked:
                continue
            main = m["genres"][0] if m["genres"] else "Other"
            if seen_genres.get(main, 0) >= 5:
                continue
            seen_genres[main] = seen_genres.get(main, 0) + 1
            picked.add(m["id"])
            out.append(self._card(m))
            if len(out) >= n:
                break
        return out

    def popular(self, n=24, genre=None, mood=None, exclude=()):
        out = []
        for m in self.by_pop:
            if m["id"] in exclude or m["n"] < 10:
                continue
            if genre and genre not in m["genres"]:
                continue
            if mood and not self._matches_mood(m, mood):
                continue
            out.append(self._card(m, match=None, reason="Loved across the catalog"))
            if len(out) >= n:
                break
        return out

    def recommend(self, user_ratings, n=24, genre=None, mood=None):
        """Hybrid scores from the user's rating profile."""
        ratings = {int(k): float(v) for k, v in (user_ratings or {}).items()
                   if int(k) in self.by_id}
        if not ratings:
            return {"results": self.popular(n, genre, mood), "cold": True}

        scores, contrib = {}, {}
        for mv, stars in ratings.items():
            w = stars - NEUTRAL  # -2.25 .. +2.25
            hood = self.neighbors.get(mv, {})
            for model, w_model in (("cf", W_CF), ("cb", W_CB)):
                for nb_id, sim in hood.get(model, []):
                    if nb_id in ratings:
                        continue
                    gain = w_model * sim * w
                    scores[nb_id] = scores.get(nb_id, 0.0) + gain
                    if gain > contrib.get(nb_id, (0.0, None))[0]:
                        contrib[nb_id] = (gain, mv)

        # blend with popularity prior; trust the profile more as it grows
        profile_trust = min(len(ratings) / 10.0, 1.0) * 0.85
        ranked = []
        for cid, s in scores.items():
            m = self.by_id[cid]
            if s <= 0:
                continue
            if genre and genre not in m["genres"]:
                continue
            if mood and not self._matches_mood(m, mood):
                continue
            final = profile_trust * s + (1 - profile_trust) * self._pop_norm(m)
            ranked.append((final, s, cid))
        ranked.sort(reverse=True)

        if not ranked:
            return {"results": self.popular(n, genre, mood, exclude=set(ratings)), "cold": True}

        top = ranked[:n]
        smax, smin = top[0][0], top[-1][0]
        span = (smax - smin) or 1.0
        results = []
        for final, s, cid in top:
            m = self.by_id[cid]
            match = int(round(72 + 27 * (final - smin) / span))
            src = contrib.get(cid, (0, None))[1]
            reason = (f"Because you rated {self.by_id[src]['title']} "
                      f"{ratings[src]:g}★") if src else "Matches your taste profile"
            results.append(self._card(m, match=match, reason=reason))

        # Personal picks only reach as far as your ratings' neighbourhood. When
        # browsing a genre or mood your taste barely touches, top the row up
        # with its best-loved titles rather than showing a nearly empty page.
        if len(results) < n:
            have = {c["id"] for c in results} | set(ratings)
            results += self.popular(n - len(results), genre, mood, exclude=have)
        return {"results": results, "cold": False}

    def similar(self, movie_id, n=12):
        m = self.by_id.get(movie_id)
        if not m:
            return []
        blended = {}
        hood = self.neighbors.get(movie_id, {})
        for model, w_model in (("cf", W_CF), ("cb", W_CB)):
            for nb_id, sim in hood.get(model, []):
                blended[nb_id] = blended.get(nb_id, 0.0) + w_model * sim
        ranked = sorted(blended.items(), key=lambda kv: kv[1], reverse=True)[:n]
        if not ranked:
            return []
        smax = ranked[0][1] or 1.0
        return [
            self._card(self.by_id[nb], match=int(round(72 + 27 * s / smax)),
                       reason=f"Similar to {m['title']}")
            for nb, s in ranked if nb in self.by_id
        ]

    def _search_external_movies(self, q, limit=6):
        import urllib.request, urllib.parse, re, json
        # generator=search + prop=description gives each hit a one-line label
        # ("2010 film by Christopher Nolan" vs "British-American filmmaker"),
        # which separates films from the people who make them in one request.
        url = ("https://en.wikipedia.org/w/api.php?action=query&generator=search"
               "&gsrsearch=" + urllib.parse.quote(f"{q} film") +
               "&gsrlimit=10&prop=description&format=json")
        req = urllib.request.Request(url, headers={"User-Agent": "CineMatch/1.0 (student portfolio project)"})
        external = []
        existing_titles = {m["title"].lower() for m in self.by_id.values()}
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                pages = list(data.get("query", {}).get("pages", {}).values())
                pages.sort(key=lambda p: p.get("index", 99))
                for idx, item in enumerate(pages):
                    title = item.get("title", "")
                    if any(bad in title.lower() for bad in ["(soundtrack)", "(album)", "(song)", "(novel)", "(character)", "list of ", "accolades"]):
                        continue
                    # keep only "(YYYY film)" / "(film)" disambiguators — drops
                    # character and franchise pages like "Morpheus (The Matrix)"
                    if "(" in title and not re.search(r"\((?:\d{4}\s+)?film\)", title, re.I):
                        continue
                    clean = re.sub(r"\s*\((19\d{2}|20\d{2})\s*film\)", "", title, flags=re.I).strip()
                    clean = re.sub(r"\s*\(film\)", "", clean, flags=re.I).strip()
                    if clean.lower() in existing_titles or title.lower() in existing_titles:
                        continue
                    snippet = (item.get("description") or "").lower()
                    # the description must describe a film, not a person or a set
                    if not re.search(r"\b(film|movie)\b", snippet):
                        continue
                    if any(bad in snippet for bad in [
                        "filmmaker", "director", "actor", "actress", "screenwriter",
                        "producer", "composer", "series of", "franchise", "film series",
                        "soundtrack", "novel", "video game", "company", "studio",
                    ]):
                        continue
                    years = re.findall(r"\b(19\d{2}|20\d{2})\b", f"{title} {snippet}")
                    year = int(years[0]) if years else 2023
                    genres = []
                    for kw, g in [("action", "Action"), ("hero", "Action"), ("sci-fi", "Sci-Fi"),
                                  ("science fiction", "Sci-Fi"), ("comedy", "Comedy"), ("horror", "Horror"),
                                  ("thriller", "Thriller"), ("drama", "Drama"), ("romance", "Romance"),
                                  ("animat", "Animation"), ("crime", "Crime"), ("adventure", "Adventure")]:
                        if kw in snippet or kw in title.lower():
                            if g not in genres:
                                genres.append(g)
                    if not genres:
                        genres = ["Drama", "Action"]
                    # crc32, not hash(): ids must survive a restart so saved ratings hold
                    mid = LIVE_ID_BASE + zlib.crc32(clean.lower().encode()) % 90_000
                    while mid in self.by_id and self.by_id[mid]["title"].lower() != clean.lower():
                        mid += 1
                    pop_val = max(4.8 - idx * 0.2, 3.5)
                    m = {
                        "id": mid,
                        "title": clean,
                        "year": year,
                        "genres": genres[:3],
                        "tags": ["cinema", "popular"],
                        "avg": 4.15,
                        "n": 100,
                        "pop": pop_val,
                        "src": "live",
                    }
                    self._register_live(m)
                    existing_titles.add(clean.lower())
                    external.append(self._card(m))
                    if len(external) >= limit:
                        break
        except Exception:
            pass
        return external

    def search(self, q, n=20):
        q = (q or "").strip().lower()
        if len(q) < 2:
            return []
        starts, contains = [], []
        for m in list(self.by_id.values()):
            t = m["title"].lower()
            if t.startswith(q):
                starts.append(m)
            elif q in t:
                contains.append(m)
        starts.sort(key=lambda m: (m["title"].lower() != q, -m["pop"]))
        contains.sort(key=lambda m: -m["pop"])
        local = [self._card(m) for m in (starts + contains)[:n]]
        # thin results usually mean the film is newer than the dataset (2018)
        if len(local) < 4:
            local += self._search_external_movies(q, limit=max(4, n - len(local)))
        return local[:n]
