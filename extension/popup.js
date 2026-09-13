const $ = (id) => document.getElementById(id);
let destPath = "", scan = null, tab = null;
// pages the engine downloads whole (the site's player API, not the page's files): YouTube, Instagram, Twitter/X
const ENGINE_RE = /^https?:\/\/(?:[\w-]+\.)?(youtube\.com|youtu\.be|instagram\.com|twitter\.com|x\.com)\//i;
function engineOf(url) {
  const m = ENGINE_RE.exec(url || "");
  if (!m) return null;
  const path = (() => { try { return new URL(url).pathname; } catch { return "/"; } })();
  const seg = path.split("/").filter(Boolean);
  if (/youtu/.test(m[1])) return { site: "youtube", what: /\/(watch|shorts\/|live\/)|youtu\.be/.test(url) ? "this video (MP4)" : "" };
  if (/instagram/.test(m[1])) return { site: "instagram", what: /^(p|reel|reels|tv)$/.test(seg[0] || "") ? "this post" : seg.length === 1 ? `everything from @${seg[0]}` : "" };
  return { site: "twitter", what: seg[1] === "status" ? "this tweet's media" : seg.length && !/^(home|explore|search|settings|i|messages|notifications)$/.test(seg[0]) ? `all media from @${seg[0]}` : "" };
}
const folderName = (path) => (path || "").split("/").filter(Boolean).pop() || "Downloads";
function showDest() {
  $("dest").textContent = "📁 " + folderName(destPath);
  $("dest").title = destPath || "Downloads (click to choose a folder)";
  $("destClear").hidden = !destPath;
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

async function refresh() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  scan = await chrome.runtime.sendMessage({ type: "scan", tabId: tab.id });
  if (!scan) { $("host").textContent = "can't read this page"; return; }
  $("host").textContent = scan.host;
  const eng = engineOf(tab.url);
  $("site").hidden = !eng?.what; $("siteAudio").hidden = eng?.site !== "youtube" || !eng.what;
  if (eng?.what) $("site").textContent = `Download ${eng.what}`;
  const isHttp = /^https?:\/\//i.test(tab.url || "");
  $("sitePdf").hidden = $("siteMd").hidden = !isHttp;
  vi = await videoItems();
  const drm = scan.videos.filter((v) => v.drm).length;
  const blobs = scan.videos.filter((v) => v.blob && !v.drm).length;
  // captured-stream fallback on a DRM page = the encrypted tracks: not offered
  const nVideos = drm && !vi.direct ? 0 : vi.items.length;
  setCount("imgs", "images", scan.images.length);
  setCount("vids", "videos", nVideos);
  setCount("docs", "documents", scan.docs.length);
  const st = [];
  if (drm) st.push(`${drm} protected video${drm > 1 ? "s" : ""} (DRM: Widevine/PlayReady) — encrypted, no tool can save ${drm > 1 ? "them" : "it"}`);
  if (blobs && !scan.captured.length) st.push(`${blobs} streamed player${blobs > 1 ? "s" : ""} — press play so the stream can be captured`);
  if (!drm && vi.stream && !eng) st.push(vi.items.length ? `${vi.items.length} streamed video${vi.items.length > 1 ? "s" : ""} (HLS/DASH) — segments are joined into one .mp4 by ffmpeg` : "streamed video — its playlist could not be read (reload and press play)");
  $("status").innerHTML = st.map((s) => `<div class="warn">${s}</div>`).join("");
}
function setCount(id, label, n) { $(id).textContent = `Download all ${label} (${n})`; $(id).disabled = !n; }

// ---------- progress bar ----------
const MB = (n) => (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB";
function showProgress(pr) {
  if (!pr || !pr.total) return;
  $("bar").hidden = false;
  $("fill").style.width = Math.round((pr.done / pr.total) * 100) + "%";
  const fails = pr.failed ? ` (${pr.failed} failed)` : "";
  const head = pr.active ? `${pr.done} / ${pr.total}` : `${pr.done - pr.failed}`;
  $("status").textContent = `${head} ${pr.kind}s saved → ${folderName(pr.where)}${fails} · ${MB(pr.bytes)}` + (pr.error ? `\n${pr.error}` : "");
}
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "progress") showProgress(m); });
// poll while a batch runs: if the worker restarted mid-batch its progress is gone, and the bar must say so
// rather than freeze on the last message it got
let lastActive = false;
async function pollProgress() {
  const pr = await chrome.runtime.sendMessage({ type: "getProgress" }).catch(() => null);
  if (pr?.total) showProgress(pr);
  else if (lastActive) { $("status").textContent = "download stopped — the extension's worker restarted; run the batch again (saved files are skipped)"; $("bar").hidden = true; }
  lastActive = !!pr?.active;
  if (lastActive) setTimeout(pollProgress, 1000);
}
pollProgress();

