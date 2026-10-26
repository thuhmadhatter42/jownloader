const $ = (id) => document.getElementById(id);
let destPath = "", scan = null, tab = null;
// the developer log: popup events go to the worker, which forwards them to the one log file (fire-and-forget)
function jlog(level, area, m, d) { try { chrome.runtime.sendMessage({ type: "log", entries: [{ t: Date.now(), l: level, a: area, m, d }] }).catch(() => {}); } catch {} }
window.addEventListener("error", (e) => jlog("error", "popup", `uncaught error: ${e.message} (${e.filename}:${e.lineno})`));
window.addEventListener("unhandledrejection", (e) => jlog("error", "popup", "unhandled rejection: " + String(e.reason?.stack || e.reason)));
// pages the engine downloads whole (the site's player API, not the page's files): YouTube, Instagram, Twitter/X
const ENGINE_RE = /^https?:\/\/(?:[\w-]+\.)?(youtube\.com|youtu\.be|instagram\.com|twitter\.com|x\.com)\//i;
function engineOf(url) {
  const m = ENGINE_RE.exec(url || "");
  if (!m) return null;
  const path = (() => { try { return new URL(url).pathname; } catch { return "/"; } })();
  const seg = path.split("/").filter(Boolean);
  if (/youtu/.test(m[1])) return { site: "youtube", name: "Video link", what: /\/(watch|shorts\/|live\/)|youtu\.be/.test(url) ? "this video (MP4)" : "" };
  if (/instagram/.test(m[1])) return { site: "instagram", name: "Post or profile", what: /^(p|reel|reels|tv)$/.test(seg[0] || "") ? "this post" : seg.length === 1 ? `everything from @${seg[0]}` : "" };
  return { site: "twitter", name: "Post or profile", what: seg[1] === "status" ? "this post's media" : seg.length && !/^(home|explore|search|settings|i|messages|notifications)$/.test(seg[0]) ? `all media from @${seg[0]}` : "" };
}
const folderName = (path) => (path || "").split("/").filter(Boolean).pop() || "Downloads";
function showDest() {
  $("destName").textContent = folderName(destPath);
  $("dest").title = (destPath || "Downloads") + " (click to choose another folder)";
  $("destClear").hidden = !destPath;
}

// one-off answers (queued, nothing new, renamed…) go to the status line of the view that is showing
const PANES = { tabDl: ["paneDl", "status"], tabPdf: ["panePdf", "pdfStatus"], tabTools: ["paneTools", "toolStatus"] };
let curTab = "tabDl";
function say(text, tone) {
  const el = $(PANES[curTab][1]);
  el.textContent = text || "";
  el.className = "status" + (tone ? " " + tone : "");
}

// {items, direct, stream}: real <video> sources when the page has them (reliable); else the HLS/DASH
// streams the tab played (one per manifest, joined into one file each); else whole files the network layer
// captured for the tab — one per file, largest variant (can include DRM segments, so less trusted)
let vi = { items: [], direct: false, stream: false };
async function videoItems() {
  const direct = scan.videos.filter((v) => !v.blob && !v.drm).map((v) => ({ url: v.url, date: v.date }));
  if (direct.length) return { items: direct, direct: true, stream: false };
  // a manifest or segment in the tab means the player streams; the whole files around it are other clips
  if (scan.captured.some((c) => !c.whole)) {
    const streams = (await chrome.runtime.sendMessage({ type: "streamCandidates", tabId: tab.id }).catch(() => null)) || [];
    return { items: streams.map((s) => ({ ...s, title: tab.title || "" })), direct: false, stream: true };
  }
  const best = new Map();
  for (const c of scan.captured.filter((c) => c.whole)) {
    let k; try { const u = new URL(c.url); k = u.origin + u.pathname; } catch { k = c.url; }
    if (!best.has(k) || (c.size || 0) > (best.get(k).size || 0)) best.set(k, c);
  }
  return { items: [...best.values()].map((c) => ({ url: c.url, ctype: c.ctype })), direct: false, stream: false };
}

