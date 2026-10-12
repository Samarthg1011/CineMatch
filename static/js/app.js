/* CineMatch — Obsidian frontend
   State lives in localStorage; every rating change re-queries the engine. */

(function () {
  "use strict";

  // ---------- state ----------
  const LS_R = "cm_ratings", LS_W = "cm_watchlist";
  let ratings = load(LS_R, {});     // { movieId: stars }
  let watchlist = load(LS_W, []);   // [ card, ... ]
  let INIT = null;                  // /api/init payload
  let disc = { genre: null, mood: null };
  let modalMovie = null;

  const $ = (id) => document.getElementById(id);

  function load(k, fb) {
    try { return JSON.parse(localStorage.getItem(k)) ?? fb; } catch { return fb; }
  }
  function save() {
    localStorage.setItem(LS_R, JSON.stringify(ratings));
    localStorage.setItem(LS_W, JSON.stringify(watchlist));
    $("watchCount").textContent = watchlist.length;
    $("rateCount").textContent = Object.keys(ratings).length;
  }

  // ---------- poster art (deterministic gradient per movie) ----------
  const GENRE_HUE = {
    Action: 8, Adventure: 28, Animation: 42, Children: 48, Comedy: 52,
    Crime: 285, Documentary: 175, Drama: 215, Fantasy: 265, "Film-Noir": 255,
    Horror: 355, IMAX: 200, Musical: 320, Mystery: 250, Romance: 335,
    "Sci-Fi": 195, Thriller: 230, War: 100, Western: 30, Other: 220,
  };
  function posterCSS(m) {
    const g = (m.genres && m.genres[0]) || "Other";
    const hue = GENRE_HUE[g] ?? 220;
    const h2 = (hue + 25 + (m.id % 40)) % 360;
    const l1 = 46 + (m.id % 12), l2 = 16 + (m.id % 8);
    return `background:linear-gradient(${150 + (m.id % 40)}deg,` +
           `hsl(${hue} 62% ${l1}%),hsl(${h2} 68% ${l2}%) 78%)`;
  }

  // ---------- real posters (resolved server-side, cached in browser) ----------
  const LS_P = "cm_posters";
  let posterCache = load(LS_P, {});
  Object.keys(posterCache).forEach((k) => {
    if (!posterCache[k] || posterCache[k] === "null" || posterCache[k] === "undefined") {
      delete posterCache[k];
    }
  });
  try { localStorage.setItem(LS_P, JSON.stringify(posterCache)); } catch(e){}
  let hydrateT = null;

  function attachCover(host, url) {
    if (!url || host.querySelector("img.cover")) return;
    const img = document.createElement("img");
    img.className = "cover";
    img.alt = "";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    const drop = () => {
      img.remove();
      host.classList.remove("has-img");
      const card = host.closest(".card");
      if (card) card.classList.remove("has-img");
    };
    img.onload = () => { if (img.naturalWidth < 2) drop(); };
    img.onerror = drop;
    // must live in the DOM before src is set — a detached lazy image never loads
    host.prepend(img);
    host.classList.add("has-img");
    const card = host.closest(".card");
    if (card) card.classList.add("has-img");
    img.src = url;
  }

  function scheduleHydrate() {
    clearTimeout(hydrateT);
    hydrateT = setTimeout(hydratePosters, 150);
  }

  async function hydratePosters() {
    const hosts = [...document.querySelectorAll("[data-pid]")];
    const need = new Set();
    hosts.forEach((h) => {
      const id = h.dataset.pid;
      if (!posterCache[id]) need.add(id);
      else attachCover(h, posterCache[id]);
    });
    if (!need.size) return;
    try {
      const res = await api("/api/posters", { ids: [...need].slice(0, 30) });
      Object.entries(res.posters || {}).forEach(([id, url]) => {
        if (url) posterCache[id] = url;
      });
      localStorage.setItem(LS_P, JSON.stringify(posterCache));
      hosts.forEach((h) => {
        const url = posterCache[h.dataset.pid];
        if (url) attachCover(h, url);
      });
      if (need.size > 30) scheduleHydrate();
    } catch (e) { console.warn("poster hydration failed", e); }
  }

  async function setHeroArt(id) {
    const host = $("heroArt");
    host.innerHTML = "";
    let url = posterCache[id];
    if (!url) {
      try {
        const res = await api("/api/posters", { ids: [id] });
        Object.assign(posterCache, res.posters);
        localStorage.setItem(LS_P, JSON.stringify(posterCache));
        url = posterCache[id];
      } catch { url = null; }
    }
    if (!url) return;
    const img = document.createElement("img");
    img.alt = "";
    img.src = url;
    host.appendChild(img);
  }

  // ---------- api ----------
  async function api(path, body) {
    const opt = body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined;
    const res = await fetch(path, opt);
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }
  const recommend = (extra = {}) => api("/api/recommend", { ratings, n: 24, ...extra });

  // ---------- toast ----------
  let toastT = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 2200);
  }

  // ---------- card builders ----------
  function starRow(id, size) {
    const wrap = document.createElement("div");
    wrap.className = "stars" + (size === "lg" ? " lg" : "");
    const current = ratings[id] || 0;
    for (let s = 1; s <= 5; s++) {
      const b = document.createElement("button");
      b.className = "st" + (s <= current ? " on" : "");
      b.textContent = "★";
      b.setAttribute("aria-label", `${s} star${s > 1 ? "s" : ""}`);
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        rate(id, s);
        wrap.querySelectorAll(".st").forEach((el, i) => el.classList.toggle("on", i < s));
      });
      wrap.appendChild(b);
    }
    return wrap;
  }

  function card(m, { showReason = true, small = false } = {}) {
    const el = document.createElement("article");
    el.className = "card";

    const poster = document.createElement("button");
    poster.className = "poster";
    poster.style.cssText = posterCSS(m);
    poster.dataset.pid = m.id;
    poster.addEventListener("click", () => openMovie(m));
    if (m.match) {
      const b = document.createElement("span");
      b.className = "badge" + (m.match >= 90 ? "" : " gold");
      b.textContent = m.match + "%";
      poster.appendChild(b);
    }
    const h = document.createElement("h3");
    h.textContent = m.title;
    poster.appendChild(h);
    el.appendChild(poster);

    const body = document.createElement("div");
    body.className = "card-body";
    const bt = document.createElement("b");
    bt.className = "t";
    bt.textContent = m.title;
    body.appendChild(bt);
    const line = document.createElement("div");
    line.className = "card-line";
    const g = document.createElement("span");
    g.className = "g";
    g.textContent = `${(m.genres || [])[0] || ""}${m.year ? " · " + m.year : ""} · ★${m.avg}`;
    line.appendChild(g);
    line.appendChild(watchBtn(m));
    body.appendChild(line);
    if (!small) body.appendChild(starRow(m.id));
    if (showReason && m.reason) {
      const r = document.createElement("p");
      r.className = "reason";
      r.textContent = m.reason;
      body.appendChild(r);
    }
    el.appendChild(body);
    return el;
  }

  function watchBtn(m) {
    const inList = watchlist.some((w) => w.id === m.id);
    const b = document.createElement("button");
    b.className = "icon-btn" + (inList ? " on" : "");
    b.textContent = inList ? "✓" : "＋";
    b.setAttribute("aria-label", inList ? "Remove from watchlist" : "Add to watchlist");
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleWatch(m);
    });
    return b;
  }

  function toggleWatch(m) {
    const i = watchlist.findIndex((w) => w.id === m.id);
    if (i >= 0) { watchlist.splice(i, 1); toast(`Removed “${m.title}”`); }
    else { watchlist.push({ id: m.id, title: m.title, year: m.year, genres: m.genres, avg: m.avg }); toast(`Added “${m.title}” to watchlist`); }
    save();
    renderWatchlist();
    renderForYou();          // refresh ✓ states lazily
  }

  function rate(id, stars) {
    ratings[id] = stars;
    save();
    toast(`Rated ${stars}★ — recommendations updated`);
    scheduleRefresh();
  }
  function unrate(id) {
    delete ratings[id];
    save();
    scheduleRefresh();
    renderRatings();
  }

  let refreshT = null;
  function scheduleRefresh() {
    clearTimeout(refreshT);
    refreshT = setTimeout(() => { renderForYou(); renderDiscover(); renderRatings(); }, 350);
  }

  // ---------- FOR YOU ----------
  async function renderForYou() {
    const rails = $("rails");
    const { results, cold } = await recommend();
    $("hero").hidden = cold || !results.length;
    $("coldHero").hidden = !cold;

    rails.innerHTML = "";
    if (!cold && results.length) {
      const hero = results[0];
      $("heroReason").textContent = hero.reason || "Top pick for you";
      $("heroTitle").textContent = hero.title.toUpperCase();
      $("heroMeta").innerHTML =
        `<span class="match">${hero.match}% Match</span><span>·</span>` +
        `<span>${hero.year ?? ""}</span><span>·</span>` +
        `<span>${(hero.genres || []).join(" · ")}</span><span>·</span>` +
        `<span class="imdb">★ ${hero.avg} (${hero.n})</span>`;
      $("heroTags").textContent = (hero.tags || []).join(" / ");
      $("heroRate").onclick = () => openMovie(hero);
      $("heroSimilar").onclick = () => openMovie(hero);
      $("heroWatch").onclick = () => toggleWatch(hero);
      setHeroArt(hero.id);

      addRail(rails, "Top picks for you", results.slice(1, 13),
        `hybrid engine · ${Object.keys(ratings).length} ratings in profile`);

      // rail per strongest-rated movie
      const top = Object.entries(ratings).filter(([, s]) => s >= 4)
        .sort((a, b) => b[1] - a[1]).slice(0, 2);
      for (const [id] of top) {
        const sim = await api(`/api/similar/${id}`);
        const seed = sim.results.length ? sim.results[0].reason : null;
        if (sim.results.length)
          addRail(rails, seed ? seed.replace("Similar to ", "Because you liked ") : "More for you",
            sim.results.filter((m) => !ratings[m.id]).slice(0, 12), "item-item similarity");
      }
    }
    addRail(rails, "Loved across the catalog", (INIT ? INIT.popular : []).slice(0, 12),
      "Bayesian popular baseline");
    scheduleHydrate();
  }

  function addRail(parent, title, items, note) {
    if (!items || !items.length) return;
    const sec = document.createElement("section");
    const head = document.createElement("div");
    head.className = "rail-head";
    head.innerHTML = `<h2></h2><small></small>`;
    head.querySelector("h2").textContent = title;
    head.querySelector("small").textContent = note || "";
    const scroll = document.createElement("div");
    scroll.className = "rail-scroll";
    items.forEach((m) => scroll.appendChild(card(m)));
    sec.appendChild(head);
    sec.appendChild(scroll);
    parent.appendChild(sec);
  }

  // ---------- DISCOVER ----------
  async function renderDiscover() {
    const grid = $("discoverGrid");
    const { results } = await recommend({ n: 36, genre: disc.genre, mood: disc.mood });
    grid.innerHTML = "";
    results.forEach((m) => grid.appendChild(card(m)));
    scheduleHydrate();
  }

  function chipRow(elId, values, key, labeler) {
    const row = $(elId);
    row.innerHTML = "";
    const all = document.createElement("button");
    all.className = "chip" + (disc[key] === null ? " on" : "");
    all.textContent = key === "genre" ? "All genres" : "Any mood";
    all.onclick = () => { disc[key] = null; renderChips(); renderDiscover(); };
    row.appendChild(all);
    values.forEach((v) => {
      const c = document.createElement("button");
      c.className = "chip" + (key === "mood" ? " mood" : "") + (disc[key] === v ? " on" : "");
      c.textContent = labeler ? labeler(v) : v;
      c.onclick = () => { disc[key] = disc[key] === v ? null : v; renderChips(); renderDiscover(); };
      row.appendChild(c);
    });
  }
  const MOOD_LABEL = {
    "mind-bending": "🧠 Mind-bending", "feel-good": "💛 Feel-good", adrenaline: "⚡ Adrenaline",
    whodunit: "🕵️ Whodunit", "rainy-day": "🌧 Rainy day", classics: "🎞 Classics",
  };
  function renderChips() {
    chipRow("genreChips", INIT.genres, "genre");
    chipRow("moodChips", INIT.moods, "mood", (m) => MOOD_LABEL[m] || m);
  }

  // ---------- WATCHLIST ----------
  function renderWatchlist() {
    const grid = $("watchGrid");
    grid.innerHTML = "";
    watchlist.forEach((m) => grid.appendChild(card(m, { showReason: false })));
    $("watchEmpty").hidden = watchlist.length > 0;
    scheduleHydrate();
  }

  // ---------- RATINGS ----------
  function renderRatings() {
    const list = $("ratingsList");
    const ids = Object.keys(ratings);
    $("ratingsEmpty").hidden = ids.length > 0;
    $("ratingsTotal").textContent = ids.length ? `(${ids.length})` : "";
    list.innerHTML = "";
    const lookup = new Map();
    (INIT?.onboarding || []).concat(INIT?.popular || []).forEach((m) => lookup.set(m.id, m));
    watchlist.forEach((m) => lookup.set(m.id, m));

    ids.forEach((id) => {
      const m = lookup.get(+id) || { id: +id, title: `Movie #${id}`, genres: [], year: "", avg: "–" };
      const row = document.createElement("div");
      row.className = "rate-row";
      const chip = document.createElement("div");
      chip.className = "rate-chip";
      chip.style.cssText = posterCSS(m);
      chip.dataset.pid = m.id;
      const t = document.createElement("div");
      t.className = "t";
      t.innerHTML = `<b></b><small></small>`;
      t.querySelector("b").textContent = m.title;
      t.querySelector("small").textContent = `${(m.genres || [])[0] || ""} ${m.year ? "· " + m.year : ""}`;
      const rm = document.createElement("button");
      rm.className = "rm";
      rm.textContent = "remove";
      rm.onclick = () => unrate(id);
      row.append(chip, t, starRow(+id), rm);
      list.appendChild(row);
    });
    scheduleHydrate();
  }
  $("clearRatings").addEventListener("click", () => {
    if (!Object.keys(ratings).length) return;
    ratings = {};
    save();
    toast("Ratings cleared — back to square one");
    scheduleRefresh();
  });

  // ---------- MOVIE MODAL ----------
  async function openMovie(m) {
    modalMovie = m;
    const mp = $("mmPoster");
    mp.style.cssText = posterCSS(m);
    mp.dataset.pid = m.id;
    const oldImg = mp.querySelector("img.cover");
    if (oldImg) oldImg.remove();
    mp.classList.remove("has-img");
    $("mmTitle").textContent = m.title;
    $("mmMeta").textContent =
      `${m.year ?? ""} · ${(m.genres || []).join(" · ")} · ★${m.avg} from ${m.n ?? "—"} ratings`;
    $("mmReason").textContent = m.reason || (m.tags || []).join(" / ") || "In the CineMatch catalog";
    const starsHost = $("mmStars");
    starsHost.innerHTML = "";
    starsHost.appendChild(starRow(m.id, "lg"));
    const w = $("mmWatch");
    const inList = watchlist.some((x) => x.id === m.id);
    w.textContent = inList ? "✓ In watchlist" : "＋ Watchlist";
    w.onclick = () => { toggleWatch(m); openMovie(m); };
    $("movieModal").hidden = false;

    const sim = await api(`/api/similar/${m.id}`);
    const host = $("mmSimilar");
    host.innerHTML = "";
    sim.results.slice(0, 10).forEach((s) => host.appendChild(card(s, { showReason: false, small: true })));
    scheduleHydrate();
  }

  // ---------- ONBOARDING ----------
  function openOnboarding() {
    const grid = $("onboardGrid");
    grid.innerHTML = "";
    INIT.onboarding.forEach((m) => {
      const el = document.createElement("div");
      el.className = "ob";
      const poster = document.createElement("div");
      poster.className = "poster";
      poster.style.cssText = posterCSS(m);
      poster.dataset.pid = m.id;
      const h = document.createElement("h3");
      h.textContent = m.title;
      poster.appendChild(h);
      el.appendChild(poster);
      el.appendChild(starRow(m.id));
      grid.appendChild(el);
    });
    updateOnboardCount();
    $("onboardModal").hidden = false;
    scheduleHydrate();
  }
  function updateOnboardCount() {
    const n = Object.keys(ratings).length;
    $("onboardCount").textContent = `${n} rated`;
    $("onboardDone").disabled = n < 3;
  }
  $("onboardDone").addEventListener("click", () => {
    $("onboardModal").hidden = true;
    switchView("foryou");
    renderForYou();
  });
  $("coldStart").addEventListener("click", openOnboarding);

  // observe rating changes to keep onboarding footer honest
  new MutationObserver(updateOnboardCount).observe($("onboardGrid"), { subtree: true, attributes: true });

  // ---------- SEARCH ----------
  const sInput = $("searchInput"), sResults = $("searchResults");
  let searchT = null;
  sInput.addEventListener("input", () => {
    clearTimeout(searchT);
    const q = sInput.value.trim();
    if (q.length < 2) { sResults.hidden = true; return; }
    searchT = setTimeout(async () => {
      const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
      sResults.innerHTML = "";
      results.slice(0, 9).forEach((m) => {
        const b = document.createElement("button");
        b.className = "sr-item";
        const chip = document.createElement("span");
        chip.className = "sr-chip";
        chip.style.cssText = posterCSS(m);
        chip.dataset.pid = m.id;
        const t = document.createElement("span");
        t.innerHTML = "<b></b><small></small>";
        t.querySelector("b").textContent = m.title;
        t.querySelector("small").textContent =
          `${m.year ?? ""} · ${(m.genres || []).join(", ")} · ★${m.avg}`;
        b.append(chip, t);
        b.onclick = () => { sResults.hidden = true; sInput.value = ""; openMovie(m); };
        sResults.appendChild(b);
      });
      sResults.hidden = results.length === 0;
      scheduleHydrate();
    }, 220);
  });
  document.addEventListener("click", (e) => {
    if (!$("searchBox").contains(e.target)) sResults.hidden = true;
  });

  // ---------- NAV / MODALS ----------
  function switchView(name) {
    document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
    document.querySelectorAll(".navlink").forEach((b) =>
      b.classList.toggle("active", b.dataset.nav === name));
    if (name === "discover") renderDiscover();
    if (name === "watchlist") renderWatchlist();
    if (name === "ratings") renderRatings();
    window.scrollTo({ top: 0 });
  }
  document.querySelectorAll("[data-nav]").forEach((b) =>
    b.addEventListener("click", (e) => { e.preventDefault(); switchView(b.dataset.nav); }));
  document.querySelectorAll(".modal").forEach((m) => {
    m.addEventListener("click", (e) => {
      if (e.target === m || e.target.closest("[data-close]")) m.hidden = true;
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".modal").forEach((m) => (m.hidden = true));
  });

  // ---------- boot ----------
  (async function boot() {
    // ?demo=1 seeds a sample taste profile (handy for demos & screenshots)
    if (new URLSearchParams(location.search).has("demo") && !Object.keys(ratings).length) {
      ratings = { 318: 5, 858: 5, 2959: 4, 79132: 5, 527: 3 };
      save();
    }
    INIT = await api("/api/init");
    $("footMeta").textContent =
      `${INIT.meta.n_movies.toLocaleString()} movies · ${INIT.meta.n_ratings.toLocaleString()} ratings · ` +
      `${INIT.meta.n_users} users · ${INIT.meta.dataset}`;
    save();
    renderChips();
    await renderForYou();
    if (!Object.keys(ratings).length) openOnboarding();
  })().catch((err) => {
    console.error(err);
    toast("Failed to reach the engine — is the server running?");
  });
})();