// the three "Download all" buttons: reply comes back once the batch is queued; the bar does the rest
const batches = { imgs: ["image", () => scan.images], vids: ["video", () => vi.items], docs: ["doc", () => scan.docs.map((d) => ({ url: d.url }))] };
for (const [id, [kind, items]] of Object.entries(batches)) $(id).onclick = async () => {
  $("status").textContent = `queuing ${kind}s → ${folderName(destPath)} …`;
  const r = await chrome.runtime.sendMessage({ type: "download", items: items(), kind, dest: destPath, prefix: $("prefix").value.trim() });
  if (!r?.queued) $("status").textContent = "nothing new to save" + (r?.skipped ? ` (${r.skipped} already saved before)` : "");
};

// the engine buttons: the page URL goes to the host, which runs the site's downloader
for (const [id, mode] of [["site", "video"], ["siteAudio", "audio"]]) $(id).onclick = async () => {
  $("status").textContent = mode === "audio" ? "downloading audio, then detecting BPM + key…" : "downloading through the site's player API…";
  const r = await chrome.runtime.sendMessage({ type: "download", items: [{ url: tab.url, engine: true, mode }], kind: "video", dest: destPath, prefix: "" });
  if (!r?.queued) $("status").textContent = r?.skipped ? "already saved before (forget saved to redo)" : "nothing to save";
};

// whole-site buttons: crawl same-origin pages breadth-first (capped) and save one merged PDF or Markdown file
for (const [id, mode] of [["sitePdf", "pdf"], ["siteMd", "md"]]) $(id).onclick = async () => {
  const maxPages = Math.max(1, parseInt($("sitePages").value, 10) || 50);
  $("status").textContent = `crawling the site (max ${maxPages} pages)…`;
  const r = await chrome.runtime.sendMessage({ type: "download", items: [{ url: tab.url, site: true, mode, maxPages }], kind: "site", dest: destPath, prefix: "" });
  if (!r?.queued) $("status").textContent = r?.skipped ? "already saved before (forget saved to redo)" : "nothing to save";
};

// collected-as-you-scroll list: lives in the background (session storage), count updates live
function showCollected(n) { $("coll").textContent = `Download collected (${n})`; $("coll").disabled = !n; $("collClear").hidden = !n; }
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "collected") showCollected(m.n); });
chrome.runtime.sendMessage({ type: "getCollected" }).then((l) => showCollected((l || []).length)).catch(() => {});
$("coll").onclick = async () => {
  $("status").textContent = `queuing collected images → ${folderName(destPath)} …`;
  const r = await chrome.runtime.sendMessage({ type: "downloadCollected", dest: destPath, prefix: $("prefix").value.trim() });
  if (!r?.queued) $("status").textContent = "nothing new to save" + (r?.skipped ? ` (${r.skipped} already saved before)` : "");
};
$("collClear").onclick = () => chrome.runtime.sendMessage({ type: "clearCollected" });

chrome.storage.sync.get({ autoVideos: false, showButton: true, collect: false, strip: false, webp: "", dest: "", prefix: "", sitePages: 50 }, (s) => {
  $("autoVideos").checked = s.autoVideos;
  $("collect").checked = s.collect;
  $("showButton").checked = s.showButton;
  $("strip").checked = s.strip; $("webp").value = s.webp === true ? "jpg" : s.webp || "";
  destPath = s.dest; showDest();
  $("prefix").value = s.prefix;
  $("sitePages").value = s.sitePages || 50;
});
$("prefix").oninput = () => chrome.storage.sync.set({ prefix: $("prefix").value.trim() });
$("sitePages").oninput = () => chrome.storage.sync.set({ sitePages: Math.max(1, parseInt($("sitePages").value, 10) || 50) });
// Finder picker runs in the native host; the popup closes when the dialog takes focus, so the
// background stores the result and the reopened popup shows it
// one Finder dialog at a time: the button is off while it's up (the popup reopens with it still open)
function pickerState(open) { for (const id of ["dest", "rnPick", "rnDest"]) $(id).disabled = open || (id === "rnDest" && !$("rnMove").checked); if (open) $("status").textContent = "finish the open Finder window first (or Cancel it)"; }
chrome.runtime.sendMessage({ type: "pickerOpen" }).then(pickerState).catch(() => {});
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "picker") pickerState(m.open); });
$("dest").onclick = () => { pickerState(true); chrome.runtime.sendMessage({ type: "chooseDir" }); };
$("destClear").onclick = async () => { await chrome.runtime.sendMessage({ type: "clearDir" }); destPath = ""; showDest(); };
chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.dest) { destPath = ch.dest.newValue || ""; showDest(); } });
for (const k of ["autoVideos", "showButton", "collect", "strip"]) $(k).onchange = () => chrome.storage.sync.set({ [k]: $(k).checked });
$("webp").onchange = () => chrome.storage.sync.set({ webp: $("webp").value });