// The context (video-site card, PDF buttons) comes from the tab URL at once; the counts follow the scan.
// Every row exists from the start with a dash, so nothing moves when the scan answers.
async function refresh() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const eng = engineOf(tab?.url);
  $("engineCard").hidden = !eng?.what;
  $("site").hidden = !eng?.what; $("siteAudio").hidden = eng?.site !== "youtube" || !eng.what; $("mp3Row").hidden = $("siteAudio").hidden;
  if (eng?.what) { $("site").textContent = `Save ${eng.what}`; $("engineName").textContent = eng.name; }
  const isHttp = /^https?:\/\//i.test(tab?.url || "");
  $("sitePdf").hidden = $("siteMd").hidden = !isHttp; $("pdfNone").hidden = isHttp;
  scan = await chrome.runtime.sendMessage({ type: "scan", tabId: tab.id }).catch(() => null);
  if (!scan) {
    $("host").textContent = "this page can't be read";
    for (const id of ["imgs", "vids", "docs"]) setCount(id, 0);
    jlog("warn", "scan", "the page could not be scanned", { url: tab?.url });
    return;
  }
  $("host").textContent = scan.host;
  vi = await videoItems();
  const drm = scan.videos.filter((v) => v.drm).length;
  const blobs = scan.videos.filter((v) => v.blob && !v.drm).length;
  // captured-stream fallback on a DRM page = the encrypted tracks: not offered
  const nVideos = drm && !vi.direct ? 0 : vi.items.length;
  setCount("imgs", scan.images.length);
  setCount("vids", nVideos);
  setCount("docs", scan.docs.length);
  jlog("debug", "scan", `${scan.host}: ${scan.images.length} image(s), ${nVideos} video(s) (direct=${vi.direct} stream=${vi.stream} drm=${drm} blob=${blobs}), ${scan.docs.length} document(s), ${scan.captured.length} captured request(s)${eng ? ", engine page: " + eng.site : ""}`);
  const st = [];
  if (drm) st.push(`${drm} protected video${drm > 1 ? "s" : ""} (DRM: Widevine/PlayReady): encrypted, no tool can save ${drm > 1 ? "them" : "it"}.`);
  if (blobs && !scan.captured.length) st.push(`${blobs} streamed player${blobs > 1 ? "s" : ""}: press play so the stream can be captured.`);
  if (!drm && vi.stream && !eng) st.push(vi.items.length ? `${vi.items.length} streamed video${vi.items.length > 1 ? "s" : ""} (HLS/DASH): segments are joined into one .mp4.` : "Streamed video: its playlist could not be read (reload and press play).");
  if (st.length && curTab === "tabDl" && !$("status").textContent) say(st.join("\n"), "warn");
}
const NOUN = { imgs: "images", vids: "videos", docs: "documents", coll: "collected pictures" };
let blocked = false;   // no native host: nothing can be written, so every save control is off (the alert says why)
const SAVE_BTNS = ["site", "siteAudio", "spGo", "sitePdf", "siteMd"];
function setCount(id, n) {
  const b = $(id);
  b.querySelector(".n").textContent = String(n);
  b.dataset.n = String(n);
  b.disabled = !n || blocked;
  b.setAttribute("aria-label", n ? `Save all ${n} ${NOUN[id]}` : `No ${NOUN[id]} on this page`);
}

