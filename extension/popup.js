const $ = (id) => document.getElementById(id);
let destPath = "", scan = null, tab = null;
const folderName = (path) => (path || "").split("/").filter(Boolean).pop() || "Downloads";
function showDest() {
  $("dest").textContent = "📁 " + folderName(destPath);
  $("dest").title = destPath || "Downloads (click to choose a folder)";
  $("destClear").hidden = !destPath;
}

// {items, direct}: real <video> sources when the page has them (reliable), else whole files the network
// layer captured for the tab — one per file, largest variant (can include DRM segments, so less trusted)
function videoItems() {
  const direct = scan.videos.filter((v) => !v.blob && !v.drm).map((v) => ({ url: v.url, date: v.date }));
  if (direct.length) return { items: direct, direct: true };
  // a manifest or segment in the tab means the player streams; the whole files around it are other clips
  if (scan.captured.some((c) => !c.whole)) return { items: [], direct: false };
  const best = new Map();
  for (const c of scan.captured.filter((c) => c.whole)) {
    let k; try { const u = new URL(c.url); k = u.origin + u.pathname; } catch { k = c.url; }
    if (!best.has(k) || (c.size || 0) > (best.get(k).size || 0)) best.set(k, c);
  }
  return { items: [...best.values()].map((c) => ({ url: c.url, ctype: c.ctype })), direct: false };
}

async function refresh() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  scan = await chrome.runtime.sendMessage({ type: "scan", tabId: tab.id });
  if (!scan) { $("host").textContent = "can't read this page"; return; }
  $("host").textContent = scan.host;
  const vi = videoItems();
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
  if (!drm && scan.captured.some((c) => !c.whole)) st.push("HLS/DASH stream on page — segments not grabbable here (use yt-dlp)");
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
const batches = { imgs: ["image", () => scan.images], vids: ["video", () => videoItems().items], docs: ["doc", () => scan.docs.map((d) => ({ url: d.url }))] };
for (const [id, [kind, items]] of Object.entries(batches)) $(id).onclick = async () => {
  $("status").textContent = `queuing ${kind}s → ${folderName(destPath)} …`;
  const r = await chrome.runtime.sendMessage({ type: "download", items: items(), kind, dest: destPath, prefix: $("prefix").value.trim() });
  if (!r?.queued) $("status").textContent = "nothing new to save" + (r?.skipped ? ` (${r.skipped} already saved before)` : "");
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

chrome.storage.sync.get({ autoVideos: false, showButton: true, collect: false, dest: "", prefix: "" }, (s) => {
  $("autoVideos").checked = s.autoVideos;
  $("collect").checked = s.collect;
  $("showButton").checked = s.showButton;
  destPath = s.dest; showDest();
  $("prefix").value = s.prefix;
});
$("prefix").oninput = () => chrome.storage.sync.set({ prefix: $("prefix").value.trim() });
// Finder picker runs in the native host; the popup closes when the dialog takes focus, so the
// background stores the result and the reopened popup shows it
// one Finder dialog at a time: the button is off while it's up (the popup reopens with it still open)
function pickerState(open) { $("dest").disabled = open; if (open) $("status").textContent = "choose a folder in the Finder window… (or Cancel it)"; }
chrome.runtime.sendMessage({ type: "pickerOpen" }).then(pickerState).catch(() => {});
chrome.runtime.onMessage.addListener((m) => { if (m?.type === "picker") pickerState(m.open); });
$("dest").onclick = () => { pickerState(true); chrome.runtime.sendMessage({ type: "chooseDir" }); };
$("destClear").onclick = async () => { await chrome.runtime.sendMessage({ type: "clearDir" }); destPath = ""; showDest(); };
chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.dest) { destPath = ch.dest.newValue || ""; showDest(); } });
for (const k of ["autoVideos", "showButton", "collect"]) $(k).onchange = () => chrome.storage.sync.set({ [k]: $(k).checked });

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