// ---------- batch rename tab ----------
// Selected files live in the background (the popup closes when Finder opens); one Finder window at a time
// (pickerState above disables both pickers while one is up, and the background refuses a second call).
let rnDest = "", rnFiles = [];
const tabs = { tabDl: "paneDl", tabRn: "paneRn" };
function showTab(id) {
  for (const [t, pane] of Object.entries(tabs)) { $(t).classList.toggle("on", t === id); $(pane).hidden = t !== id; }
  try { localStorage.setItem("tab", id); } catch {}
  $("status").textContent = ""; $("bar").hidden = true;
}
for (const t of Object.keys(tabs)) $(t).onclick = () => showTab(t);
try { if (localStorage.getItem("tab") === "tabRn") showTab("tabRn"); } catch {}
function showRename() {
  const n = rnFiles.length, move = $("rnMove").checked;
  $("rnFiles").textContent = n ? `${n} file${n > 1 ? "s" : ""} selected in ${folderName(rnFiles[0].path.slice(0, rnFiles[0].path.lastIndexOf("/")))}/` : "no files selected";
  $("rnClear").hidden = !n;
  $("rnDest").textContent = "📁 " + (rnDest ? folderName(rnDest) : "destination");
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
  $("rnGo").textContent = mode === "copy" ? `Copy ${n} renamed → ${rnDest ? folderName(rnDest) : "…"}` : mode === "move" ? `Move ${n} renamed → ${rnDest ? folderName(rnDest) : "…"}` : `Rename ${n} file${n === 1 ? "" : "s"}`;
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
  $("rnGo").disabled = true; $("status").textContent = "renaming…";
  const r = await chrome.runtime.sendMessage({ type: "rename", name: $("rnName").value.trim(), dest: move ? rnDest : "", mode });
  if (!r?.ok) { $("status").innerHTML = `<div class="warn">${r?.output || "rename failed"}</div>`; showRename(); return; }
  const verb = mode === "copy" ? "copied" : mode === "move" ? "moved" : "renamed";
  $("status").textContent = `${r.done} ${verb} → ${r.ops[0].name}${r.done > 1 ? " … " + r.ops[r.ops.length - 1].name : ""}` + (r.failed ? `\n${r.failed} failed: ${r.error}` : "");
  loadRenameFiles();
};
// the finishing steps on the selected files, in place (no rename)
for (const [id, opt, val] of [["rnStrip", "strip", true], ["rnWebp", "webp", "jpg"], ["rnPng", "webp", "png"]]) $(id).onclick = async () => {
  $(id).disabled = true; $("status").textContent = opt === "strip" ? "stripping metadata…" : "converting…";
  const r = await chrome.runtime.sendMessage({ type: "finish", paths: rnFiles.map((f) => f.path), [opt]: val });
  if (!r?.ok) { $("status").innerHTML = `<div class="warn">${r?.output || "failed"}</div>`; showRename(); return; }
  const bad = r.results.filter((x) => !x.ok), done = r.results.length - bad.length;
  $("status").textContent = `${done} file${done === 1 ? "" : "s"} ${opt === "strip" ? "stripped" : "converted"}` + (bad.length ? `\n${bad.length} failed: ${bad[0].output}` : "");
  loadRenameFiles();
};
chrome.storage.sync.get({ renameDest: "", rnMove: false, rnMode: "move", rnName: "" }, (s) => {
  rnDest = s.renameDest; $("rnMove").checked = s.rnMove; $("rnName").value = s.rnName;
  for (const r of document.querySelectorAll("input[name=rnMode]")) r.checked = r.value === s.rnMode;
  showRename();
});
chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.renameDest) { rnDest = ch.renameDest.newValue || ""; showRename(); } if (area === "session" && ch.renameFiles) loadRenameFiles(); });
loadRenameFiles();

// saved-URL memory: skipped while the file is still in its folder; the link wipes it
async function showSaved() { const n = await chrome.runtime.sendMessage({ type: "savedCount" }).catch(() => 0); $("forget").textContent = `forget saved (${n || 0})`; }
$("forget").onclick = async () => { await chrome.runtime.sendMessage({ type: "forgetSaved" }); showSaved(); $("status").textContent = "saved-list forgotten — everything counts as new"; };
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "progress") { if (m.active && !lastActive) { lastActive = true; setTimeout(pollProgress, 1000); } if (!m.active) showSaved(); } });
showSaved();

// sidebar: same page in Brave's side panel (stays open across tabs and pages; rescans on every switch/load)
const PANEL = new URLSearchParams(location.search).has("panel");
if (PANEL) {
  document.body.classList.add("panel");
  chrome.tabs.onActivated.addListener(() => refresh());
  chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete" && id === tab?.id) refresh(); });
} else $("sidebar").onclick = async () => {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  await chrome.sidePanel.open({ windowId: t.windowId });
  window.close();
};

refresh();