// ---------- the run dock ----------
// One view of what is running, wherever the popup is: n of m, the tally (a cell per file: solid saved,
// hatched failed, pulsing in flight, hollow waiting, struck when stopped), what is in flight, every failure
// with its reason, and Stop. After the run it keeps the verdict until dismissed.
const MB = (n) => (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB";
const PLURAL = { image: "images", video: "videos", doc: "documents", song: "songs", site: "sites", file: "files" };
const noun = (kind, n) => n === 1 ? (kind === "doc" ? "document" : kind || "file") : PLURAL[kind] || (kind || "file") + "s";
// Every failure is named with its reason (the worker builds "song 6 Floaty: YouTube refused the download ...");
// a run where nothing was saved ends on an error line, never a quiet "0 saved".
function runModel(pr) {
  const saved = pr.done - pr.failed, errs = pr.errors || [];
  const m = { tone: "", meta: [], errs: [], now: "", head: "", groups: { items: [], more: 0 } };
  if (pr.crawl && pr.active) {
    const of = Math.max(pr.crawl.m, pr.crawl.n);
    m.head = pr.stopping ? "Stopping…" : `Page ${pr.crawl.n} of ${of}`;
    m.meta = [pr.crawl.url];
    m.frac = pr.crawl.n / of;
    return m;
  }
  if (pr.active) {
    m.head = pr.stopping ? `Stopping… ${saved} saved so far` : `${pr.done} of ${pr.total} ${noun(pr.kind, pr.total)}`;
    if (pr.now?.length && !pr.stopping) m.now = "Now: " + pr.now.slice(0, 4).join(", ");
    // a stream / sized file in flight reports how far along it is (pr.part, the sum of the in-flight fractions): the bar then moves
    // smoothly instead of stepping per finished file. Small runs only; a long list keeps its per-file cells.
    if (pr.part > 0 && pr.total && pr.total <= 8 && !pr.stopping) {
      m.frac = Math.min(0.99, (pr.done + pr.part) / pr.total);
      m.head += ` · ${Math.floor(m.frac * 100)}%`;
    }
  } else if (pr.stopped) {
    m.head = `Stopped — ${saved} saved, ${pr.notStarted || 0} not started`;
    if (pr.cut) m.meta.push(`${pr.cut} cut off, partial file${pr.cut > 1 ? "s" : ""} removed`);
  } else if (pr.failed && !saved) {
    m.tone = "bad";
    m.head = `Nothing was saved: ${pr.failed === 1 ? "the " + noun(pr.kind, 1) + " failed" : "all " + pr.failed + " " + noun(pr.kind, pr.failed) + " failed"}`;
  } else if (pr.failed > saved) {
    m.tone = "warn";
    m.head = `${pr.failed} of ${pr.done} ${noun(pr.kind, pr.done)} failed`;
  } else {
    m.head = `${saved} ${noun(pr.kind, saved)} saved`;
    if (pr.failed) m.tone = "warn";
  }
  const cutNote = m.meta.splice(0);
  m.meta.push(folderName(pr.where));
  if (pr.bytes) m.meta.push(MB(pr.bytes));
  if (pr.failed) m.meta.push(`${pr.failed} failed`);
  m.meta.push(...cutNote);
  m.errs = errs.slice(-6);
  if (errs.length > 6) m.more = `${errs.length - 6} earlier failure${errs.length - 6 > 1 ? "s" : ""} in the log (Tools → Open log)`;
  if (!pr.failed && !errs.length && pr.error && !pr.active) m.note = pr.error;   // saved, with a caveat (no ffmpeg: tracks kept separate)
  m.groups = groupFails(pr);
  return m;
}
// The dock groups failures that share a reason: "8 songs: YouTube refused the download …" instead of eight lines of
// the same words; three or fewer keep their own lines. Each group names who failed (the first few) in its tooltip.
function groupFails(pr) {
  const fails = pr.fails?.length ? pr.fails : (pr.errors || []).map((e) => ({ who: "", why: e }));
  const by = new Map();
  for (const f of fails) { if (!by.has(f.why)) by.set(f.why, []); by.get(f.why).push(f.who); }
  const out = [];
  for (const [why, who] of by) {
    if (who.length <= 3 || !who[0]) for (const w of who) out.push({ text: w ? `${w}: ${why}` : why });
    else out.push({ count: `${who.length} ${noun(pr.kind, who.length)}`, text: why, title: who.slice(0, 12).join("\n") + (who.length > 12 ? `\n… ${who.length - 12} more` : "") });
  }
  const older = Math.max(0, (pr.failed || 0) - fails.length);   // the worker keeps the last 100
  return { items: out.slice(-6), more: out.length - Math.min(out.length, 6) + (older ? 1 : 0), older };
}
// the same, as plain lines (the log, tests, screen readers): head, failures, what is in flight
function progressText(pr) {
  const m = runModel(pr);
  return [m.head, ...(m.more ? ["… " + m.more] : []), ...m.errs.map((e) => "✗ " + e), ...(m.note ? [m.note] : []), ...(m.now ? [m.now] : [])].join("\n");
}
let dismissed = false, lastSaid = "";
function cellStates(pr) {
  const saved = Math.max(0, pr.done - pr.failed), failed = pr.failed || 0;
  const running = pr.active ? Math.min(pr.now?.length || 0, pr.total - pr.done) : 0;
  const rest = Math.max(0, pr.total - saved - failed - running);
  return [["s", saved], ["f", failed], ["a", running], [pr.stopped ? "x" : "", rest]];
}
function drawBar(pr, frac) {
  const bar = $("bar"), fill = $("fill");
  bar.setAttribute("aria-valuemax", String(pr.crawl && pr.active ? Math.max(pr.crawl.m, pr.crawl.n) : pr.total));
  bar.setAttribute("aria-valuenow", String(pr.crawl && pr.active ? pr.crawl.n : pr.done));
  const states = cellStates(pr);
  if (frac != null || pr.total > 120) {               // a crawl, or too many files for cells: one proportional bar
    bar.classList.add("meter");
    for (const c of [...bar.querySelectorAll("i")]) c.remove();
    if (frac != null) { fill.style.width = Math.round(frac * 100) + "%"; fill.innerHTML = '<b class="s" style="width:100%"></b>'; return; }
    const done = pr.done + (pr.active ? 0 : 0);
    fill.style.width = Math.round((pr.total ? (done + states[2][1]) / pr.total : 0) * 100) + "%";
    fill.innerHTML = states.slice(0, 3).filter(([, n]) => n).map(([k, n]) => `<b class="${k}" style="width:${(n / Math.max(1, done + states[2][1])) * 100}%"></b>`).join("");
    return;
  }
  bar.classList.remove("meter");
  fill.style.width = Math.round((pr.total ? pr.done / pr.total : 0) * 100) + "%";
  const want = states.flatMap(([k, n]) => Array(n).fill(k));
  const cells = [...bar.querySelectorAll("i")];
  for (let i = 0; i < want.length; i++) {
    let c = cells[i];
    if (!c) { c = document.createElement("i"); bar.append(c); }
    if (c.className !== want[i]) c.className = want[i];   // only a changed cell re-animates
  }
  for (let i = want.length; i < cells.length; i++) cells[i].remove();
}
function showProgress(pr) {
  if (!pr || !pr.total) return;
  if (pr.active) dismissed = false;
  if (dismissed) return;
  const m = runModel(pr);
  $("run").hidden = false;
  $("runHead").textContent = m.head;
  $("runHead").className = m.tone;
  $("runMeta").replaceChildren(...m.meta.map((t) => Object.assign(document.createElement("span"), { textContent: t })));
  $("runNow").textContent = m.now; $("runNow").hidden = !m.now;
  const lis = [], g = m.groups;
  if (g.more) lis.push(Object.assign(document.createElement("li"), { className: "more", textContent: "… more failures in the log (Tools → Open log)" }));
  for (const it of g.items) {
    const li = document.createElement("li");
    if (it.count) { const b = document.createElement("b"); b.textContent = it.count + ": "; li.append(b); li.title = it.title; }
    li.append(document.createTextNode(it.text));
    lis.push(li);
  }
  if (m.note) lis.push(Object.assign(document.createElement("li"), { className: "note", textContent: m.note }));
  $("runErrs").replaceChildren(...lis); $("runErrs").hidden = !lis.length;
  drawBar(pr, m.frac);
  $("stop").hidden = !pr.active; $("stop").disabled = !!pr.stopping;
  $("runClose").hidden = !!pr.active;
  // the screen reader hears the start, each quarter, and the verdict — not every file
  const q = pr.active ? (pr.crawl ? "crawl" : Math.floor((pr.done / pr.total) * 4)) : "end";
  const key = `${pr.total}|${q}|${pr.stopping ? 1 : 0}`;
  if (key !== lastSaid) { lastSaid = key; $("runSay").textContent = m.head; }
}
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "progress") showProgress(m); });
// poll while a batch runs: if the worker restarted mid-batch its progress is gone, and the dock must say so
// rather than freeze on the last message it got
let lastActive = false;
async function pollProgress() {
  const pr = await chrome.runtime.sendMessage({ type: "getProgress" }).catch(() => null);
  if (pr?.total) showProgress(pr);
  else if (lastActive) {
    $("run").hidden = false; $("stop").hidden = true; $("runClose").hidden = false;
    $("runHead").textContent = "Download stopped: the extension's worker restarted"; $("runHead").className = "bad";
    $("runMeta").textContent = "Run it again; files already saved are skipped."; $("runErrs").replaceChildren(); $("runNow").textContent = "";
    jlog("error", "progress", "the worker restarted mid-batch: progress lost");
  }
  lastActive = !!pr?.active;
  if (lastActive) setTimeout(pollProgress, 1000);
}
pollProgress();
// Stop: the worker aborts what is in flight, starts nothing new, has the host kill engine processes and remove
// partial files; finished files stay. The dock says "Stopping…" until the last job has let go.
$("stop").onclick = async () => {
  $("stop").disabled = true;
  jlog("info", "click", "Stop");
  const r = await chrome.runtime.sendMessage({ type: "stop" }).catch(() => null);
  if (!r?.ok) $("stop").disabled = false;
};
$("runClose").onclick = () => { dismissed = true; $("run").hidden = true; chrome.runtime.sendMessage({ type: "clearProgress" }).catch(() => {}); };

