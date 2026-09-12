// Jownloader — content script (runs in every frame).
// Owns: scanning this frame's images / videos / documents (with per-image dates and best-quality
// variants), the ⬇ overlay button on every video player, and the auto-download-videos switch.

(() => {
  if (window.__jownloader) return;
  window.__jownloader = true;

  const settings = { autoVideos: false, showButton: true, collect: false };
  const autoDone = new Set();      // video URLs already auto-downloaded this page
  const buttons = new Map();       // video element -> overlay button

  function host() {
    if (top === window) return location.hostname;
    try { return new URL(document.referrer).hostname || location.hostname; } catch { return location.hostname; }
  }

  // ---------- scanning ----------
  // "" would resolve to the page itself, so an empty src is "no URL", never a candidate
  function abs(u) { if (!u) return null; try { return new URL(u, location.href).href; } catch { return null; } }
  function okUrl(u) { return u && /^https?:/.test(u); }

  function bestOf(cands) {
    // cands: [{u, n}] where n = srcset width/density; highest wins, else first usable
    const ok = cands.filter((c) => okUrl(c.u));
    if (!ok.length) return null;
    ok.sort((a, b) => (b.n || 0) - (a.n || 0));
    return ok[0].u;
  }
  function srcsetCands(srcset) {
    return srcset.split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
      const [u, d] = s.split(/\s+/);
      return { u: abs(u), n: parseFloat(d) || 0 };
    });
  }
  // ---------- upload / published date for an element ----------
  const MONTH_RE = "(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*";
  const YEAR = "(?:\\d{4}|'\\d{2})";     // 2021 or '21
  const TEXT_DATE = new RegExp("\\b(\\d{4}-\\d{2}-\\d{2}|" + MONTH_RE + "\\.? \\d{1,2},? " + YEAR + "|\\d{1,2} " + MONTH_RE + "\\.? " + YEAR + "|\\d{1,2}/\\d{1,2}/\\d{2,4})(?!\\d)", "i");   // (?!\\d): the next text node may start right after the year
  const TEXT_DATE_ALL = new RegExp(TEXT_DATE.source, "gi");
  // returns the LOCAL calendar date "YYYY-MM-DD" (a bare date like 2025-03-07 stays that day; a timestamp is
  // shown in the browser's timezone), or "" when nothing parses
  function ymd(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
  function parseable(str) {
    if (!str) return "";
    str = String(str).trim();
    const bare = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str);
    if (bare) return str;
    if (/^\d{9,13}$/.test(str)) { const t = Number(str) * (str.length <= 10 ? 1000 : 1); return t > 946684800000 ? ymd(new Date(t)) : ""; }
    // a string with no year ("Jul 28, 3:05 pm" in a tooltip) parses to 2001 in V8: not a date we know
    const t = /\d{4}|'\d{2}\b/.test(str) ? Date.parse(str) : NaN;
    if (!isNaN(t) && t > 946684800000 && t < Date.now() + 31536000000) return ymd(new Date(t));
    const m = TEXT_DATE.exec(str);
    if (m) { const t2 = Date.parse(m[1]); if (!isNaN(t2) && t2 > 946684800000) return ymd(new Date(t2)); }
    return "";
  }
  let pageDate;
  function pageDateOf() {
    if (pageDate !== undefined) return pageDate;
    pageDate = "";
    for (const sel of ['meta[property="article:published_time"]', 'meta[property="og:updated_time"]', 'meta[itemprop="uploadDate"]', 'meta[itemprop="datePublished"]', 'meta[name="date"]']) {
      const m = document.querySelector(sel); if (m?.content && (pageDate = parseable(m.content))) return pageDate;
    }
    for (const sc of document.querySelectorAll('script[type="application/ld+json"]')) {
      const m = /"(uploadDate|datePublished)"\s*:\s*"([^"]+)"/.exec(sc.textContent || "");
      if (m && (pageDate = parseable(m[2]))) return pageDate;
    }
    return pageDate;
  }
  // nearest dated thing around the element: <time datetime>, a title/datetime attribute, or visible date text
  function dateNear(el) {
    let node = el;
    for (let i = 0; node && i < 12 && node !== document.body && node !== document.documentElement; i++, node = node.parentElement) {
      const times = node.querySelectorAll("time[datetime]");
      if (times.length > 1) break;                       // this container spans several posts: stop, use the page date
      if (times.length === 1) { const d = parseable(times[0].getAttribute("datetime")) || parseable(times[0].textContent); if (d) return d; }
      const dated = [];
      for (const a of node.querySelectorAll("[datetime], [title], [data-date], [data-timestamp]")) {
        const d = parseable(a.getAttribute("datetime") || a.getAttribute("data-date") || a.getAttribute("data-timestamp") || a.getAttribute("title"));
        if (d) dated.push(d);
      }
      if (dated.length > 1) break;
      if (dated.length === 1) return dated[0];
      if (i >= 2) {
        // visible date text anywhere in this container (a lightbox header beside the picture, a post's byline):
        // exactly one distinct date → that's it; several → this box spans posts
        const found = new Set();
        for (const m of (node.textContent || "").matchAll(TEXT_DATE_ALL)) { const d = parseable(m[1]); if (d) found.add(d); if (found.size > 1) break; }
        if (found.size > 1) break;
        if (found.size === 1) return [...found][0];
      }
    }
    return pageDateOf();
  }

  // one URL per image: the largest srcset/picture candidate, else what the browser is showing
  function bestUrl(img) {
    const cands = [];
    const pic = img.closest("picture");
    if (pic) for (const s of pic.querySelectorAll("source[srcset]")) cands.push(...srcsetCands(s.srcset));
    if (img.srcset) cands.push(...srcsetCands(img.srcset));
    return bestOf(cands) || [img.currentSrc, img.src, img.dataset.src, img.dataset.original, img.dataset.lazySrc].map(abs).find(okUrl) || null;
  }
  function scanImages() {
    const out = new Map();   // url -> {url, date}
    for (const img of document.querySelectorAll("img")) {
      if ((img.naturalWidth && img.naturalWidth < 32) || (img.naturalHeight && img.naturalHeight < 32)) continue;
      const best = bestUrl(img);
      if (best && !out.has(best)) out.set(best, { url: best, date: dateNear(img) });
    }
    for (const a of document.querySelectorAll("a[href]")) {
      if (/\.(jpe?g|png|gif|webp|avif)(\?|$)/i.test(a.href) && !out.has(a.href)) out.set(a.href, { url: a.href, date: dateNear(a) });
    }
    if (top === window) {
      const og = document.querySelector('meta[property="og:image"]');
      if (og?.content && !out.size) { const a = abs(og.content); if (okUrl(a)) out.set(a, { url: a, date: pageDateOf() }); }
    }
    pageDate = undefined;
    return [...out.values()];
  }

  // Quality variants a page's player offers. Players keep them in one of three places:
  //   <source src size|label|res|data-quality>              Plyr, video.js quality menus, plain HTML
  //   flashvars { video_url, video_url_text: '480p', video_alt_url, video_alt_url_text: '720p' … }   KVS
  //   sources: [{ file|src|url, label|height|res: '720p' }]  JW Player, Clappr, Flowplayer-style configs
  // Returns [{u, n}] with n = the variant's height in px (from its label, else a _720p in the URL, else 0).
  // A script's list belongs to a player only when it names the file that player is playing (cur), so two
  // players on one page never trade variants. Inline scripts are read once per scan, one group per script.
  const MEDIA_FILE = /\.(mp4|webm|m4v|mov)(\?|$)/i;
  const heightOf = (label, u) => parseInt(label, 10) || parseInt((/[_-](\d{3,4})p\b/.exec(u) || [])[1] || "", 10) || 0;
  const fileOf = (u) => { try { return decodeURIComponent(new URL(u).pathname).split("/").filter(Boolean).pop() || ""; } catch { return u; } };
  let scriptGroups;
  function playerVariants(v, cur) {
    const out = [];
    for (const s of v.querySelectorAll("source")) {
      const u = abs(s.src); if (!okUrl(u)) continue;
      out.push({ u, n: heightOf(s.getAttribute("size") || s.getAttribute("label") || s.getAttribute("res") || s.dataset.quality || s.title || "", u) });
    }
    if (!scriptGroups) {
      scriptGroups = [];
      for (const sc of document.querySelectorAll("script:not([src])")) {
        const t = sc.textContent || "", g = [];
        const urls = {}, texts = {};
        for (const m of t.matchAll(/\b(video(?:_alt)?_url\d*)(_text)?\s*:\s*['"]([^'"]+)['"]/g)) (m[2] ? texts : urls)[m[1]] = m[3];
        for (const k in urls) { const u = abs(urls[k]); if (okUrl(u)) g.push({ u, n: heightOf(texts[k] || "", u) }); }
        for (const m of t.matchAll(/\{[^{}]*\}/g)) {
          const f = /\b(?:file|src|url)\s*:\s*['"]([^'"]+)['"]/.exec(m[0]); if (!f) continue;
          const u = abs(f[1]); if (!okUrl(u) || !MEDIA_FILE.test(u)) continue;
          const l = /\b(?:label|height|res|quality|size)\s*:\s*['"]?(\d{3,4})/.exec(m[0]);
          g.push({ u, n: heightOf(l ? l[1] : "", u) });
        }
        if (g.length) scriptGroups.push(g);
      }
    }
    const f = fileOf(cur);
    for (const g of scriptGroups) if (g.some((c) => fileOf(c.u) === f)) out.push(...g);
    return out;
  }
  function stem(u) { try { return decodeURIComponent(new URL(u).pathname).split("/").filter(Boolean).pop().replace(/[_-]?\d{3,4}p(\.\w+)?$/, "").replace(/\.\w+$/, ""); } catch { return ""; } }
  function videoUrls(v) {
    const cands = [v.currentSrc, v.src, ...[...v.querySelectorAll("source")].map((s) => s.src)];
    const usable = cands.map((u) => (u && u.startsWith("blob:") ? u : abs(u))).filter((u) => u && /^(https?|blob):/.test(u));
    if (!usable.length) return [];
    const cur = usable[0];
    if (cur.startsWith("blob:")) return [cur];
    // best quality of the same video, if the player lists variants (e.g. 480p playing, 1080p offered).
    // A variant is only "better" when it is a different FILE — a different filename (17909_1080p.mp4 vs
    // 17909_2160p.mp4). Same filename = the file already playing, whatever token sits in the path; the
    // browser's own URL (fresh token) is the one that downloads, the script's copy 404s.
    const s = stem(cur), mine = playerVariants(v, cur);
    // what is playing measures by its decoded height; before any frame is decoded, by its own label
    const curH = v.videoHeight || (mine.find((c) => c.u === cur) || {}).n || heightOf("", cur);
    const better = mine.filter((c) => s && stem(c.u) === s && fileOf(c.u) !== fileOf(cur)).sort((a, b) => b.n - a.n)[0];
    return [better && better.n > curH ? better.u : cur];
  }
  // A grid's hover/teaser loop is not the video: tiny frame, muted, looping, no controls. Never offered.
  const PREVIEW_H = 360;
  const isPreview = (v) => (v.videoHeight > 0 && v.videoHeight < PREVIEW_H) || (v.muted && v.loop && !v.controls);
  function scanVideos() {
    scriptGroups = null;
    const out = new Map();
    for (const v of document.querySelectorAll("video")) {
      if (isPreview(v)) continue;
      const urls = videoUrls(v);
      const poster = abs(v.poster) || "";
      const date = dateNear(v);
      for (const u of urls) out.set(u, { url: u, blob: u.startsWith("blob:"), drm: !!v.mediaKeys, poster, playing: !v.paused, w: v.videoWidth, h: v.videoHeight, date });
    }
    for (const a of document.querySelectorAll("a[href]")) {
      if (/\.(mp4|webm|mov|m4v|mkv)(\?|$)/i.test(a.href)) out.set(a.href, { url: a.href, blob: false });
    }
    return [...out.values()];
  }

  const DOC_RE = /\.(pdf|docx?|xlsx?|pptx?|odt|ods|odp|epub|mobi|txt|md|rtf|csv|zip|7z|rar|gz|tgz|dmg|pkg)(\?|#|$)/i;
  function scanDocs() {
    const out = new Map();
    for (const a of document.querySelectorAll("a[href], iframe[src], embed[src], object[data]")) {
      const u = a.href || a.src || a.data;
      if (!okUrl(u) || !DOC_RE.test(u)) continue;
      out.set(u, { url: u, text: (a.textContent || a.title || "").trim().slice(0, 80) });
    }
    // the page itself is a PDF / plain document
    if (top === window && /^(application\/pdf|text\/plain)$/.test(document.contentType || "")) out.set(location.href, { url: location.href, text: document.title });
    return [...out.values()];
  }

  chrome.runtime.onMessage.addListener((msg, _s, reply) => {
    if (msg.type === "scanFrame") reply({ host: host(), images: scanImages(), videos: scanVideos(), docs: scanDocs() });
    else if (msg.type === "pageChanged") { autoDone.clear(); collected.clear(); }   // load or pushState: a new page
    return false;
  });

  // ---------- download helpers ----------
  async function downloadVideo(v, btn) {
    if (v.mediaKeys) { flash(btn, "protected video (DRM) — can't be saved by any tool", 4000); return; }
    const urls = videoUrls(v).filter((u) => !u.startsWith("blob:"));
    const date = dateNear(v);
    let items = urls.map((url) => ({ url, date }));
    if (!items.length) {
      items = (await capturedWhole()).map((c) => ({ url: c.url, ctype: c.ctype, date }));
      if (!items.length) { flash(btn, "streamed video (HLS/DASH) — segments, not grabbable here", 4000); return; }
    }
    const r = await chrome.runtime.sendMessage({ type: "download", items, kind: "video" });
    flash(btn, r?.queued ? `saving ${r.queued} → ${(await chrome.runtime.sendMessage({ type: "getSettings" }))?.dest?.split("/").filter(Boolean).pop() || "Downloads"}` : r?.skipped ? "already saved before" : "failed", 4000);
  }
  // Whole files the tab's network layer saw, offered for an MSE/blob player ONLY when the tab holds no
  // stream manifest or segment: if it does, the player is a segment stream and any whole file in the
  // list is some other clip (a grid preview), never this video.
  async function capturedWhole() {
    const cap = (await chrome.runtime.sendMessage({ type: "getMedia" }).catch(() => null)) || [];
    return cap.some((c) => !c.whole) ? [] : cap.filter((c) => c.whole);
  }
  function flash(btn, text, ms = 2000) {
    if (!btn) return;
    btn.textContent = text;
    clearTimeout(btn._t);
    btn._t = setTimeout(() => { btn._t = 0; btn.textContent = label(btn._v); }, ms);
  }

  // ---------- overlay button ----------
  function label(v) {
    const [u] = videoUrls(v);
    if (v.mediaKeys) return "⬇ protected (DRM)";
    // real pixels of what is playing beat any number in the URL (sites label files above their real size);
    // a variant the player offers beyond what's playing is shown as the site's claim, marked so
    if (u === (v.currentSrc || v.src) && v.videoWidth) return `⬇ Download ${v.videoWidth}×${v.videoHeight}`;
    const pv = u && playerVariants(v, v.currentSrc || v.src).find((c) => c.u === u);
    const h = (pv && pv.n) || (u && !u.startsWith("blob:") && (parseInt((/_(\d{3,4})p\b/.exec(u) || [])[1], 10) || 0)) || v.videoHeight || 0;
    return "⬇ Download" + (h ? ` "${h}p" (site's label)` : "");
  }
  function makeButton(v) {
    const b = document.createElement("button");
    b._v = v;
    b.textContent = label(v);
    b.setAttribute("data-jownloader", "1");
    Object.assign(b.style, {
      position: "fixed", zIndex: "2147483647", padding: "6px 10px", font: "600 13px system-ui, sans-serif",
      color: "#fff", background: "rgba(20,20,20,.85)", border: "1px solid rgba(255,255,255,.5)",
      borderRadius: "6px", cursor: "pointer", opacity: "0.9", pointerEvents: "auto",
    });
    b.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); downloadVideo(v, b); }, true);
    document.documentElement.appendChild(b);
    return b;
  }
  function place(v, b) {
    const r = v.getBoundingClientRect();
    const visible = r.width > 80 && r.height > 60 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
    b.style.display = visible ? "block" : "none";
    b.style.left = Math.max(0, r.left + 8) + "px";
    b.style.top = Math.max(0, r.top + 8) + "px";
  }
  function refreshButtons() {
    for (const v of document.querySelectorAll("video")) {
      const want = settings.showButton && !isPreview(v) && (!!(v.currentSrc || v.src || v.querySelector("source")) || !v.paused);
      let b = buttons.get(v);
      if (want && !b) { b = makeButton(v); buttons.set(v, b); }
      if (b && !b._t) { const l = label(v); if (b.textContent !== l) b.textContent = l; }
      if (!want && b) { b.remove(); buttons.delete(v); }
      if (b) place(v, b);
    }
    for (const [v, b] of buttons) if (!v.isConnected) { b.remove(); buttons.delete(v); }
  }
  let raf = 0;
  function tick() { refreshButtons(); raf = buttons.size ? requestAnimationFrame(tick) : 0; }
  let collectTimer = 0;
  function kick() {
    refreshButtons(); if (buttons.size && !raf) raf = requestAnimationFrame(tick);
    if (settings.collect && !collectTimer) collectTimer = setTimeout(() => { collectTimer = 0; collectVisible(); }, 200);
  }

  // ---------- auto-download switch ----------
  async function autoGrab(v) {
    if (!settings.autoVideos || v.mediaKeys || isPreview(v)) return;
    let items = videoUrls(v).filter((u) => !u.startsWith("blob:")).map((url) => ({ url }));
    if (!items.length && !v.paused) items = (await capturedWhole()).map((c) => ({ url: c.url, ctype: c.ctype }));
    items = items.filter((i) => !autoDone.has(i.url));
    if (!items.length) return;
    items.forEach((i) => autoDone.add(i.url));
    chrome.runtime.sendMessage({ type: "download", items, kind: "video" });
  }
  function autoGrabAll() { if (settings.autoVideos) document.querySelectorAll("video").forEach(autoGrab); }

  // ---------- collect as you scroll ----------
  // With the "Collect as I scroll" switch on, every image that is ON SCREEN and rendered at least
  // COLLECT_MIN px on its shorter side is reported once to the background. Thumbnails render small,
  // so a lightbox or feed of full posts collects the real pictures and leaves the grid behind them alone.
  const COLLECT_MIN = 300;
  const collected = new Set();     // URLs this frame already reported
  const IMG_EXT = /\.(jpe?g|png|gif|webp|avif)(\?|$)/i;
  function collectVisible() {
    if (!settings.collect) return;
    for (const img of document.querySelectorAll("img")) {
      const r = img.getBoundingClientRect();
      const shown = Math.min(r.width, r.height);
      if (shown < COLLECT_MIN || r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) continue;
      // not loaded yet → wait for its load event; real pixels smaller than the box → an upscaled thumbnail
      // (lightboxes show the thumb blown up as a placeholder until the full file arrives), never the picture
      if (!img.naturalWidth || Math.min(img.naturalWidth, img.naturalHeight) < Math.max(COLLECT_MIN, shown)) continue;
      // the link around the picture, when it points at an image file, is the full-size original
      const a = img.closest("a[href]");
      const url = (a && IMG_EXT.test(a.href) && okUrl(a.href)) ? a.href : bestUrl(img);
      if (!url || collected.has(url)) continue;
      collected.add(url);
      chrome.runtime.sendMessage({ type: "collect", url, date: dateNear(img) }).catch(() => {});
    }
  }

  // ---------- wiring ----------
  for (const ev of ["play", "playing", "pause", "ended", "emptied"]) {
    document.addEventListener(ev, (e) => { if (e.target?.tagName === "VIDEO") { kick(); if (ev === "play" || ev === "playing") autoGrab(e.target); } }, true);
  }
  document.addEventListener("loadedmetadata", (e) => { if (e.target?.tagName === "VIDEO") { kick(); autoGrab(e.target); } }, true);
  addEventListener("scroll", kick, { passive: true });
  document.addEventListener("load", (e) => { if (e.target?.tagName === "IMG") kick(); }, true);   // lazy images sizing in
  addEventListener("resize", kick);
  new MutationObserver((recs) => {
    if (recs.every((r) => r.target.hasAttribute?.("data-jownloader"))) return;   // our own buttons changing text
    kick(); autoGrabAll();
  }).observe(document.documentElement, { childList: true, subtree: true });

  chrome.storage.sync.get({ autoVideos: false, showButton: true, collect: false }, (s) => { Object.assign(settings, s); kick(); autoGrabAll(); });
  chrome.storage.onChanged.addListener((ch, area) => {
    if (area !== "sync") return;
    for (const k in ch) settings[k] = ch[k].newValue;
    kick(); autoGrabAll();
    if (ch.collect?.newValue) collectVisible();   // switch just turned on: what's on screen now counts
  });
})();
