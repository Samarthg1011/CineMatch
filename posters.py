"""
CineMatch — poster resolver.

Movie posters are resolved keylessly from the Wikipedia REST summary API:
a film article's lead image is almost always its theatrical poster. We try
"Title (YYYY film)" → "Title (film)" → "Title", validate that the page is a
film article from around the right year, and cache every answer (including
misses) in data/posters.json so each movie is looked up at most once.
"""

import csv
import json
import re
import time
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "data" / "posters.json"
LINKS_FILE = Path(__file__).parent / "data" / "raw" / "ml-latest-small" / "links.csv"
API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TIMEOUT = 8


class PosterResolver:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        if CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                self._cache = {str(k): v for k, v in data.items() if v}
            except Exception:
                self._cache = {}
        self.tmdb_links = {}
        if LINKS_FILE.exists():
            try:
                with LINKS_FILE.open(encoding="utf-8") as f:
                    for row in csv.reader(f):
                        if row and row[0] != "movieId" and len(row) >= 3 and row[2]:
                            self.tmdb_links[int(row[0])] = row[2]
            except Exception:
                pass

    def _resolve_tmdb(self, tid):
        url = f"https://www.themoviedb.org/movie/{tid}"
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                html = r.read().decode("utf-8")
                img = re.findall(r'og:image.+?content=[\x27\x22]([^\x27\x22]+)[\x27\x22]', html)
                if img:
                    return img[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _fetch_summary(page_title):
        url = API + urllib.parse.quote(page_title.replace(" ", "_"), safe="")
        req = urllib.request.Request(url, headers=UA)
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception:
                if attempt == 0:
                    time.sleep(0.4)
                continue
        return None

    @staticmethod
    def _looks_like_film(data, year):
        if not data or data.get("type") != "standard":
            return False
        blob = f"{data.get('description', '')} {data.get('extract', '')[:300]}".lower()
        if "film" not in blob and "movie" not in blob:
            return False
        if year:
            years = {int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", blob)}
            if years and not any(abs(y - year) <= 2 for y in years):
                return False
        return True

    @staticmethod
    def _image_of(data):
        img = (data.get("originalimage") or {}).get("source") \
            or (data.get("thumbnail") or {}).get("source")
        return img

    @staticmethod
    def _search_title(clean_title, year):
        query = f"{clean_title} {year or ''} film".strip()
        url = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=" + urllib.parse.quote(query) + "&srlimit=2&format=json"
        req = urllib.request.Request(url, headers=UA)
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = json.loads(r.read().decode("utf-8"))
                    return [item["title"] for item in data.get("query", {}).get("search", [])]
            except Exception:
                if attempt == 0:
                    time.sleep(0.4)
                continue
        return []

    def _resolve_one(self, mid, title, year):
        if mid and int(mid) in self.tmdb_links:
            img = self._resolve_tmdb(self.tmdb_links[int(mid)])
            if img:
                return img

        clean = re.sub(r"\s*\([^)]*\)", "", title).strip()
        candidates = []
        if year:
            candidates.append(f"{clean} ({year} film)")
            candidates.append(f"{title} ({year} film)")
        candidates += [f"{clean} (film)", clean, title]
        for hit in self._search_title(clean, year):
            if hit not in candidates:
                candidates.append(hit)
        for page in candidates:
            data = self._fetch_summary(page)
            if data and self._looks_like_film(data, year):
                img = self._image_of(data)
                if img:
                    return img
        return None

    # -- public --------------------------------------------------------------

    def get_many(self, items):
        """items: [(movie_id, title, year)] -> {movie_id: url|None}"""
        out, missing = {}, []
        with self._lock:
            for mid, title, year in items:
                key = str(mid)
                if key in self._cache and self._cache[key] is not None:
                    out[mid] = self._cache[key]
                else:
                    missing.append((mid, title, year))

        if missing:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = pool.map(lambda it: (it[0], self._resolve_one(it[0], it[1], it[2])), missing)
            fresh = {k: v for k, v in results if v}
            out.update(fresh)
            if fresh:
                with self._lock:
                    self._cache.update({str(k): v for k, v in fresh.items()})
                    try:
                        CACHE_FILE.write_text(
                            json.dumps(self._cache, separators=(",", ":")), encoding="utf-8")
                    except OSError:
                        pass
        return out