// the "Save from this page" rows: reply comes back once the batch is queued; the dock does the rest
const batches = { imgs: ["image", () => scan.images], vids: ["video", () => vi.items], docs: ["doc", () => scan.docs.map((d) => ({ url: d.url }))] };
for (const [id, [kind, items]] of Object.entries(batches)) $(id).onclick = async () => {
  say(`Queuing ${noun(kind, 2)} for ${folderName(destPath)}…`);
  jlog("info", "click", `Download all ${kind}s`, { items: items().length, dest: destPath || "Downloads" });
  const r = await chrome.runtime.sendMessage({ type: "download", items: items(), kind, tabId: tab?.id, dest: destPath, prefix: $("prefix").value.trim() });
  say(r?.queued ? "" : "Nothing new to save" + (r?.skipped ? ` (${r.skipped} already saved before)` : "") + ".");
};

// the engine buttons: the page URL goes to the host, which runs the site's downloader
for (const [id, mode] of [["site", "video"], ["siteAudio", "audio"]]) $(id).onclick = async () => {
  say(mode === "audio" ? "Downloading the audio, then detecting BPM and key…" : "Downloading through the site's player…");
  jlog("info", "click", `Download from this page (${mode})`, { url: tab.url });
  const r = await chrome.runtime.sendMessage({ type: "download", items: [{ url: tab.url, engine: true, mode }], kind: "video", dest: destPath, prefix: "" });
  if (!r?.queued) say(r?.skipped ? "Already saved before (Tools → forget saved to redo it)." : "Nothing to save.");
};

const crawlFollow = () => document.querySelector("input[name=crawl]:checked").value === "site";
function showCrawl() { $("sitePages").disabled = !crawlFollow(); }
for (const r of document.querySelectorAll("input[name=crawl]")) r.onchange = () => { chrome.storage.sync.set({ crawl: r.value }); showCrawl(); };
// whole-site buttons: crawl same-origin pages breadth-first (capped) and save one merged PDF or Markdown file
for (const [id, mode] of [["sitePdf", "pdf"], ["siteMd", "md"]]) $(id).onclick = async () => {
  const follow = crawlFollow(), maxPages = Math.max(1, parseInt($("sitePages").value, 10) || 50);
  say(follow ? `Crawling the site (at most ${maxPages} pages)…` : "Saving this page…");
  jlog("info", "click", `Save ${follow ? "site" : "page"} as ${mode}`, { url: tab.url, maxPages });
  const r = await chrome.runtime.sendMessage({ type: "download", items: [{ url: tab.url, site: true, mode, maxPages, follow }], kind: "site", dest: destPath, prefix: "" });
  say(r?.queued ? "" : r?.skipped ? "Already saved before (Tools → forget saved to redo it)." : "Nothing to save.");
};

// collected-as-you-scroll list: lives in the background (session storage), count updates live
function showCollected(n) { setCount("coll", n); $("collClear").hidden = !n; }
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "collected") showCollected(m.n); });
chrome.runtime.sendMessage({ type: "getCollected" }).then((l) => showCollected((l || []).length)).catch(() => {});
$("coll").onclick = async () => {
  say(`Queuing the collected pictures for ${folderName(destPath)}…`);
  jlog("info", "click", "Download collected");
  const r = await chrome.runtime.sendMessage({ type: "downloadCollected", dest: destPath, prefix: $("prefix").value.trim() });
  say(r?.queued ? "" : "Nothing new to save" + (r?.skipped ? ` (${r.skipped} already saved before)` : "") + ".");
};
$("collClear").onclick = () => chrome.runtime.sendMessage({ type: "clearCollected" });

// the two disclosures say what is on without being opened
function showSums() {
  const b = [$("collect").checked && "collecting", $("autoVideos").checked && "auto-download", $("showButton").checked && "video buttons"].filter(Boolean);
  $("browseSum").textContent = b.length ? b.join(", ") : "off";
  const f = [$("strip").checked && "strip metadata", $("webp").value && `WebP → ${$("webp").value.toUpperCase().replace("JPG", "JPEG")}`].filter(Boolean);
  $("finishSum").textContent = f.length ? f.join(", ") : "off";
}
for (const d of ["browseBox", "finishBox"]) {
  try { $(d).open = localStorage.getItem(d) === "1"; } catch {}
  $(d).ontoggle = () => { try { localStorage.setItem(d, $(d).open ? "1" : "0"); } catch {} };
}
chrome.storage.sync.get({ autoVideos: false, showButton: true, collect: false, strip: false, webp: "", dest: "", prefix: "", sitePages: 50, crawl: "page" }, (s) => {
  for (const r of document.querySelectorAll("input[name=crawl]")) r.checked = r.value === s.crawl;
  showCrawl();
  $("autoVideos").checked = s.autoVideos;
  $("collect").checked = s.collect;
  $("showButton").checked = s.showButton;
  $("strip").checked = s.strip; $("webp").value = s.webp === true ? "jpg" : s.webp || "";
  destPath = s.dest; showDest();
  $("prefix").value = s.prefix;
  $("sitePages").value = s.sitePages || 50;
  showSums();
});
$("prefix").oninput = () => chrome.storage.sync.set({ prefix: $("prefix").value.trim() });
$("sitePages").oninput = () => chrome.storage.sync.set({ sitePages: Math.max(1, parseInt($("sitePages").value, 10) || 50) });
// Finder picker runs in the native host; the popup closes when the dialog takes focus, so the
// background stores the result and the reopened popup shows it
// one Finder dialog at a time: the buttons are off while it's up (the popup reopens with it still open)
function pickerState(open) {
  for (const id of ["dest", "rnPick", "rnDest", "spLibAdd"]) $(id).disabled = open || (id === "rnDest" && !$("rnMove").checked);
  if (open) say("Finish the open Finder window first (or cancel it).", "warn");
}
chrome.runtime.sendMessage({ type: "pickerOpen" }).then(pickerState).catch(() => {});
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "picker") pickerState(m.open); });
$("dest").onclick = () => { pickerState(true); chrome.runtime.sendMessage({ type: "chooseDir" }); };
$("destClear").onclick = async () => { await chrome.runtime.sendMessage({ type: "clearDir" }); destPath = ""; showDest(); };
chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.dest) { destPath = ch.dest.newValue || ""; showDest(); } });
for (const k of ["autoVideos", "showButton", "collect", "strip"]) $(k).onchange = () => { chrome.storage.sync.set({ [k]: $(k).checked }); showSums(); };
$("webp").onchange = () => { chrome.storage.sync.set({ webp: $("webp").value }); showSums(); };
// "Save audio as MP3" lives twice (the music-list card and the video-link card); both are the one `mp3` key
const mp3Boxes = [...document.querySelectorAll("input.mp3")];
chrome.storage.sync.get({ mp3: false }, (s) => { for (const b of mp3Boxes) b.checked = !!s.mp3; });
for (const b of mp3Boxes) b.onchange = () => { for (const o of mp3Boxes) o.checked = b.checked; chrome.storage.sync.set({ mp3: b.checked }); };
chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.mp3) for (const b of mp3Boxes) b.checked = !!ch.mp3.newValue; });

// ---------- views: a tablist (arrow keys move, Home/End jump) ----------
const tabIds = Object.keys(PANES);
function showTab(id, focus) {
  curTab = id;
  for (const t of tabIds) {
    const on = t === id;
    $(t).classList.toggle("on", on); $(t).setAttribute("aria-selected", String(on)); $(t).tabIndex = on ? 0 : -1;
    $(PANES[t][0]).hidden = !on;
  }
  if (focus) $(id).focus();
  try { localStorage.setItem("tab", id); } catch {}
}
for (const t of tabIds) {
  $(t).onclick = () => showTab(t);
  $(t).onkeydown = (e) => {
    const i = tabIds.indexOf(t), n = tabIds.length;
    const to = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
    if (to !== undefined) { e.preventDefault(); showTab(tabIds[to], true); }
  };
}
try { const t = localStorage.getItem("tab"); if (t in PANES) showTab(t); } catch {}

// ---------- rename + clean-up (Tools) ----------
// Selected files live in the background (the popup closes when Finder opens); one Finder window at a time
// (pickerState above disables the pickers while one is up, and the background refuses a second call).
let rnDest = "", rnFiles = [];
function showRename() {
  const n = rnFiles.length, move = $("rnMove").checked;
  $("rnFiles").textContent = n ? `${n} file${n > 1 ? "s" : ""} in ${folderName(rnFiles[0].path.slice(0, rnFiles[0].path.lastIndexOf("/")))}/` : "no files selected";
  $("rnClear").hidden = !n;
  $("rnDestName").textContent = rnDest ? folderName(rnDest) : "Destination";
  $("rnDest").title = rnDest || "choose a folder";
  $("rnDest").disabled = !move;
  $("rnDestClear").hidden = !move || !rnDest;
  for (const r of document.querySelectorAll("input[name=rnMode]")) r.disabled = !move;
  const mode = move ? document.querySelector("input[name=rnMode]:checked").value : "rename";
  const ready = n && $("rnName").value.trim() && (!move || rnDest);
  $("rnGo").disabled = !ready;
  $("rnStrip").disabled = !n; $("rnWebp").disabled = $("rnPng").disabled = !rnFiles.some((f) => /\.webp$/i.test(f.path));
  $("rnStrip").textContent = n ? `Strip metadata (${n})` : "Strip metadata";
  const nw = rnFiles.filter((f) => /\.webp$/i.test(f.path)).length;
  $("rnWebp").textContent = nw ? `WebP → JPEG (${nw})` : "WebP → JPEG"; $("rnPng").textContent = nw ? `WebP → PNG (${nw})` : "WebP → PNG";
  $("rnGo").textContent = mode === "copy" ? `Copy ${n} renamed to ${rnDest ? folderName(rnDest) : "…"}` : mode === "move" ? `Move ${n} renamed to ${rnDest ? folderName(rnDest) : "…"}` : `Rename ${n} file${n === 1 ? "" : "s"}`;
}
async function loadRenameFiles() { rnFiles = await chrome.runtime.sendMessage({ type: "getRenameFiles" }).catch(() => []) || []; showRename(); }
$("rnPick").onclick = () => { pickerState(true); chrome.runtime.sendMessage({ type: "chooseFiles" }); };
$("rnClear").onclick = async () => { await chrome.runtime.sendMessage({ type: "clearRenameFiles" }); loadRenameFiles(); };
$("rnDest").onclick = () => { pickerState(true); chrome.runtime.sendMessage({ type: "chooseRenameDest" }); };
$("rnDestClear").onclick = async () => { await chrome.storage.sync.set({ renameDest: "" }); };
$("rnMove").onchange = () => { chrome.storage.sync.set({ rnMove: $("rnMove").checked }); showRename(); };
$("rnName").oninput = () => { chrome.storage.sync.set({ rnName: $("rnName").value.trim() }); showRename(); };
for (const r of document.querySelectorAll("input[name=rnMode]")) r.onchange = () => { chrome.storage.sync.set({ rnMode: r.value }); showRename(); };
$("rnGo").onclick = async () => {
  const move = $("rnMove").checked, mode = move ? document.querySelector("input[name=rnMode]:checked").value : "rename";
  $("rnGo").disabled = true; say("Renaming…");
  const r = await chrome.runtime.sendMessage({ type: "rename", name: $("rnName").value.trim(), dest: move ? rnDest : "", mode });
  if (!r?.ok) { say(r?.output || "Rename failed.", "warn"); showRename(); return; }
  const verb = mode === "copy" ? "copied" : mode === "move" ? "moved" : "renamed";
  say(`${r.done} ${verb}: ${r.ops[0].name}${r.done > 1 ? " … " + r.ops[r.ops.length - 1].name : ""}` + (r.failed ? `\n${r.failed} failed: ${r.error}` : ""), r.failed ? "warn" : "");
  loadRenameFiles();
};
// the finishing steps on the selected files, in place (no rename)
for (const [id, opt, val] of [["rnStrip", "strip", true], ["rnWebp", "webp", "jpg"], ["rnPng", "webp", "png"]]) $(id).onclick = async () => {
  $(id).disabled = true; say(opt === "strip" ? "Stripping metadata…" : "Converting…");
  const r = await chrome.runtime.sendMessage({ type: "finish", paths: rnFiles.map((f) => f.path), [opt]: val });
  if (!r?.ok) { say(r?.output || "Failed.", "warn"); showRename(); return; }
  const bad = r.results.filter((x) => !x.ok), done = r.results.length - bad.length;
  say(`${done} file${done === 1 ? "" : "s"} ${opt === "strip" ? "stripped" : "converted"}` + (bad.length ? `\n${bad.length} failed: ${bad[0].output}` : ""), bad.length ? "warn" : "");
  loadRenameFiles();
};
chrome.storage.sync.get({ renameDest: "", rnMove: false, rnMode: "move", rnName: "" }, (s) => {
  rnDest = s.renameDest; $("rnMove").checked = s.rnMove; $("rnName").value = s.rnName;
  for (const r of document.querySelectorAll("input[name=rnMode]")) r.checked = r.value === s.rnMode;
  showRename();
});
chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.renameDest) { rnDest = ch.renameDest.newValue || ""; showRename(); } if (area === "session" && ch.renameFiles) loadRenameFiles(); });
loadRenameFiles();

// ---------- dependencies + the alert above the views ----------
// No host, or a missing tool, blocks saving: it is said at the top of the popup, in the path of the task,
// with the fix next to it; the Tools view has the per-tool detail.
const DEP_LABEL = { "yt-dlp": "yt-dlp", ffmpeg: "ffmpeg", "gallery-dl": "gallery-dl", exiftool: "exiftool", pypdf: "pypdf", analyzer: "BPM/key analyzer" };
const hintText = (h) => { const v = Object.values(h || {}); return v.length ? "\n" + v.join("\n") : ""; };
// the installer's own "no prebuilt package for this macOS - install X by hand: ..." lines travel in the setup output
const byHand = (out) => { const l = String(out || "").split("\n").filter((x) => /by hand|Homebrew could not|⚠|❌/.test(x)); return l.length ? "\n" + l.join("\n") : ""; };
// one line per missing tool: why it is not there and how to get it by hand (the tooltip alone is never seen)
const depsWhyText = (deps, hints) => Object.entries(deps || {}).filter(([, v]) => !v.present)
  .map(([k]) => (hints?.[k] ? `✗ ${DEP_LABEL[k] || k}: ${hints[k]}` : "")).concat(hints?.venv ? [hints.venv] : []).filter(Boolean).join("\n");
function showAlert(kind, title, body, install) {
  $("alert").hidden = !kind;
  if (!kind) return;
  $("alert").className = "alert" + (kind === "bad" ? " bad" : "");
  const b = document.createElement("b"); b.textContent = title;
  $("alertText").replaceChildren(b, document.createTextNode(body));
  $("alertInstall").hidden = !install;
}
async function showDeps() {
  const r = await chrome.runtime.sendMessage({ type: "deps" }).catch(() => null);
  if (!r?.ok) {
    const why = r?.output || "native host not reachable — run native/install.sh";
    $("deps").textContent = why; $("depsInstall").hidden = true; $("depsWhy").hidden = true;
    showAlert("bad", "Nothing can be saved yet", /install\.sh/.test(why) ? "The helper app that writes files is not installed. Run native/install.sh once, then reopen this." : why);
    setBlocked(true);
    jlog("warn", "deps", "host not reachable: " + (r?.output || "no answer")); return;
  }
  setBlocked(false);
  const missing = Object.entries(r.deps).filter(([, v]) => !v.present).map(([k]) => k);
  jlog("info", "deps", missing.length ? "missing: " + missing.join(", ") : "all present", { python: r.python });
  $("deps").replaceChildren(...Object.entries(r.deps).map(([k, v]) => Object.assign(document.createElement("span"), { className: "dep " + (v.present ? "ok" : "no"), textContent: DEP_LABEL[k] || k, title: v.present ? "installed" : "missing" })));
  $("deps").title = Object.values(r.hints || {}).join("\n");
  const why = depsWhyText(r.deps, r.hints);
  $("depsWhy").textContent = why; $("depsWhy").hidden = !why;
  $("depsInstall").hidden = !missing.length;
  showAlert(missing.length ? "warn" : "", `${missing.length} tool${missing.length > 1 ? "s" : ""} missing`, `${missing.map((k) => DEP_LABEL[k] || k).join(", ")}: some saves will fail until ${missing.length > 1 ? "they are" : "it is"} installed.`, true);
}
function setBlocked(on) {
  if (blocked === on) return;
  blocked = on;
  for (const id of ["imgs", "vids", "docs", "coll"]) $(id).disabled = on || !Number($(id).dataset.n || 0);
  for (const id of SAVE_BTNS) $(id).disabled = on;
}
async function installDeps() {
  $("depsInstall").disabled = $("alertInstall").disabled = true;
  jlog("info", "click", "Install missing dependencies");
  say("Installing missing dependencies… (this can take a few minutes)");
  const r = await chrome.runtime.sendMessage({ type: "setup" }).catch(() => null);
  $("depsInstall").disabled = $("alertInstall").disabled = false;
  say(r?.ok
    ? (r.missing?.length ? `Installed what it could. Still missing: ${r.missing.join(", ")}${r.python ? ` (python: ${r.python})` : ""}${hintText(r.hints)}${byHand(r.output)}` : "All dependencies installed.")
    : (r?.output || "Install failed."), r?.ok && !r.missing?.length ? "" : "warn");
  showDeps();
}
$("depsInstall").onclick = installDeps;
$("alertInstall").onclick = installDeps;
showDeps();
// an install/update auto-check may have run before this popup ever opened — show what it found, once
chrome.runtime.sendMessage({ type: "getSetupResult" }).then((r) => {
  if (!r) return;
  say(r.ok
    ? (r.missing.length ? `Dependencies installed. Still missing: ${r.missing.join(", ")}${r.python ? ` (python: ${r.python})` : ""}${hintText(r.hints)}${byHand(r.output)}` : "Dependencies installed.")
    : (r.output || "Dependency install failed."), r.ok && !r.missing.length ? "" : "warn");
}).catch(() => {});

// ---------- log ----------
// One developer log holds the host, the engine and this extension (native/jlog.py). Open it in TextEdit, or copy its tail.
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {}
  const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch {}
  ta.remove(); return ok;
}
$("logOpen").onclick = async () => {
  const r = await chrome.runtime.sendMessage({ type: "openLog" }).catch(() => null);
  say(r?.ok ? `Opened ${r.path} in TextEdit.` : `Couldn't open the log: ${r?.output || "native host not reachable — run native/install.sh"}`, r?.ok ? "" : "warn");
};
$("logCopy").onclick = async () => {
  const r = await chrome.runtime.sendMessage({ type: "tailLog", lines: 200 }).catch(() => null);
  if (!r?.ok) { say(`Couldn't read the log: ${r?.output || "native host not reachable — run native/install.sh"}`, "warn"); return; }
  say((await copyText(r.text)) ? `Copied the last ${r.lines} lines of the log.` : "The browser refused the copy; use Open log instead.");
};

// saved-URL memory: skipped while the file is still in its folder; the link wipes it
async function showSaved() { const n = await chrome.runtime.sendMessage({ type: "savedCount" }).catch(() => 0); $("forget").textContent = `forget saved (${n || 0})`; }
$("forget").onclick = async () => { await chrome.runtime.sendMessage({ type: "forgetSaved" }); showSaved(); say("Saved list forgotten: everything counts as new."); };
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "progress") { if (m.active && !lastActive) { lastActive = true; setTimeout(pollProgress, 1000); } if (!m.active) showSaved(); } });
showSaved();

// sidebar: same page in Brave's side panel (stays open across tabs and pages; rescans on every switch/load)
const PANEL = new URLSearchParams(location.search).has("panel");
if (PANEL) {
  document.body.classList.add("panel"); document.documentElement.classList.add("panel");
  chrome.tabs.onActivated.addListener(() => refresh());
  chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete" && id === tab?.id) refresh(); });
} else $("sidebar").onclick = async () => {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  await chrome.sidePanel.open({ windowId: t.windowId });
  window.close();
};

refresh();
