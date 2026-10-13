// Jownloader — service worker.
// Owns: media URL capture per tab (webRequest), filename policy, and saving files through the native host.
//
// Saving never uses Brave's own download path: Brave's default "Ask where to save each file" prompts
// on every extension download, and chrome.downloads can't write outside ~/Downloads. Instead the worker
// fetches each file itself (it shares the browser session, so logged-in sites work) and streams the
// bytes over a native-messaging port to native/jownloader_host.py, which writes them into ~/Downloads
// or the chosen folder. No host → the batch fails loudly with "run native/install.sh".

const NATIVE_HOST = "com.jshriver.jownloader";
const MEDIA_EXT = /\.(mp4|webm|mkv|mov|m4v|m4a|mp3|ogg|oga|ogv|flac|wav|aac|ts|m3u8|mpd)(\?|$)/i;
const EXT_BY_MIME = {
  "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov", "video/x-matroska": "mkv",
  "video/ogg": "ogv", "video/mp2t": "ts", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/webm": "weba",
  "audio/ogg": "ogg", "audio/wav": "wav", "audio/flac": "flac", "audio/aac": "aac",
  "application/vnd.apple.mpegurl": "m3u8", "application/x-mpegurl": "m3u8", "application/dash+xml": "mpd",
  "application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
  "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp", "image/tiff": "tif",
};

// ---------- developer log ----------
// log(level, area, msg, data): a ring buffer of the last LOG_MAX entries (memory + chrome.storage.session under
// its own key, byte-capped, apart from the page-hook manifest cache), forwarded to the native host in batches
// (fire-and-forget, never awaited by a save) so ONE file, ~/Library/Logs/Jownloader/jownloader.log, holds both
// sides. Popup and content scripts send theirs as {type:"log"} messages. Cookie / token values never get in.
const LOG_MAX = 2000, LOG_KEY = "devLog", LOG_SENT_KEY = "devLogSent", LOG_BYTES = 400 * 1024;
const LOG_FLUSH_MS = 2000, LOG_BATCH = 200, LOG_DOWN_MS = 30000;
const logRing = [];                  // oldest first: {n, t, l, a, m, d}
let logLastN = 0, logSent = 0, logSendTimer = 0, logSaveTimer = 0, logBusy = null, logDownUntil = 0;
const LOG_SECRET_KEY = "(?:cookie|authorization|token|passwd|password|secret|api[_-]?key|sessionid|session_id|signature)";
const logRedact = (s) => String(s)
  .replace(new RegExp('("[\\w-]*' + LOG_SECRET_KEY + '[\\w-]*"\\s*:\\s*)"[^"]*"', "gi"), '$1"***"')     // JSON: "authorization":"Bearer x"
  .replace(/\b(cookie|set-cookie|authorization|proxy-authorization)\s*[:=]\s*[^\r\n]*/gi, "$1: ***")
  .replace(/([\w-]*(?:token|passwd|password|secret|api[_-]?key|sessionid|session_id|signature)[\w-]*)(\s*[=:]\s*)([^\s&"'`;,<>]+)/gi, "$1$2***");
const logUrl = (u) => logRedact(String(u || "")).slice(0, 300);
function logAdd(e) {
  logRing.push(e);
  if (logRing.length > LOG_MAX) logRing.splice(0, logRing.length - LOG_MAX);
  if (!logSendTimer) logSendTimer = setTimeout(logFlush, LOG_FLUSH_MS);
  if (!logSaveTimer) logSaveTimer = setTimeout(logSave, 1500);
}
function log(level, area, msg, data) {
  try {
    const e = { n: (logLastN = Math.max(logLastN + 1, Date.now())), t: Date.now(), l: level, a: String(area), m: logRedact(msg).slice(0, 600) };
    if (data !== undefined) {
      let d; try { d = typeof data === "string" ? data : JSON.stringify(data); } catch { d = String(data); }
      e.d = logRedact(d).slice(0, 800);
    }
    logAdd(e);
    if (level === "error") console.error("[jownloader]", area, msg, data ?? ""); else if (level === "warn") console.warn("[jownloader]", area, msg, data ?? "");
  } catch {}
}
// an entry from the popup or a content script: its own timestamp, its area prefixed with where it came from
function logFrom(src, entries) {
  for (const x of Array.isArray(entries) ? entries.slice(0, 200) : []) {
    try {
      const e = { n: (logLastN = Math.max(logLastN + 1, Date.now())), t: Number(x.t) || Date.now(), l: ["debug", "info", "warn", "error"].includes(x.l) ? x.l : "info", a: `${src}.${String(x.a || "ui").slice(0, 16)}`, m: logRedact(x.m ?? "").slice(0, 600) };
      if (x.d !== undefined) { let d; try { d = typeof x.d === "string" ? x.d : JSON.stringify(x.d); } catch { d = String(x.d); } e.d = logRedact(d).slice(0, 800); }
      logAdd(e);
    } catch {}
  }
}
function logSave() {
  logSaveTimer = 0;
  let bytes = 0, keep = logRing.length;
  while (keep > 0 && bytes + (logRing[keep - 1].m.length + (logRing[keep - 1].d || "").length + 80) < LOG_BYTES) { keep--; bytes += logRing[keep].m.length + (logRing[keep].d || "").length + 80; }
  chrome.storage.session.set({ [LOG_KEY]: logRing.slice(keep), [LOG_SENT_KEY]: logSent }).catch(() => {});
}
// forwards what the host has not seen: down the open port when a batch is running, else one sendNativeMessage per
// batch. A host that cannot be reached is left alone for LOG_DOWN_MS; nothing is lost (the ring keeps the entries).
function logFlush() {
  if (logSendTimer) { clearTimeout(logSendTimer); logSendTimer = 0; }
  if (logBusy) return logBusy;
  logBusy = (async () => {
    await 0;                      // logBusy must be assigned before the body can reach its finally (an empty flush ends at once)
    try {
      while (Date.now() >= logDownUntil) {
        const batch = logRing.filter((e) => e.n > logSent).slice(0, LOG_BATCH);
        if (!batch.length) break;
        const entries = batch.map(({ t, l, a, m, d }) => ({ t, l, a, m, d }));
        let ok = false;
        if (hostPort) { try { hostPort.postMessage({ cmd: "log", entries, noreply: true }); ok = true; } catch {} }
        if (!ok) { try { ok = !!(await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "log", entries })); } catch { ok = false; } }
        if (!ok) { logDownUntil = Date.now() + LOG_DOWN_MS; break; }
        logSent = batch[batch.length - 1].n;
      }
    } finally { logBusy = null; }
    if (logRing.some((e) => e.n > logSent) && !logSendTimer) logSendTimer = setTimeout(logFlush, Math.max(LOG_FLUSH_MS, logDownUntil - Date.now()));
    logSave();
  })();
  return logBusy;
}
// everything the host has not seen yet, now (the popup's "Copy last 200 lines" wants the file to be current)
async function logFlushNow() {
  logDownUntil = 0;
  for (let i = 0; i < 20 && logRing.some((e) => e.n > logSent); i++) await logFlush();
}
const logReady = chrome.storage.session.get([LOG_KEY, LOG_SENT_KEY]).then((r) => {
  const prev = Array.isArray(r[LOG_KEY]) ? r[LOG_KEY] : [];
  if (prev.length) {
    logRing.unshift(...prev);
    if (logRing.length > LOG_MAX) logRing.splice(0, logRing.length - LOG_MAX);
    logLastN = Math.max(logLastN, prev[prev.length - 1].n || 0);
  }
  logSent = Math.max(logSent, Number(r[LOG_SENT_KEY]) || 0);
}).catch(() => {});
self.addEventListener("unhandledrejection", (ev) => log("error", "worker", "unhandled promise rejection: " + String(ev.reason?.stack || ev.reason)));
self.addEventListener("error", (ev) => log("error", "worker", `uncaught error: ${ev.message} (${ev.filename}:${ev.lineno})`));
log("info", "worker", `service worker started, extension ${chrome.runtime.getManifest().version}`);

// ---------- media capture (per tab, in session storage) ----------
const mediaKey = (tabId) => `media:${tabId}`;
// a captured URL is a whole file unless it is a playlist/manifest, a segment of one, or a YouTube track
// (googlevideo serves separate video/audio ranges); decided here, once, for every consumer (popup counts,
// ⬇ button, auto-download)
const isWhole = (url, ctype) => !/mpegurl|dash/.test(ctype || "") && !/\.(m3u8|mpd|m4s|ts)(\?|$)/i.test(url) && !/googlevideo\.com\/videoplayback/.test(url);
async function getMedia(tabId) {
  const r = await chrome.storage.session.get(mediaKey(tabId));
  return r[mediaKey(tabId)] || {};
}
const mediaList = async (tabId) => Object.entries(await getMedia(tabId)).map(([url, info]) => ({ url, ...info, whole: isWhole(url, info.ctype) }));
async function addMedia(tabId, url, info) {
  const m = await getMedia(tabId);
  if (m[url]) return;
  m[url] = info;
  await chrome.storage.session.set({ [mediaKey(tabId)]: m });
}
// a new page (load or pushState): captured streams go, and the tab's content scripts drop their per-page state
function clearMedia(tabId) {
  chrome.storage.session.remove(mediaKey(tabId));
  chrome.tabs.sendMessage(tabId, { type: "pageChanged" }).catch(() => {});
  manifestCache.clear();   // a new page's manifest URLs are never the ones just cached
}

// ---------- collected-as-you-scroll images (session storage, survives worker restarts) ----------
async function getCollected() { return (await chrome.storage.session.get("collected")).collected || {}; }
// content scripts report many images in the same instant; read-modify-write is serialized so none is lost
let collectChain = Promise.resolve();
function addCollected(it) {
  const step = async () => {
    const c = await getCollected(), k = canon(it.url);
    if (c[k]) return;
    // sized variants of one picture live in one folder as WIDTHxHEIGHT_name (300x300_… / 1536x2048_…):
    // only the biggest survives, whichever order they arrive in
    const dims = (u) => { const m = /(\d{2,5})x(\d{2,5})[_.-]/.exec(u.split("/").pop() || ""); return m ? +m[1] * +m[2] : 0; };
    const dir = (u) => u.slice(0, u.lastIndexOf("/"));
    const area = dims(k);
    if (area) for (const ok in c) {
      const oa = dims(ok);
      if (!oa || dir(ok) !== dir(k)) continue;
      if (oa >= area) return;
      delete c[ok];
    }
    c[k] = { url: it.url, date: it.date || "", ts: Date.now() };
    await chrome.storage.session.set({ collected: c });
    chrome.runtime.sendMessage({ type: "collected", n: Object.keys(c).length }).catch(() => {});
  };
  return (collectChain = collectChain.then(step, step));   // a failed step never wedges the chain
}
async function clearCollected() {
  await chrome.storage.session.remove("collected");
  chrome.runtime.sendMessage({ type: "collected", n: 0 }).catch(() => {});
}

// Streamed-chunk URLs collapse to one entry for the whole file.
function normalizeMediaUrl(url) {
  try {
    const u = new URL(url);
    for (const k of ["bytestart", "byteend", "range"]) u.searchParams.delete(k);
    return u.toString();
  } catch { return url; }
}

chrome.webRequest.onResponseStarted.addListener(
  (d) => {
    if (d.tabId < 0) return;
    const h = Object.fromEntries((d.responseHeaders || []).map((x) => [x.name.toLowerCase(), x.value || ""]));
    const ctype = (h["content-type"] || "").split(";")[0].trim().toLowerCase();
    const isMedia = ctype.startsWith("video/") || ctype.startsWith("audio/") ||
      ctype.includes("mpegurl") || ctype.includes("dash+xml") || MEDIA_EXT.test(d.url);
    if (!isMedia) return;
    addMedia(d.tabId, normalizeMediaUrl(d.url), { ctype, size: Number(h["content-length"] || 0), ts: Date.now(), frame: d.frameId });
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object"] },
  ["responseHeaders"]
);
chrome.webNavigation.onCommitted.addListener((d) => { if (d.frameId === 0) clearMedia(d.tabId); });
// single-page apps swap content without a load: a pushState is a new page for our purposes too
chrome.webNavigation.onHistoryStateUpdated.addListener((d) => { if (d.frameId === 0) clearMedia(d.tabId); });
chrome.tabs.onRemoved.addListener(clearMedia);
chrome.tabs.onRemoved.addListener((id) => { dropManifestTexts(id).catch(() => {}); });

// ---------- filenames ----------
function sanitize(s) { return s.replace(/[^\w.\-]+/g, "_").replace(/^[_.]+|_+$/g, "").slice(0, 120); }   // never a leading dot (/.zshrc)
// typed by the user: keep spaces, strip only what a filename can't hold
function typed(s) { return s.replace(/[\/\\:*?"<>|]+/g, "").trim().slice(0, 80); }
const hasExt = (name) => /\.[a-z0-9]{2,5}$/i.test(name);
const extForMime = (mime) => EXT_BY_MIME[(mime || "").split(";")[0].trim().toLowerCase()] || "";

function extOf(url, mime) {
  try {
    const p = decodeURIComponent(new URL(url).pathname).replace(/\/+$/, "");   // ".mp4/" → ".mp4"
    const m = /\.([a-z0-9]{2,5})$/i.exec(p);
    if (m) return m[1].toLowerCase();
  } catch {}
  return extForMime(mime);
}

// [date] tokens in the typed name: [yy-mm-dd], [mm-d-yyyy], [jan-d-yyyy], [yy-january-dd] …
// y/yy = 2-digit year, yyy/yyyy = 4-digit, m/mm = month number, d/dd = day,
// jan…dec / mon = short month name, january…december / month = full name; case follows the token.
const MONTHS = ["january","february","march","april","may","june","july","august","september","october","november","december"];
const DATE_TOKEN = new RegExp("(" + MONTHS.join("|") + "|month|" + MONTHS.map((m) => m.slice(0, 3)).join("|") + "|mon|y+|m+|d+)", "gi");
function matchCase(word, like) {
  if (like === like.toUpperCase()) return word.toUpperCase();
  if (like[0] === like[0].toUpperCase()) return word[0].toUpperCase() + word.slice(1);
  return word;
}
function formatDate(pattern, d) {
  const p2 = (n) => String(n).padStart(2, "0");
  return pattern.replace(DATE_TOKEN, (t) => {
    const l = t.toLowerCase();
    if (l === "month" || MONTHS.includes(l)) return matchCase(MONTHS[d.getMonth()], t);
    if (l === "mon" || MONTHS.some((m) => m.slice(0, 3) === l)) return matchCase(MONTHS[d.getMonth()].slice(0, 3), t);
    if (l[0] === "y") return l.length >= 3 ? String(d.getFullYear()) : String(d.getFullYear()).slice(-2);
    if (l[0] === "m") return l.length >= 2 ? p2(d.getMonth() + 1) : String(d.getMonth() + 1);
    return l.length >= 2 ? p2(d.getDate()) : String(d.getDate());
  });
}
// date: "YYYY-MM-DD" (that calendar day, local) or any parsable timestamp; nothing → today
function expandDate(name, date) {
  if (!/\[[^\]]+\]/.test(name)) return name;
  let d;
  const bare = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || "");
  if (bare) d = new Date(+bare[1], +bare[2] - 1, +bare[3]);
  else d = date ? new Date(date) : new Date();
  if (isNaN(d)) d = new Date();
  return name.replace(/\[([^\]]+)\]/g, (_, pat) => formatDate(pat, d));
}

// No prefix typed → the file's own name, untouched. Prefix → prefix_N (with [date] tokens expanded).
// opts: { prefix, n, date }
function filenameFor(url, kind, mime, opts = {}) {
  if (opts.prefix) {
    const ext = extOf(url, mime);
    return `${typed(expandDate(opts.prefix, opts.date))}_${opts.n}` + (ext ? "." + ext : "");
  }
  let base = "";
  try {
    const u = new URL(url);
    base = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "");
  } catch {}
  base = sanitize(base || kind);
  if (!hasExt(base)) { const ext = extForMime(mime); if (ext) base += "." + ext; }
  return base;
}

// numbering continues across batches for the same folder+prefix
async function nextCounter(dest, prefix, count) {
  const key = `counter:${dest}|${prefix}`;
  const cur = (await chrome.storage.local.get(key))[key] || 0;
  await chrome.storage.local.set({ [key]: cur + count });
  return cur + 1;
}

// canonical identity of a media URL: no query, no fragment (chunked/signed variants collapse)
function canon(url) { try { const u = new URL(url); return u.origin + u.pathname; } catch { return url; } }
// Dedupe identity of an engine link: the VIDEO, not the page path. YouTube: the id out of watch?v= / youtu.be /
// shorts / embed / live; any other engine site keeps its query (minus tracking noise), so ?id= style links differ.
const TRACKING_PARAM = /^(utm_.*|fbclid|gclid|igshid|igsh|si|feature|pp|ref_src|ref_url|s|t)$/i;
function ytId(u) {
  const h = u.hostname.replace(/^(www|m|music)\./, "");
  if (h === "youtu.be") return u.pathname.split("/")[1] || "";
  if (h === "youtube.com" || h === "youtube-nocookie.com") {
    if (u.pathname === "/watch") return u.searchParams.get("v") || "";
    return (/^\/(?:shorts|embed|live|v)\/([\w-]+)/.exec(u.pathname) || [])[1] || "";
  }
  return "";
}
function sortedQuery(u, dropTracking) {
  const kept = [...u.searchParams].filter(([k]) => !(dropTracking && TRACKING_PARAM.test(k))).sort((a, b) => (a[0] + "=" + a[1]).localeCompare(b[0] + "=" + b[1]));
  return kept.length ? "?" + kept.map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v)).join("&") : "";
}
function engineCanon(url) {
  try {
    const u = new URL(url), id = ytId(u);
    if (id) return "https://www.youtube.com/watch?v=" + id;
    return u.origin + u.pathname + sortedQuery(u, true);
  } catch { return url; }
}
// a page saved as a site job: the query is part of the page's identity (viewtopic.php?t=, index.php?title=)
function siteCanon(url) { try { const u = new URL(url); return u.origin + u.pathname + sortedQuery(u, false); } catch { return url; } }
// Older spellings of an engine key. A record under one counts as saved (mp3: only when its file is an .mp3).
// Pre-0.1.43 keys were canon(url)+"#mode": right for youtu.be/<id>, shorts and embed links, wrong for watch?v=
// (every video shared one key), so that one is never an alias.
function engineAlts(url, mode, mp3, key) {
  const alts = [], legacy = canon(url) + "#" + mode + (mode === "audio" && mp3 ? "-mp3" : "");
  const watchPath = /^https?:\/\/[^/]*youtube\.com\/watch$/.test(canon(url));
  if (!watchPath && legacy !== key) alts.push({ k: legacy });
  if (mode === "audio" && mp3) {   // saved as MP3 before 0.1.41 (when MP3 was the only audio): '<url>#audio' with an .mp3 file
    alts.push({ k: engineCanon(url) + "#audio", mp3: true });
    if (!watchPath) alts.push({ k: canon(url) + "#audio", mp3: true });
  }
  return alts;
}

// ---------- native host ----------
// One persistent port streams file bytes (open / chunk… / close per file, several files interleaved);
// it is closed when no batch is running so the host process exits. One-shot jobs (the Finder picker, the
// exists check) use sendNativeMessage, which spawns their own host process — the picker would otherwise
// block the streaming port while the dialog is up.
let hostPort = null, hostSeq = 0;
const hostWaiters = new Map();   // message id -> resolve
function hostConnect() {
  if (hostPort) return hostPort;
  log("info", "host", "connect: opening the persistent port to " + NATIVE_HOST);
  const p = chrome.runtime.connectNative(NATIVE_HOST);
  p.onMessage.addListener((m) => { const w = hostWaiters.get(m.id); if (w) { hostWaiters.delete(m.id); w(m); } });
  p.onDisconnect.addListener(() => {
    hostPort = null;
    const err = chrome.runtime.lastError?.message || "native host disconnected";
    log(hostWaiters.size ? "error" : "info", "host", "port disconnected: " + err, { waiting: [...hostWaiters.keys()] });
    for (const [id, w] of hostWaiters) { hostWaiters.delete(id); w({ id, ok: false, output: err }); }
  });
  return (hostPort = p);
}
function hostRelease() { if (hostPort && !progress.active) { log("info", "host", "port released: no batch is running"); try { hostPort.disconnect(); } catch {} hostPort = null; keyCache.clear(); } }
function hostAsk(port, msg) {
  return new Promise((resolve) => {
    hostWaiters.set(msg.id, resolve);
    try { port.postMessage(msg); } catch (e) { hostWaiters.delete(msg.id); log("error", "host", `postMessage ${msg.cmd} ${msg.id} threw: ${e?.message || e}`); resolve({ ok: false, output: String(e?.message || e) }); }
  });
}
const NO_HOST = /not found|forbidden|disconnected/i;
// one request, own host process; never throws
async function hostOnce(m) {
  try { return (await chrome.runtime.sendNativeMessage(NATIVE_HOST, m)) || { ok: false, output: "native host did not answer" }; }
  catch (e) { return { ok: false, output: hostError(String(e?.message || e)) }; }
}
const hostError = (out) => NO_HOST.test(out || "") ? "native host not installed — run native/install.sh" : out;

// ---------- dependencies (yt-dlp, ffmpeg, gallery-dl, exiftool, pypdf, the BPM/key analyzer) ----------
async function getDeps() {
  try { return await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "deps" }); }
  catch (e) { return { ok: false, output: hostError(String(e?.message || e)) }; }
}
// brew installs can take minutes: the SAME persistent port fetch() uses, so it never blocks on the
// one-shot channel's own timeout. Releases the port again when done unless a batch is mid-flight.
async function runSetup() {
  const port = hostConnect();
  const r = await hostAsk(port, { cmd: "setup", id: "s" + (++hostSeq) });
  hostRelease();
  return r.ok ? r : { ...r, output: hostError(r.output) };
}
// install / update: check once, and only bother the user (badge) if something is actually missing
chrome.runtime.onInstalled.addListener((det) => {
  log("info", "worker", `installed/updated (${det?.reason}), version ${chrome.runtime.getManifest().version}`);
  (async () => {
    const d = await getDeps();
    if (!d?.ok) { log("warn", "deps", "host unreachable at install: " + (d?.output || "no answer")); return; }   // nothing to check yet; the popup's own status explains why
    const missing = Object.entries(d.deps).filter(([, v]) => !v.present).map(([k]) => k);
    log("info", "deps", missing.length ? "missing: " + missing.join(", ") : "all dependencies present");
    if (!missing.length) return;
    setBadge("…");
    const r = await runSetup();
    setBadge(r.missing?.length ? "!" : "", r.missing?.length ? "#d33" : undefined);
    await chrome.storage.session.set({ setupResult: { ok: r.ok, output: r.output || "", missing: r.missing || [], python: r.python || "", hints: r.hints || {} } });
  })();
});

function b64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
// file type from the first bytes (URL had no extension and the server sent none / octet-stream)
function sniffExt(b) {
  const at = (i, str) => [...str].every((ch, k) => b[i + k] === ch.charCodeAt(0));
  if (b.length < 12) return "";
  if (at(4, "ftyp")) return at(8, "qt") ? "mov" : "mp4";
  if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return "webm";
  if (b[0] === 0xff && b[1] === 0xd8) return "jpg";
  if (at(1, "PNG")) return "png";
  if (at(0, "GIF8")) return "gif";
  if (at(0, "RIFF") && at(8, "WEBP")) return "webp";
  if (at(0, "%PDF")) return "pdf";
  if (at(0, "ID3") || (b[0] === 0xff && (b[1] & 0xe0) === 0xe0)) return "mp3";
  return "";
}
const CHUNK = 1024 * 1024;      // bytes per host message
const STALL_MS = 60000;         // no bytes for this long = the file failed, the slot moves on
// ---------- Stop ----------
// Every batch owns an AbortController (live ones in liveRuns); Stop aborts them all. A save that sees its signal
// abort stops fetching, has the host drop its partial file, and resolves {ok:false, stopped:true}. Engine jobs
// (a subprocess in the host) are cancelled by id: the host kills the process group and removes its partial files.
const STOP_MSG = "stopped";
const NEVER = new AbortController().signal;   // callers outside a batch (tests, one-offs) are never stopped
const liveRuns = new Set();                   // AbortController per running batch
const engineJobs = new Set();                 // host fetch ids running right now
const stopError = () => Object.assign(new Error(STOP_MSG), { stopped: true });
const stoppedReply = (extra = {}) => ({ ok: false, stopped: true, output: STOP_MSG, ...extra });
// a promise that rejects when sig aborts: raced against each read, one listener per file (not one per chunk)
function abortPromise(sig) {
  const p = new Promise((_, rej) => { if (sig.aborted) rej(stopError()); else sig.addEventListener("abort", () => rej(stopError()), { once: true }); });
  p.catch(() => {});
  return p;
}
const sleepFor = (ms, sig = NEVER) => new Promise((resolve) => { const t = setTimeout(resolve, ms); sig.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true }); });
const withStop = (sig, own) => sig === NEVER ? own : own ? AbortSignal.any([own, sig]) : sig;
// Fetch url with the browser session and stream it to the host as name inside dest ("" = ~/Downloads).
// Flow-controlled: one chunk in flight per file, the host acks each before the next is sent, so memory
// stays bounded and every message resets the worker's idle timer. Resolves {ok, path} or {ok:false, output}.
// Never throws.
async function saveViaHost(url, name, dest, kind, fin = {}, sig = NEVER) {
  const id = "s" + (++hostSeq);
  let stallTimer = 0;
  log("info", "save", `${id} file: ${name} -> ${dest || "Downloads"}`, { url: logUrl(url), kind, strip: !!fin.strip, webp: fin.webp || "" });
  const stopP = abortPromise(sig);
  const read = (reader) => Promise.race([
    reader.read(), stopP,
    new Promise((_, rej) => { stallTimer = setTimeout(() => rej(new Error(`stalled: no data for ${STALL_MS / 1000} s`)), STALL_MS); }),
  ]).finally(() => clearTimeout(stallTimer));
  let res, reader, first;
  try {
    if (sig.aborted) throw stopError();
    res = await fetch(url, { credentials: "include", signal: withStop(sig) });
    log("debug", "save", `${id} response HTTP ${res.status} ${res.headers.get("content-type") || "?"} length=${res.headers.get("content-length") || "?"}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    reader = res.body.getReader();
    first = await read(reader);   // first chunk before "open": it decides the extension when nothing else does
  } catch (e) {
    try { reader?.cancel(); } catch {}
    if (sig.aborted) { log("info", "save", `${id} stopped before any byte was written`); return stoppedReply(); }
    log("warn", "save", `${id} fetch failed: ${e?.message || e}`, { url: logUrl(url) }); return { ok: false, output: String(e?.message || e) };
  }
  if (first.value && looksEncrypted(first.value)) { log("warn", "save", `${id} refused: encrypted (DRM) bytes`, { url: logUrl(url) }); try { reader.cancel(); } catch {} return { ok: false, output: "protected video (DRM) — encrypted file, not saved" }; }
  if (!hasExt(name)) {
    const ext = extForMime(res.headers.get("content-type")) || (first.value ? sniffExt(first.value) : "") || { video: "mp4", image: "jpg", doc: "pdf" }[kind] || "";
    if (ext) name += "." + ext;
  }
  const port = hostConnect();
  const opened = await hostAsk(port, { cmd: "open", id, name, dst: dest || "", ...fin });
  if (!opened.ok) { log("error", "save", `${id} host refused open: ${opened.output}`); try { reader.cancel(); } catch {} return { ok: false, output: hostError(opened.output) }; }
  try {
    let buf = new Uint8Array(0);
    const send = async (part) => {
      if (sig.aborted) throw stopError();
      const r = await hostAsk(port, { cmd: "chunk", id, data: b64(part) });
      if (!r.ok) throw new Error(hostError(r.output));
      progress.bytes += part.length; pushProgress(false);
    };
    for (let chunk = first; ; chunk = await read(reader)) {
      const { value, done } = chunk;
      if (done) break;
      if (buf.length) { const n = new Uint8Array(buf.length + value.length); n.set(buf); n.set(value, buf.length); buf = n; } else buf = value;
      while (buf.length >= CHUNK) { const part = buf.subarray(0, CHUNK); buf = buf.subarray(CHUNK); await send(part); }
    }
    if (buf.length) await send(buf);
    if (sig.aborted) throw stopError();
    const closed = await hostAsk(port, { cmd: "close", id });
    log(closed.ok ? "info" : "error", "save", `${id} closed: ${closed.ok ? `${closed.path} (${closed.bytes} bytes)` : closed.output}`);
    return closed.ok ? closed : { ok: false, output: hostError(closed.output) };
  } catch (e) {
    const stopped = sig.aborted;
    log(stopped ? "info" : "error", "save", `${id} ${stopped ? "stopped mid-file: partial file removed" : "aborted mid-file: " + (e?.message || e)}`, { url: logUrl(url) });
    try { reader.cancel(); } catch {}
    try { port.postMessage({ cmd: "abort", id }); } catch {}
    return stopped ? stoppedReply({ cut: true }) : { ok: false, output: String(e?.message || e) };
  }
}

// ---------- HLS / DASH streams ----------
// A player fed through Media Source Extensions has a blob: src; what the tab's network layer saw is a
// manifest (.m3u8 / .mpd) and its segments. The stream is saved by reading the manifest, taking the best
// variant (highest resolution, then bandwidth; a separate audio rendition when the variant is video-only),
// fetching its segments in order with the page session through the same acked pipe, and having the host
// join them into one .mp4 with ffmpeg. Clear-key AES-128 HLS (the key is handed to the browser in the
// open) is decrypted here; DRM (SAMPLE-AES, Widevine/PlayReady key formats, MPD ContentProtection) is
// refused, never worked around. Live streams (no end yet) are refused.
const isManifest = (url, ctype) => /mpegurl|dash\+xml/.test(ctype || "") || /\.(m3u8|mpd)(\?|$)/i.test(url);
const STREAM_AHEAD = 3;   // segments fetched ahead of the one being sent
async function fetchText(url) {
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) throw new Error(`HTTP ${res.status} (${url.split("/").pop().split("?")[0].slice(0, 40)})`);
  return res.text();
}
// Some CDNs sign the manifest URL for one fetch only (a repeat of the exact same URL, even from the
// page that just used it, comes back 403). streamCandidates() reads a manifest to classify it, then
// saveStreamViaHost() reads the SAME url to actually save it — two fetches of one one-shot URL. Cache
// the body per exact URL so only the first caller ever hits the network; the second reuses it. A
// failed fetch is never cached, so a real retry still gets a real attempt.
const manifestCache = new Map();   // url -> Promise<string>
// A site that spends the one-shot URL in the page's own player leaves nothing to fetch: the first request
// we make is already the second one. hook.js (page world) copies the manifest text the page itself got,
// hookbridge.js forwards it here, and it is kept (session storage: survives a worker restart) keyed by the
// exact URL. A copy is always preferred to a request; the network is only the fallback.
const MT_KEY = "manifestTexts", MT_BYTES = 2 * 1024 * 1024, MT_AGE = 30 * 60 * 1000;
// A page can say anything, so a copy is trusted only as far as it can be bound: it is stored under
// (tab id, frame origin, URL) taken from the message sender — never from the message — and used only by a
// save that starts from that same tab, and only while the tab has itself requested something from the URL's
// origin (webRequest capture). The whole cache is capped at MT_BYTES (oldest out first), so it can never
// crowd the other session-storage users out of their quota.
const mtLooksLikeManifest = (t) => /^\s*#EXTM3U/.test(t) || /<MPD[\s>]/i.test(t.slice(0, 4096));
let mtChain = Promise.resolve();
function mtPrune(all, now) {
  let bytes = 0;
  for (const [k, v] of Object.entries(all).sort((a, b) => b[1].ts - a[1].ts)) {
    if (now - v.ts > MT_AGE || bytes + v.text.length > MT_BYTES) delete all[k]; else bytes += v.text.length;
  }
}
function storeManifestText(sender, url, text) {
  const tabId = sender?.tab?.id;
  if (tabId == null || tabId < 0 || typeof url !== "string" || typeof text !== "string" || text.length > MT_BYTES || !mtLooksLikeManifest(text)) return Promise.resolve();
  const key = `${tabId}|${sender.origin || ""}|${url}`;
  const step = async () => {
    const all = (await chrome.storage.session.get(MT_KEY))[MT_KEY] || {}, now = Date.now();
    all[key] = { tabId, url, text, ts: now };
    mtPrune(all, now);
    try { await chrome.storage.session.set({ [MT_KEY]: all }); } catch (e) { console.warn("manifest copy not kept", e?.message || e); }
  };
  return (mtChain = mtChain.then(step, step));
}
function dropManifestTexts(tabId) {
  const step = async () => {
    const all = (await chrome.storage.session.get(MT_KEY))[MT_KEY] || {};
    for (const [k, v] of Object.entries(all)) if (v.tabId === tabId) delete all[k];
    await chrome.storage.session.set({ [MT_KEY]: all });
  };
  return (mtChain = mtChain.then(step, step));
}
async function pageManifestText(url, tabId) {
  if (tabId == null) return null;
  await mtChain.catch(() => {});
  let origin; try { origin = new URL(url).origin; } catch { return null; }
  const all = (await chrome.storage.session.get(MT_KEY))[MT_KEY] || {};
  const norm = normalizeMediaUrl(url);
  const mine = Object.values(all).filter((e) => e.tabId === tabId && (e.url === url || e.url === norm || normalizeMediaUrl(e.url) === norm)).sort((a, b) => b.ts - a.ts);
  if (!mine.length) return null;
  const requested = Object.keys(await getMedia(tabId)).some((u) => { try { return new URL(u).origin === origin; } catch { return false; } });
  return requested ? mine[0].text : null;
}
function fetchManifestOnce(url, tabId = null) {
  const k = tabId + "|" + url;
  if (!manifestCache.has(k)) {
    manifestCache.set(k, pageManifestText(url, tabId).then((t) => t ?? fetchText(url)).catch((e) => { manifestCache.delete(k); throw e; }));
  }
  return manifestCache.get(k);
}
// one segment (or a byte range of one); a fetch silent for STALL_MS fails the file
async function fetchBytes(url, range, sig = NEVER) {
  // 2 retries (0.5 s, then 1.5 s) on a network error or a 5xx; a stall (STALL_MS of silence), a 4xx or a DRM refusal fails at once
  for (let attempt = 0; ; attempt++) {
    if (sig.aborted) throw stopError();
    try { return await fetchBytesOnce(url, range, sig); }
    catch (e) {
      if (sig.aborted) throw stopError();
      if (e?.retry !== true) throw e;
      if (attempt >= 2) { log("error", "stream", `segment gave up after 3 tries: ${e.cause?.message || e.cause}`, { url: logUrl(url) }); throw e.cause; }
      log("warn", "stream", `segment retry ${attempt + 1}/2 after: ${e.cause?.message || e.cause}`, { url: logUrl(url) });
      await sleepFor(attempt ? 1500 : 500, sig);
    }
  }
}
async function fetchBytesOnce(url, range, sig = NEVER) {
  const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), STALL_MS), again = (e) => ctl.signal.aborted || sig.aborted ? e : Object.assign(new Error("retry"), { retry: true, cause: e });
  try {
    let res;
    try { res = await fetch(url, { credentials: "include", signal: withStop(sig, ctl.signal), headers: range ? { Range: `bytes=${range.start}-${range.end}` } : {} }); }
    catch (e) { throw again(e); }
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status} (${url.split("/").pop().split("?")[0].slice(0, 40)})`);
      throw res.status >= 500 ? again(err) : err;
    }
    let b;
    try { b = new Uint8Array(await res.arrayBuffer()); }
    catch (e) { throw again(e); }
    return range && res.status === 200 ? b.subarray(range.start, range.end + 1) : b;   // server ignored the range
  } finally { clearTimeout(t); }
}
function parseAttrs(s) { const o = {}; for (const m of s.matchAll(/([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))/g)) o[m[1]] = m[2] ?? m[3]; return o; }
// {master, variants, audio} for a master playlist; {segs, init, live} for a media playlist
function parseHls(text, base) {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const abs = (u) => new URL(u, base).href;
  if (/^#EXT-X-STREAM-INF/m.test(text)) {
    const variants = [], audio = [];
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith("#EXT-X-STREAM-INF:")) {
        const a = parseAttrs(lines[i].slice(18));
        while (lines[i + 1] && lines[i + 1].startsWith("#")) i++;
        if (!lines[i + 1]) break;
        variants.push({ url: abs(lines[++i]), bw: +a.BANDWIDTH || 0, h: +(a.RESOLUTION || "0x0").split("x")[1] || 0, audio: a.AUDIO || "", codecs: a.CODECS || "" });
      } else if (lines[i].startsWith("#EXT-X-MEDIA:")) {
        const a = parseAttrs(lines[i].slice(13));
        if (a.TYPE === "AUDIO" && a.URI) audio.push({ group: a["GROUP-ID"] || "", url: abs(a.URI), def: a.DEFAULT === "YES" });
      }
    }
    return { master: true, variants, audio };
  }
  const segs = []; let init = null, key = null, seq = 0, range = null, lastEnd = 0;
  const byterange = (s) => { const [n, o] = s.split("@").map(Number); const start = isNaN(o) ? lastEnd : o; lastEnd = start + n; return { start, end: start + n - 1 }; };
  for (const l of lines) {
    if (l.startsWith("#EXT-X-MEDIA-SEQUENCE:")) seq = +l.slice(22) || 0;
    else if (l.startsWith("#EXT-X-MAP:")) { const a = parseAttrs(l.slice(11)); init = { url: abs(a.URI), range: a.BYTERANGE ? byterange(a.BYTERANGE) : null }; }
    else if (l.startsWith("#EXT-X-KEY:")) { const a = parseAttrs(l.slice(11)); key = a.METHOD === "NONE" ? null : { method: a.METHOD, url: a.URI ? abs(a.URI) : "", iv: a.IV || "", format: a.KEYFORMAT || "identity" }; }
    else if (l.startsWith("#EXT-X-BYTERANGE:")) range = byterange(l.slice(17));
    else if (!l.startsWith("#")) { segs.push({ url: abs(l), range, key, seq: seq + segs.length }); range = null; }
  }
  return { master: false, segs, init, live: !/#EXT-X-ENDLIST/.test(text) };
}
const DRM_MSG = "protected video (DRM) — encrypted stream, not saved";
// tracks to fetch for an HLS URL: [{kind, ext, init, segs, label}]
async function hlsTracks(url, text, tabId) {
  const m = parseHls(text, url), tracks = [];
  if (m.master) {
    if (!m.variants.length) throw new Error("empty master playlist");
    const v = m.variants.slice().sort((a, b) => b.h - a.h || b.bw - a.bw)[0];
    const rend = m.audio.filter((a) => a.group === v.audio);
    const a = rend.find((r) => r.def) || rend[0];
    tracks.push({ kind: "video", url: v.url, label: v.h ? v.h + "p" : "" });
    if (a) tracks.push({ kind: "audio", url: a.url });   // a rendition with a URI is a separate playlist (muxed audio has none)
  } else tracks.push({ kind: "video", url, parsed: m });
  for (const t of tracks) {
    const p = t.parsed || parseHls(await fetchManifestOnce(t.url, tabId), t.url);
    if (p.master) throw new Error("nested master playlist");
    if (p.live) throw new Error("live stream — it has no end to save yet");
    if (!p.segs.length) throw new Error("empty playlist");
    for (const s of p.segs) if (s.key && (s.key.method !== "AES-128" || s.key.format !== "identity" || !s.key.url)) throw new Error(DRM_MSG);
    t.init = p.init; t.segs = p.segs; t.ext = p.init || /\.(m4s|mp4)(\?|$)/i.test(p.segs[0].url) ? "mp4" : "ts";
    delete t.parsed;
  }
  return tracks;
}
// clear-key AES-128: key fetched once per URL, IV from the tag or the segment's sequence number
const keyCache = new Map();
async function decryptSeg(bytes, seg) {
  const k = seg.key;
  if (!k) return bytes;
  if (!keyCache.has(k.url)) keyCache.set(k.url, fetchBytes(k.url).then((b) => crypto.subtle.importKey("raw", b, "AES-CBC", false, ["decrypt"])).catch((e) => { keyCache.delete(k.url); throw e; }));
  const iv = new Uint8Array(16);
  if (k.iv) { const hex = k.iv.replace(/^0x/i, "").padStart(32, "0"); for (let i = 0; i < 16; i++) iv[i] = parseInt(hex.substr(i * 2, 2), 16); }
  else new DataView(iv.buffer).setUint32(12, seg.seq);
  return new Uint8Array(await crypto.subtle.decrypt({ name: "AES-CBC", iv }, await keyCache.get(k.url), bytes));
}
// a small XML reader (service workers have no DOMParser): {name, attrs, children, text}
const unent = (s) => s.replace(/&(amp|lt|gt|quot|apos|#(\d+)|#x([0-9a-f]+));/gi, (m, n, d, h) => d ? String.fromCodePoint(+d) : h ? String.fromCodePoint(parseInt(h, 16)) : { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" }[n.toLowerCase()]);
function parseXml(text) {
  const root = { name: "#", attrs: {}, children: [], text: "" }, stack = [root];
  for (const m of text.matchAll(/<\?[^]*?\?>|<!--[^]*?-->|<!\[CDATA\[([^]*?)\]\]>|<\/([\w:.-]+)\s*>|<([\w:.-]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>|([^<]+)/g)) {
    const top = stack[stack.length - 1];
    if (m[1] !== undefined) top.text += m[1];
    else if (m[2]) { if (stack.length > 1) stack.pop(); }
    else if (m[3]) {
      const attrs = {};
      for (const a of (m[4] || "").matchAll(/([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) attrs[a[1].replace(/^.*:/, "")] = unent(a[2] ?? a[3]);
      const el = { name: m[3].replace(/^.*:/, ""), attrs, children: [], text: "" };
      top.children.push(el);
      if (!m[5]) stack.push(el);
    } else if (m[6]) top.text += unent(m[6]);
  }
  return root;
}
const isoDur = (s) => { const m = /P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?/.exec(s || ""); return m ? (+m[1] || 0) * 86400 + (+m[2] || 0) * 3600 + (+m[3] || 0) * 60 + (+m[4] || 0) : 0; };
const rangeOf = (s) => { if (!s) return null; const [a, b] = s.split("-").map(Number); return { start: a, end: b }; };
// tracks for a static MPD: best video representation + best audio (SegmentTemplate with or without a
// timeline, SegmentList, or a single BaseURL file); first Period only
function dashTracks(text, url) {
  const kids = (el, n) => el.children.filter((c) => c.name === n);
  const mpd = kids(parseXml(text), "MPD")[0];
  if (!mpd) throw new Error("not an MPD");
  if (mpd.attrs.type === "dynamic") throw new Error("live stream — it has no end to save yet");
  const baseOf = (el, base) => { const b = kids(el, "BaseURL")[0]; return b ? new URL(b.text.trim(), base).href : base; };
  const period = kids(mpd, "Period")[0];
  if (!period) throw new Error("empty MPD");
  const dur = isoDur(period.attrs.duration || mpd.attrs.mediaPresentationDuration);
  const pbase = baseOf(period, baseOf(mpd, url));
  const tracks = [];
  for (const kind of ["video", "audio"]) {
    let best = null;
    for (const as of kids(period, "AdaptationSet")) {
      if (kids(as, "ContentProtection").length) throw new Error(DRM_MSG);
      for (const rep of kids(as, "Representation")) {
        if (kids(rep, "ContentProtection").length) throw new Error(DRM_MSG);
        const mime = rep.attrs.mimeType || as.attrs.mimeType || "";
        if ((as.attrs.contentType || mime.split("/")[0]) !== kind) continue;
        const h = +rep.attrs.height || +as.attrs.height || 0, bw = +rep.attrs.bandwidth || 0;
        if (!best || h > best.h || (h === best.h && bw > best.bw)) best = { as, rep, h, bw, mime };
      }
    }
    if (!best) continue;
    const { as, rep, mime } = best;
    const base = baseOf(rep, baseOf(as, pbase));
    const t = { kind, ext: /webm/.test(mime) ? "webm" : "mp4", label: kind === "video" && best.h ? best.h + "p" : "", init: null, segs: [] };
    const st = kids(rep, "SegmentTemplate")[0] || kids(as, "SegmentTemplate")[0];
    const tmpl = { ...(kids(as, "SegmentTemplate")[0] || {}).attrs, ...(kids(rep, "SegmentTemplate")[0] || {}).attrs };
    const timeline = st && kids(st, "SegmentTimeline")[0];
    const list = kids(rep, "SegmentList")[0] || kids(as, "SegmentList")[0];
    const fill = (s, n, time) => new URL(s.replace(/\$RepresentationID\$/g, rep.attrs.id || "").replace(/\$Bandwidth\$/g, rep.attrs.bandwidth || "")
      .replace(/\$Number(?:%0(\d+)d)?\$/g, (_, w) => String(n).padStart(+w || 0, "0")).replace(/\$Time\$/g, String(time)).replace(/\$\$/g, "$"), base).href;
    if (tmpl.media) {
      if (tmpl.initialization) t.init = { url: fill(tmpl.initialization, 0, 0) };
      const start = tmpl.startNumber === undefined ? 1 : +tmpl.startNumber, ts = +tmpl.timescale || 1;
      if (timeline) {
        let n = start, time = 0;
        for (const s of kids(timeline, "S")) {
          if (s.attrs.t !== undefined) time = +s.attrs.t;
          for (let r = 0; r <= Math.max(0, +s.attrs.r || 0); r++) { t.segs.push({ url: fill(tmpl.media, n++, time) }); time += +s.attrs.d; }
        }
      } else {
        if (!dur || !+tmpl.duration) throw new Error("MPD gives no segment count");
        const count = Math.ceil(dur * ts / +tmpl.duration);
        for (let n = start; n < start + count; n++) t.segs.push({ url: fill(tmpl.media, n, 0) });
      }
    } else if (list) {
      const ini = kids(list, "Initialization")[0];
      if (ini) t.init = { url: new URL(ini.attrs.sourceURL || "", base).href, range: rangeOf(ini.attrs.range) };
      for (const s of kids(list, "SegmentURL")) t.segs.push({ url: new URL(s.attrs.media || "", base).href, range: rangeOf(s.attrs.mediaRange) });
    } else t.segs.push({ url: base });   // SegmentBase / BaseURL only: the representation is one whole file
    if (!t.segs.length) throw new Error("no segments in MPD");
    tracks.push(t);
  }
  if (!tracks.length) throw new Error("no video in MPD");
  return tracks;
}
// the manifests worth offering for a tab: masters (and media playlists no captured master lists), newest first
async function streamsFor(tabId) {
  const cap = (await mediaList(tabId)).filter((c) => isManifest(c.url, c.ctype)).sort((a, b) => b.ts - a.ts);
  const found = [], children = new Set(), failed = [];
  for (const c of cap) {
    let text;
    try { text = await fetchManifestOnce(c.url, tabId); }
    catch (e) { log("warn", "stream", `manifest not readable: ${e?.message || e}`, { url: logUrl(c.url) }); failed.push({ url: c.url, output: String(e?.message || e) }); continue; }
    if (text.trimStart().startsWith("#EXTM3U")) {
      const m = parseHls(text, c.url);
      if (m.master) for (const v of [...m.variants, ...m.audio]) children.add(canon(v.url));
      found.push({ url: c.url, master: m.master });
    } else if (/<MPD[\s>]/i.test(text)) found.push({ url: c.url, master: true });
  }
  const seen = new Set();
  const ok = found.filter((s) => (s.master || !children.has(canon(s.url))) && !seen.has(canon(s.url)) && seen.add(canon(s.url))).map((s) => ({ url: s.url, stream: true }));
  log("debug", "stream", `candidates for tab ${tabId}: ${cap.length} captured manifest(s), ${found.length} readable, ${ok.length} offered, ${failed.length} failed`, ok.map((s) => logUrl(s.url)));
  if (ok.length || !failed.length) return ok;
  // every manifest the tab captured failed to (re-)fetch (a one-shot signed URL some sites issue,
  // already spent by the page's own player): offer the newest one anyway, so the real save is attempted
  // and its real error reaches the user — never a silent drop that shows the wrong "press play" message.
  return [{ url: failed[0].url, stream: true }];
}
// a stream's file name when nothing was typed: the last meaningful path segment of the manifest URL
// (not index/master/a hex id …), else the page title
const STREAM_NOISE = /^(master|index|playlist|manifest|prog_index|chunklist.*|stream|video|videos|media|hls|dash|main|out|v\d*|\d+)$/i;
function streamName(url, title) {
  try {
    const parts = decodeURIComponent(new URL(url).pathname).split("/").filter(Boolean).map((p) => p.replace(/\.(m3u8|mpd)$/i, ""));
    for (let i = parts.length - 1; i >= 0; i--) { const s = sanitize(parts[i]); if (s.length > 2 && !STREAM_NOISE.test(s) && !/^[a-f0-9_-]{16,}$/i.test(s)) return s; }
  } catch {}
  return sanitize((title || "").replace(/\s*[|\-–—:]\s*[^|\-–—:]*$/, "")) || "video";
}
// Fetch every segment of the stream's best tracks, stream them to the host as temp files, then have the
// host join them into name.mp4 in dest. Resolves {ok, path} or {ok:false, output}. Never throws.
async function saveStreamViaHost(url, name, dest, fin = {}, tabId = null, sig = NEVER) {
  const port = hostConnect(), ids = [];
  try {
    if (sig.aborted) throw stopError();
    const text = await fetchManifestOnce(url, tabId);
    const isHls = text.trimStart().startsWith("#EXTM3U");
    const tracks = isHls ? await hlsTracks(url, text, tabId) : dashTracks(text, url);
    log("info", "stream", `${name}: ${isHls ? "HLS" : "DASH"} manifest, ${tracks.length} track(s) to fetch`, { url: logUrl(url), tracks: tracks.map((t) => ({ kind: t.kind, label: t.label, ext: t.ext, segments: t.segs.length, init: !!t.init })) });
    for (const t of tracks) {
      const id = "s" + (++hostSeq);
      const opened = await hostAsk(port, { cmd: "open", id, name: `${name}.${t.kind}.${t.ext}`, dst: dest || "", temp: true });
      if (!opened.ok) throw new Error(hostError(opened.output));
      ids.push(id);
      const parts = [...(t.init ? [{ ...t.init, init: true }] : []), ...t.segs];
      log("debug", "stream", `${name}: ${t.kind} track ${id}: ${parts.length} part(s), ${STREAM_AHEAD} fetched ahead`);
      const pending = [];
      for (let i = 0, k = 0; i < parts.length || pending.length; k++) {
        while (pending.length < STREAM_AHEAD && i < parts.length) {
          const s = parts[i++], p = fetchBytes(s.url, s.range, sig).then((b) => s.init ? b : decryptSeg(b, s));
          p.catch(() => {});   // a Stop leaves the ones fetched ahead unawaited: never an unhandled rejection
          pending.push(p);
        }
        const bytes = await pending.shift();
        if (sig.aborted) throw stopError();
        if (k === 0 && looksEncrypted(bytes)) throw new Error(DRM_MSG);
        for (let o = 0; o < bytes.length; o += CHUNK) {
          const r = await hostAsk(port, { cmd: "chunk", id, data: b64(bytes.subarray(o, o + CHUNK)) });
          if (!r.ok) throw new Error(hostError(r.output));
        }
        progress.bytes += bytes.length; pushProgress(false);
      }
      const closed = await hostAsk(port, { cmd: "close", id });
      if (!closed.ok) throw new Error(hostError(closed.output));
    }
    if (sig.aborted) throw stopError();
    const r = await hostAsk(port, { cmd: "join", id: "s" + (++hostSeq), ids, name, dst: dest || "", ...fin });
    log(r.ok ? "info" : "error", "stream", `${name}: join ${r.ok ? `ok -> ${r.path} (${r.bytes} bytes)${r.output ? " — " + r.output : ""}` : "FAILED: " + r.output}`);
    return r.ok ? r : { ok: false, output: hostError(r.output) };
  } catch (e) {
    for (const id of ids) try { port.postMessage({ cmd: "abort", id }); } catch {}
    if (sig.aborted) { log("info", "stream", `${name}: stopped, ${ids.length} temp track(s) removed`); return stoppedReply({ cut: ids.length > 0 }); }
    log("error", "stream", `${name}: stream save failed: ${e?.message || e}`, { url: logUrl(url) });
    return { ok: false, output: streamError(String(e?.message || e)) };
  }
}
// a 401/403 fetching the manifest itself (not a segment) almost always means the site signed that
// exact URL for one use, and the page's own player already spent it — never a fetch we can retry
const streamError = (msg) => /^HTTP 40[13] \([^)]*\.(m3u8|mpd)\)/.test(msg || "")
  ? `${msg} — this site's video link is good for one use and the page already used it; refresh the page and download immediately`
  : msg;

// ---------- whole site: crawl same-origin pages breadth-first, save one merged PDF or Markdown file ----------
// Uses chrome.tabs (navigate a background tab, wait for load) + chrome.debugger (Page.printToPDF) — no
// Node, no headless browser of its own. Link discovery and text extraction go through the existing
// content script (siteLinks / siteMarkdown), same as every other scan in this file.
const SITE_ASSET_EXT = /\.(png|jpe?g|gif|webp|svg|ico|bmp|tiff?|pdf|zip|rar|tar|gz|7z|mp4|webm|mov|avi|mkv|mp3|wav|ogg|flac|css|js|mjs|json|xml|woff2?|ttf|eot|otf|exe|dmg|pkg|apk)(\?.*)?$/i;
// Links that change state when merely fetched (GET): a crawl runs with the user's logged-in session, so it never
// follows them. Words are matched as whole path pieces (/logout, /user/delete, /remove_item.php) and as parts of
// query-parameter names (?action=, ?token=, ?_wpnonce=, ?logout=).
const SITE_UNSAFE_WORDS = ["logout", "log-out", "log_out", "logoff", "log-off", "signout", "sign-out", "sign_out", "signoff",
  "delete", "remove", "destroy", "unsubscribe", "unfollow", "cancel", "revoke", "disconnect", "checkout",
  "add-to-cart", "add_to_cart", "addtocart", "cart-add", "empty-cart", "purchase"];
const SITE_UNSAFE_PARAMS = ["action", "token", "nonce", "csrf", "logout", "signout", "delete", "remove", "unsubscribe", "unfollow", "destroy"];
const SITE_UNSAFE_PATH = new RegExp("(^|[/_.\\-])(" + SITE_UNSAFE_WORDS.join("|") + ")($|[/_.\\-])", "i");
const SITE_UNSAFE_PARAM = new RegExp(SITE_UNSAFE_PARAMS.join("|"), "i");
function siteUnsafe(u) {
  if (SITE_UNSAFE_PATH.test(u.pathname)) return true;
  for (const k of u.searchParams.keys()) if (SITE_UNSAFE_PARAM.test(k)) return true;
  return false;
}
// same-origin, no fragment, no asset link — null if it should not be crawled. The trailing slash stays (/docs/ and
// /docs can be different pages); safe=true also drops state-changing links.
function siteNorm(href, origin, safe) {
  try {
    const u = new URL(href, origin);
    if (u.origin !== origin) return null;
    u.hash = "";
    if (SITE_ASSET_EXT.test(u.pathname)) return null;
    if (safe && siteUnsafe(u)) return null;
    return u.href;
  } catch { return null; }
}
// What the server answers for a page, asked BEFORE the tab navigates (service-worker fetch with the user's session;
// the body is never read). Skips what must not become a page: HTTP >= 400, redirect loops/failures, anything
// that is not HTML (a download link would stall the tab and fire the browser's own download path).
async function siteProbe(url, sig = NEVER) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 20000);
  try {
    const r = await fetch(url, { credentials: "include", redirect: "follow", signal: withStop(sig, ctl.signal) });
    const ctype = (r.headers.get("content-type") || "").toLowerCase(), disp = r.headers.get("content-disposition") || "";
    ctl.abort();
    if (r.status >= 400) return { skip: `HTTP ${r.status}` };
    if (/^\s*attachment/i.test(disp)) return { skip: "download link" };
    if (ctype && !/^(text\/html|application\/xhtml)/.test(ctype)) return { skip: `not a page (${ctype.split(";")[0]})` };
    return { final: r.url || url };
  } catch (e) { return { skip: String(e?.message || e) }; }
  finally { clearTimeout(timer); }
}
function unb64(s) {
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
// resolves once the tab has finished loading (or after a timeout, so a page that never fires "complete" can't wedge the crawl)
function waitTabLoad(tabId, timeoutMs = 30000, sig = NEVER) {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(finish, timeoutMs);
    sig.addEventListener("abort", finish, { once: true });
    function onUpdated(id, info) { if (id === tabId && info.status === "complete") finish(); }
    function finish() { if (done) return; done = true; clearTimeout(timer); chrome.tabs.onUpdated.removeListener(onUpdated); resolve(); }
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") finish(); }).catch(finish);
  });
}
async function siteLinksOf(tabId, origin) {
  let hrefs; try { hrefs = await chrome.tabs.sendMessage(tabId, { type: "siteLinks" }, { frameId: 0 }); } catch { hrefs = []; }
  const out = []; for (const h of hrefs || []) { const n = siteNorm(h, origin, true); if (n) out.push(n); } return out;
}
async function siteMarkdownOf(tabId) {
  try { return await chrome.tabs.sendMessage(tabId, { type: "siteMarkdown" }, { frameId: 0 }) || { title: "", text: "" }; }
  catch { return { title: "", text: "" }; }
}
// Crawls breadth-first from url (same origin only), rendering each page to a PDF (merged by the host) or
// collecting its readable text into one Markdown file. Resolves {ok, path, ...} or {ok:false, output}.
async function saveSiteViaHost(url, mode, maxPages, dest, fin = {}, follow = false, sig = NEVER) {
  const port = hostConnect();
  const origin = new URL(url).origin;
  const start = siteNorm(url, origin) || url;
  const visited = new Set([start]), queue = [start];
  const hostname = new URL(url).hostname.replace(/^www\./, "");
  const name = sanitize(hostname) || "site";
  const crawlId = ++crawlSeq;
  log("info", "crawl", `#${crawlId} start: ${mode} of ${logUrl(url)} follow=${follow} maxPages=${follow ? maxPages : 1}`);
  if (mode !== "pdf") {   // Markdown writes one file at the very end: find out now that the host or the folder is unusable, not after every page
    const pid = "s" + (++hostSeq);
    const probe = await hostAsk(port, { cmd: "open", id: pid, name: ".jownloader-check.md", dst: dest || "", temp: true });
    if (!probe.ok) return { ok: false, output: hostError(probe.output) };
    try { port.postMessage({ cmd: "abort", id: pid }); } catch {}
  }
  const tab = await chrome.tabs.create({ url: "about:blank", active: false });
  const tabId = tab.id;
  let attached = false;
  const ids = [], titles = [], mdParts = [];
  let n = 0;
  if (!follow) maxPages = 1;   // "This page only"
  try {
    if (mode === "pdf") { await chrome.debugger.attach({ tabId }, "1.3"); attached = true; }
    while (queue.length && n < maxPages) {
      if (sig.aborted) throw stopError();
      const pageUrl = queue.shift(), isStart = pageUrl === start && !n;
      // ask the server first: error pages, downloads and redirect targets already seen never become pages
      const probe = await siteProbe(pageUrl, sig);
      if (sig.aborted) throw stopError();
      if (probe.skip) { log("info", "crawl", `#${crawlId} skip ${logUrl(pageUrl)}: ${probe.skip}`); if (isStart) throw new Error(`${pageUrl} was not saved: ${probe.skip}`); continue; }
      const final = siteNorm(probe.final, origin);
      if (!final || (final !== pageUrl && visited.has(final))) { log("info", "crawl", `#${crawlId} skip ${logUrl(pageUrl)}: redirected to ${logUrl(probe.final)} (off-site or already seen)`); if (isStart) throw new Error(`${pageUrl} was not saved: redirected away`); continue; }
      visited.add(final);
      await chrome.tabs.update(tabId, { url: pageUrl });
      await waitTabLoad(tabId, 30000, sig);
      if (sig.aborted) throw stopError();
      let pageTitle = "", landedUrl = "";
      try { const t = await chrome.tabs.get(tabId); pageTitle = t.title || ""; landedUrl = t.url || ""; } catch {}
      if (/^chrome-error:/.test(landedUrl)) { log("warn", "crawl", `#${crawlId} skip ${logUrl(pageUrl)}: the page did not load (chrome-error)`); if (isStart) throw new Error(`${pageUrl} was not saved: the page did not load`); continue; }
      const landed = siteNorm(landedUrl, origin);   // a script/meta redirect after load
      if (!landed || (landed !== final && visited.has(landed))) { log("info", "crawl", `#${crawlId} skip ${logUrl(pageUrl)}: script redirect to ${logUrl(landedUrl)}`); if (isStart) throw new Error(`${pageUrl} was not saved: redirected away`); continue; }
      visited.add(landed);
      n++;
      log("info", "crawl", `#${crawlId} page ${n}: ${logUrl(pageUrl)} "${pageTitle.slice(0, 80)}"`);
      crawlProgress(crawlId, n, Math.min(maxPages, n + queue.length), pageUrl);
      if (mode === "pdf") {
        const r = await chrome.debugger.sendCommand({ tabId }, "Page.printToPDF", { printBackground: true });
        const bytes = unb64(r.data);
        const id = "s" + (++hostSeq);
        const opened = await hostAsk(port, { cmd: "open", id, name: `${name}.${n}.pdf`, dst: dest || "", temp: true });
        if (!opened.ok) throw new Error(hostError(opened.output));
        ids.push(id); titles.push(pageTitle);
        for (let o = 0; o < bytes.length; o += CHUNK) {
          const r2 = await hostAsk(port, { cmd: "chunk", id, data: b64(bytes.subarray(o, o + CHUNK)) });
          if (!r2.ok) throw new Error(hostError(r2.output));
        }
        progress.bytes += bytes.length; pushProgress(false);
        const closed = await hostAsk(port, { cmd: "close", id });
        if (!closed.ok) throw new Error(hostError(closed.output));
      } else {
        const { title, text } = await siteMarkdownOf(tabId);
        titles.push(title || pageTitle);
        mdParts.push(`# ${title || pageTitle}\n\n${pageUrl}\n\n${text}\n\n---\n\n`);
        progress.bytes += text.length; pushProgress(false);
      }
      if (!follow) break;
      const links = await siteLinksOf(tabId, origin);
      const before = queue.length;
      for (const l of links) if (!visited.has(l)) { visited.add(l); queue.push(l); }
      log("debug", "crawl", `#${crawlId} ${links.length} same-site safe link(s) on page ${n}, ${queue.length - before} new, ${queue.length} queued`);
    }
  } catch (e) {
    for (const id of ids) try { port.postMessage({ cmd: "abort", id }); } catch {}
    if (sig.aborted) { log("info", "crawl", `#${crawlId} stopped after ${n} page(s): nothing written`); return stoppedReply({ cut: n > 0 }); }
    log("error", "crawl", `#${crawlId} failed after ${n} page(s): ${e?.message || e}`);
    return { ok: false, output: String(e?.message || e) };
  } finally {
    crawlProgress(crawlId, 0);
    if (attached) try { await chrome.debugger.detach({ tabId }); } catch {}
    if (tabId != null) try { await chrome.tabs.remove(tabId); } catch {}
  }
  if (sig.aborted) { for (const id of ids) try { port.postMessage({ cmd: "abort", id }); } catch {} return stoppedReply({ cut: true }); }
  log("info", "crawl", `#${crawlId} crawled ${n} page(s), ${mode === "pdf" ? "merging PDFs" : "writing Markdown"}`);
  if (mode === "pdf") {
    const r = await hostAsk(port, { cmd: "mergepdf", id: "s" + (++hostSeq), ids, name, titles, dst: dest || "", ...fin });
    log(r.ok ? "info" : "error", "crawl", `#${crawlId} merge ${r.ok ? "ok -> " + r.path : "FAILED: " + r.output}`);
    return r.ok ? r : { ok: false, output: hostError(r.output) };
  }
  const md = mdParts.join("");
  const bytes = new TextEncoder().encode(md);
  const id = "s" + (++hostSeq);
  const mdName = (titles[0] || "").replace(/[\/\\\x00-\x1f]+/g, "-").replace(/\s+/g, " ").trim().replace(/^\.+/, "").slice(0, 150).replace(/[\ud800-\udbff]$/, "") || name;   // named by the first page's title, like the PDF
  const opened = await hostAsk(port, { cmd: "open", id, name: mdName + ".md", dst: dest || "", ...fin });
  if (!opened.ok) return { ok: false, output: hostError(opened.output) };
  try {
    for (let o = 0; o < bytes.length; o += CHUNK) {
      const r = await hostAsk(port, { cmd: "chunk", id, data: b64(bytes.subarray(o, o + CHUNK)) });
      if (!r.ok) throw new Error(hostError(r.output));
    }
    const closed = await hostAsk(port, { cmd: "close", id });
    return closed.ok ? closed : { ok: false, output: hostError(closed.output) };
  } catch (e) {
    try { port.postMessage({ cmd: "abort", id }); } catch {}
    return { ok: false, output: String(e?.message || e) };
  }
}

// ---------- the engine: sites whose media sits behind a player API ----------
// The page URL goes to the host, whose engine runs the site's downloader (YouTube video/audio, Instagram,
// Twitter/X) with the browser's own session. One reply when it is done; the host keeps streaming other files.
async function saveEngineViaHost(url, mode, dest, fin = {}, extra = {}, sig = NEVER) {
  if (sig.aborted) return stoppedReply({ notStarted: true });
  const port = hostConnect();
  const id = "s" + (++hostSeq), t0 = Date.now();
  log("info", "engine", `${id} dispatch to the host engine: ${mode} ${logUrl(url)} -> ${dest || "Downloads"}`, { mp3: !!extra.mp3, analyze: !!extra.analyze, strip: !!fin.strip, webp: fin.webp || "", song: extra.meta ? `${extra.meta.number}. ${extra.meta.artists} - ${extra.meta.title}` : undefined });
  engineJobs.add(id);
  const onStop = () => { try { port.postMessage({ cmd: "cancel", id: "c" + (++hostSeq), jobs: [id] }); } catch {} log("info", "engine", `${id} stop: asked the host to cancel it`); };
  sig.addEventListener("abort", onStop, { once: true });
  let r;
  try { r = await hostAsk(port, { cmd: "fetch", id, url, mode, dst: dest || "", ...fin, ...extra }); }
  finally { engineJobs.delete(id); sig.removeEventListener("abort", onStop); }
  if (r.stopped || (sig.aborted && !r.ok)) { log("info", "engine", `${id} stopped after ${((Date.now() - t0) / 1000).toFixed(1)} s: the host killed it and removed its partial files`); return stoppedReply({ cut: true }); }
  log(r.ok ? "info" : "error", "engine", `${id} reply after ${((Date.now() - t0) / 1000).toFixed(1)} s: ${r.ok ? "ok " + (r.path || "") + (r.output ? " — " + r.output : "") : "FAILED: " + r.output}`);
  return r.ok ? r : { ok: false, output: hostError(r.output) };
}

// ---------- Spotify links ----------
// A link's track list comes from the host (the link's public embed page, no sign-in); every song then runs
// through the engine as its own item, so progress, the Batch counts and dedupe all work unchanged.
// Songs are fetched one at a time, SPOTIFY_GAP_MS apart (start to start), so YouTube never sees a burst.
const SPOTIFY_GAP_MS = 20000;   // keep equal to engine.py's SPOTIFY_GAP_S
let spotifyChain = Promise.resolve(), spotifyLast = 0;
function spotifyTurn(fn, sig = NEVER) {
  const run = spotifyChain.then(async () => {
    if (sig.aborted) return stoppedReply({ notStarted: true });    // stopped while waiting its turn: never started
    const wait = spotifyLast + SPOTIFY_GAP_MS - Date.now();
    if (wait > 0) { log("debug", "spotify", `waiting ${Math.round(wait / 1000)} s (the gap between two songs)`); await sleepFor(wait, sig); }
    if (sig.aborted) return stoppedReply({ notStarted: true });
    spotifyLast = Date.now();
    return fn();
  });
  spotifyChain = run.catch(() => {});
  return run;
}
async function spotifyList(url) {
  const port = hostConnect();
  log("info", "spotify", "list requested: " + logUrl(url));
  try {
    const r = await hostAsk(port, { cmd: "list", id: "s" + (++hostSeq), url });
    if (r.ok) log("info", "spotify", `list ok: ${r.kind} "${r.name}" ${r.tracks?.length} song(s)${r.truncated ? " (embed cut short)" : ""}${r.skipped?.length ? `, ${r.skipped.length} skipped (not songs)` : ""}`);
    else log("error", "spotify", "list FAILED: " + r.output);
    return r.ok ? r : { ok: false, output: hostError(r.output) };
  } finally { hostRelease(); }
}

// ---------- progress (the popup's bar) ----------
// errors = every failure of the run, one line each ("song 6 Floaty: YouTube refused the download ..."); now = what is running
// stopping = Stop was pressed and jobs are letting go; stopped = the run ended by Stop: notStarted never began, cut were
// cut off mid-file (their partial files removed). done/failed count only jobs that finished on their own.
const progress = { total: 0, done: 0, failed: 0, bytes: 0, active: 0, where: "", kind: "", error: "", errors: [], fails: [], now: [], crawl: null, stopping: false, stopped: false, notStarted: 0, cut: 0 };
// site crawl: page n of m (m = the pages known so far, capped by "Max pages") + the URL being rendered; n = 0 clears it
// Every crawl has its own entry (id -> {n, m, url}); the bar and badge show the sum of the crawls running, and
// are only cleared when the last one ends. m only grows, and the bar never steps back (m is raised to hold it).
let crawlSeq = 0, crawlFrac = 0;
const crawls = new Map();
function crawlProgress(id, n, m, url) {
  if (n) { const prev = crawls.get(id); crawls.set(id, { n, m: Math.max(m, n, prev ? prev.m : 0), url }); }
  else crawls.delete(id);
  if (!crawls.size) { crawlFrac = 0; progress.crawl = null; }
  else {
    let sn = 0, sm = 0; for (const c of crawls.values()) { sn += c.n; sm += c.m; }
    if (sn / sm < crawlFrac) sm = Math.ceil(sn / crawlFrac);
    crawlFrac = Math.max(crawlFrac, sn / sm);
    progress.crawl = { n: sn, m: sm, url: url || [...crawls.values()].pop().url };
  }
  if (!progress.crawl && !progress.active) setBadge("");
  pushProgress(true);
}
// The badge follows the run: the page number while a crawl renders, else the files still to go (4 characters fit),
// blank when idle; endOfRun leaves "!" in red when something failed.
let badgeNow = null;
function setBadge(text, color = "#2563eb") {
  if (badgeNow === text + color) return;
  badgeNow = text + color;
  try { chrome.action.setBadgeBackgroundColor({ color }); chrome.action.setBadgeText({ text }); } catch {}
}
function syncBadge() {
  if (progress.crawl) setBadge(String(progress.crawl.n));
  else if (progress.active) { const left = Math.max(0, progress.total - progress.done - progress.notStarted - progress.cut); setBadge(progress.stopping ? "■" : left > 9999 ? "9999" : String(left)); }
}
let progTimer = 0;
function pushProgress(force) {
  if (progTimer && !force) return;
  progTimer = setTimeout(() => { progTimer = 0; syncBadge(); chrome.runtime.sendMessage({ type: "progress", ...progress }).catch(() => {}); }, force ? 0 : 150);
}
// Stop: every running batch's signal aborts. Queued jobs never start, in-flight fetches abort and their partial files
// are dropped, engine processes are killed by the host. The batches unwind on their own and the last one ends the run.
function stopAll() {
  if (!progress.active || !liveRuns.size) return { ok: true, idle: true };
  progress.stopping = true;
  log("info", "stop", `Stop pressed: ${liveRuns.size} batch(es), ${progress.now.length} job(s) in flight, ${engineJobs.size} engine job(s), ${progress.total - progress.done - progress.now.length} queued`, { now: progress.now.slice(0, 8) });
  for (const ctl of liveRuns) ctl.abort();
  pushProgress(true);
  return { ok: true };
}

// ---------- a batch ----------
// Every URL that was saved successfully is remembered (canonical form → {ts, path}, chrome.storage.local).
// A URL is skipped while its file is still on disk or still downloading; trash the file and it downloads
// again. "forget saved" in the popup wipes the memory.
const inFlight = new Set();   // canonical URLs downloading right now
// one storage key per URL ("saved:" + canonical), so marking a file never rewrites the whole list
const savedKey = (c) => "saved:" + c;
async function getSaved(canons) {
  const r = await chrome.storage.local.get(canons.map(savedKey));
  const out = {};
  for (const c of canons) if (r[savedKey(c)]) out[c] = r[savedKey(c)];
  return out;
}
const markSaved = (c, path) => chrome.storage.local.set({ [savedKey(c)]: { ts: Date.now(), path: path || "" } });
async function savedKeys() { return Object.keys(await chrome.storage.local.get(null)).filter((k) => k.startsWith("saved:")); }
// which of these saved files are still where we put them (one host round trip). Host unreachable → none
// counts as saved, so nothing is silently skipped: the batch runs and every file fails with the install hint.
async function stillOnDisk(paths) {
  if (!paths.length) return [];
  try {
    const r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "exists", paths });
    return r?.exists || paths.map(() => false);
  } catch { return paths.map(() => false); }
}
// an MP4 whose header carries encryption boxes is a DRM file: unplayable bytes, never saved
function looksEncrypted(b) {
  let s = "";
  for (let i = 0, end = Math.min(b.length, 65536); i < end; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, Math.min(i + 0x8000, end)));
  return /pssh|encv|enca|tenc|sinf/.test(s);
}
// batches can start together (auto-download + a button): dedupe + counter run one batch at a time
let batchChain = Promise.resolve();
const serial = (fn) => (batchChain = batchChain.then(fn, fn));
// items: [{url, ctype?, date?}] or plain URLs. onQueued fires with {queued, skipped, unusable} once the
// batch is deduped, before any bytes move; the popup follows the rest through "progress" messages.
async function downloadAll(items, kind, opts = {}, onQueued = () => {}) {
  const settings = await chrome.storage.sync.get({ dest: "", prefix: "", strip: false, webp: "", spotifyBpm: false, mp3: false });
  const dest = opts.dest ?? settings.dest, prefix = opts.prefix ?? settings.prefix;
  const fin = { strip: !!settings.strip, webp: settings.webp === true ? "jpg" : settings.webp || "" };   // finishing steps the host applies to every file
  log("info", "batch", `start: ${items.length} item(s), kind=${kind}, dest=${dest || "Downloads"}, prefix=${prefix || "-"}, strip=${fin.strip}, webp=${fin.webp || "-"}, mp3=${!!settings.mp3}, spotifyBpm=${!!settings.spotifyBpm}`);
  // dedupe within the batch by canonical URL, and against everything ever saved
  const jobs = [], seen = new Set();
  let skipped = 0, unusable = 0;
  // the tab a save starts from (the popup's active tab when the message names none): only that tab's page-copied manifests are used
  const srcTab = opts.tabId ?? (await chrome.tabs.query({ active: true, lastFocusedWindow: true }).catch(() => []))[0]?.id ?? null;
  await serial(async () => {
    const cands = [];
    for (const raw of items) {
      const it = typeof raw === "string" ? { url: raw } : raw;
      if (!it.url || !/^https?:/.test(it.url)) { unusable++; log("info", "dedupe", "unusable (not an http/https URL): " + logUrl(it.url)); continue; }
      // a page saved as video AND as audio = two files; a Spotify song in two lists = two files (one per list folder)
      const mode = it.mode || "video";
      const c = it.site ? siteCanon(it.url) + "#site-" + (it.mode || "pdf") + (it.follow ? "-follow" : "")
        : it.engine ? engineCanon(it.url) + "#" + mode + (it.spotify ? ":" + (it.spotify.folder || "") : mode === "audio" && settings.mp3 ? "-mp3" : "")
        : canon(it.url);
      const alts = it.engine && !it.spotify ? engineAlts(it.url, mode, settings.mp3, c) : [];
      if (seen.has(c) || inFlight.has(c)) { skipped++; log("info", "dedupe", `skip: ${seen.has(c) ? "the same item twice in this batch" : "already downloading right now"}`, { key: c }); continue; }
      seen.add(c); cands.push({ alts, url: it.url, mime: it.ctype || "", date: it.date || "", stream: !!it.stream, engine: !!it.engine, site: !!it.site, maxPages: it.maxPages || 50, follow: !!it.follow, mode: it.mode || "video", title: it.title || "", spotify: it.spotify || null, c });
    }
    // saved before AND the file is still there → skip; gone from the folder → save again
    const saved = await getSaved(cands.map((j) => j.c));
    const altRecs = await getSaved(cands.flatMap((j) => j.alts.map((a) => a.k)));
    for (const j of cands) if (!saved[j.c]) for (const a of j.alts) if (altRecs[a.k] && (!a.mp3 || /\.mp3$/i.test(altRecs[a.k].path || ""))) { saved[j.c] = altRecs[a.k]; log("debug", "dedupe", `older record counts: ${a.k}`, { key: j.c }); break; }
    const prior = cands.filter((j) => saved[j.c]);
    const there = await stillOnDisk(prior.map((j) => saved[j.c].path || ""));
    const keep = new Set(prior.filter((j, i) => there[i] || !saved[j.c].path).map((j) => j.c));
    for (const j of cands) {
      if (keep.has(j.c)) { skipped++; log("info", "dedupe", `skip: saved before, ${saved[j.c].path ? "its file is still at " + saved[j.c].path : "no path was recorded"}`, { key: j.c }); }
      else { jobs.push(j); inFlight.add(j.c); log("info", "dedupe", `queue: ${saved[j.c] ? "saved before but its file is gone (" + (saved[j.c].path || "?") + "), downloading again" : "new"}`, { key: j.c }); }
    }
    let n = prefix ? await nextCounter(dest, typed(prefix), jobs.length) : 0;
    // a stream's name carries no extension: the host names the joined file (.mp4); a site job is named
    // from the hostname, inside saveSiteViaHost
    for (const j of jobs) j.filename = j.engine || j.site ? "" : j.stream ? (prefix ? `${typed(expandDate(prefix, j.date))}_${n++}` : streamName(j.url, j.title)) : filenameFor(j.url, kind, j.mime, { prefix, n: n++, date: j.date });
  });
  log("info", "batch", `queued ${jobs.length}, skipped ${skipped}, unusable ${unusable}`);
  onQueued({ queued: jobs.length, skipped, unusable });
  if (!jobs.length) return;

  if (!progress.active) {
    Object.assign(progress, { total: 0, done: 0, failed: 0, bytes: 0, error: "", errors: [], fails: [], now: [], stopping: false, stopped: false, notStarted: 0, cut: 0 });
    if (!progress.crawl) setBadge("");   // a "!" from the last run's failures
  }
  const ctl = new AbortController(), sig = ctl.signal;
  liveRuns.add(ctl);
  progress.total += jobs.length; progress.active++; progress.where = dest || "Downloads"; progress.kind = kind;
  pushProgress(true);
  const queue = jobs.slice();
  let hadStop = false;
  try {
    await Promise.all(Array.from({ length: 4 }, async () => {   // four files at a time
      while (queue.length && !sig.aborted) {
        const j = queue.shift();
        const label = jobLabel(j), t0 = Date.now();
        progress.now.push(label); pushProgress(false);
        log("info", "job", `start ${label}`, { key: j.c, url: logUrl(j.url), type: j.spotify ? "spotify" : j.engine ? "engine" : j.site ? "site" : j.stream ? "stream" : "file" });
        let r;
        try {
          r = j.spotify ? await spotifyTurn(() => saveEngineViaHost(j.url, "audio", dest, {}, { meta: j.spotify, analyze: !!settings.spotifyBpm, mp3: !!settings.mp3 }, sig), sig) : j.engine ? await saveEngineViaHost(j.url, j.mode, dest, fin, { mp3: j.mode === "audio" && !!settings.mp3 }, sig) : j.site ? await saveSiteViaHost(j.url, j.mode, j.maxPages, dest, fin, j.follow, sig) : j.stream ? await saveStreamViaHost(j.url, j.filename, dest, fin, srcTab, sig) : await saveViaHost(j.url, j.filename, dest, kind, fin, sig);
          if (r.ok) await markSaved(j.c, r.path);
        }
        catch (e) { r = { ok: false, output: String(e?.message || e) }; log("error", "job", `${label} threw: ${e?.stack || e}`); }
        inFlight.delete(j.c);
        const at = progress.now.indexOf(label); if (at >= 0) progress.now.splice(at, 1);
        const secs = ((Date.now() - t0) / 1000).toFixed(1);
        if (r.stopped) {
          // Stop: not a failure. A job still waiting its turn never started; one that had begun was cut off.
          hadStop = true;
          if (r.notStarted || !r.cut) progress.notStarted++; else progress.cut++;
          log("info", "job", `stopped ${label} after ${secs} s (${r.notStarted || !r.cut ? "not started" : "cut off, partial file removed"})`, { key: j.c });
          pushProgress(false);
          if (opts.tabId != null) chrome.tabs.sendMessage(opts.tabId, { type: "saveResult", c: j.c, ok: false, output: "stopped" }).catch(() => {});
          continue;
        }
        if (!r.ok) {
          // every failure is named in the popup with its reason ("song 6 Floaty: YouTube refused the download ..."), never just counted
          const why = j.spotify ? `${label}: ${stripTitle(r.output, j.spotify.title || "")}` : r.output;
          progress.failed++; progress.error = why;
          progress.errors.push(j.spotify ? why : `${label}: ${r.output}`); if (progress.errors.length > 100) progress.errors.shift();
          progress.fails.push({ who: label, why: j.spotify ? stripTitle(r.output, j.spotify.title || "") : String(r.output || "failed") }); if (progress.fails.length > 100) progress.fails.shift();
          log("error", "job", `FAILED ${label} after ${secs} s: ${r.output}`, { key: j.c });
        } else {
          log("info", "job", `done ${label} in ${secs} s -> ${r.path || ""}${r.output ? " — " + r.output : ""}`);
          if (r.output) progress.error = r.output;   // saved, with a caveat (e.g. no ffmpeg: tracks left separate)
        }
        progress.done++; pushProgress(false);
        // the ⬇ button's own flash only reflects "queued", not how the save actually finished (it runs
        // async, after the button already replied) — tell the originating tab the real outcome so a
        // failure that happens after the click is never silent.
        if (opts.tabId != null) chrome.tabs.sendMessage(opts.tabId, { type: "saveResult", c: j.c, ok: r.ok, output: r.output || "" }).catch(() => {});
      }
    }));
  } finally {
    liveRuns.delete(ctl);
    for (const j of queue) inFlight.delete(j.c);
    if (queue.length) { progress.notStarted += queue.length; hadStop = true; log("info", "stop", `${queue.length} queued job(s) never started`); }
    if (hadStop || sig.aborted) progress.stopped = true;
    progress.active--;
    if (![...liveRuns].some((c) => c.signal.aborted)) progress.stopping = false;   // a batch queued after Stop runs on
    if (!progress.active) { progress.stopping = false; endOfRun(jobs.some((j) => j.spotify)); }
    pushProgress(true);
    hostRelease();
  }
}

const jobLabel = (j) => j.spotify ? `song ${j.spotify.number ?? "?"} ${j.spotify.title || ""}`.trim()
  : j.engine ? `${j.mode} ${logUrl(j.url).replace(/^https?:\/\/(www\.)?/, "").slice(0, 60)}`
  : j.site ? `site ${j.mode} ${logUrl(j.url).replace(/^https?:\/\/(www\.)?/, "").slice(0, 50)}`
  : j.stream ? `stream ${j.filename}` : `file ${j.filename}`;
const stripTitle = (out, title) => String(out || "failed").replace(new RegExp("^" + String(title).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ":\\s*"), "");
// the last batch of a run has ended: the log gets the verdict, the badge turns red on any failure, and a Spotify run
// (long, the popup usually closed) raises a notification naming the failures
function endOfRun(hadSpotify) {
  const ok = progress.done - progress.failed;
  if (progress.stopped) log("info", "batch", `run stopped: ${ok} saved, ${progress.failed} failed, ${progress.cut} cut off, ${progress.notStarted} not started of ${progress.total}`);
  else log(progress.failed ? "warn" : "info", "batch", `run finished: ${ok} saved, ${progress.failed} failed of ${progress.total}`, progress.errors.slice(-5));
  if (!progress.failed) { if (!progress.crawl) setBadge(""); return; }
  setBadge("!", "#d33");
  if (progress.stopped) return;   // Stop was pressed: the person is looking, no notification
  if (hadSpotify) notify(ok ? `Jownloader: ${progress.failed} song${progress.failed > 1 ? "s" : ""} failed` : "Jownloader: no song was saved", progress.errors.slice(-3).join("\n"));
}

function notify(title, message) {
  try { chrome.notifications.create({ type: "basic", iconUrl: "icon128.png", title, message: String(message || "").slice(0, 300) }); } catch {}
}

let pickerOpen = false;   // a Finder dialog is up (folder or file picker); one at a time, never queued

// One Finder dialog at a time: a click while one is up (the popup reopened behind it) is refused, never
// queued. The popup closes when the dialog takes focus, so results go to storage and the reopened popup reads them.
async function withPicker(cmd, onResult) {
  if (pickerOpen) return { ok: false, output: "picker already open" };
  pickerOpen = true; chrome.runtime.sendMessage({ type: "picker", open: true }).catch(() => {});
  log("info", "picker", `${cmd}: Finder dialog opened`);
  let r;
  try { r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd }); }
  catch (e) { r = { ok: false, output: hostError(String(e?.message || e)) }; }
  log(r?.ok ? "info" : "warn", "picker", `${cmd}: ${r?.ok ? "chose " + (r.path || (r.files || []).length + " file(s)") : r?.output || "no answer"}`);
  pickerOpen = false; chrome.runtime.sendMessage({ type: "picker", open: false }).catch(() => {});
  if (r?.ok) await onResult(r);
  else if (r?.output && r.output !== "cancelled") notify("Jownloader: Finder dialog failed", r.output);
  return r;
}

// ---------- batch rename ----------
// The picked files live in session storage (the popup is gone by the time the dialog closes).
// name → name_N.ext, numbered oldest file first, counter continuing per target folder like downloads;
// [date] tokens take the file's own modification date. mode "copy" keeps the originals; dest "" = in place.
async function getRenameFiles() { return (await chrome.storage.session.get("renameFiles")).renameFiles || []; }
async function renameBatch(name, dest, mode) {
  const files = (await getRenameFiles()).sort((a, b) => a.mtime - b.mtime || a.path.localeCompare(b.path));
  name = typed(name || "");
  if (!files.length || !name) return { ok: false, output: !name ? "type a name first" : "no files selected" };
  const dirOf = (p) => p.slice(0, p.lastIndexOf("/"));
  const groups = new Map();                       // target folder → files, so each folder numbers on its own
  for (const f of files) { const d = dest || dirOf(f.path); if (!groups.has(d)) groups.set(d, []); groups.get(d).push(f); }
  const ops = [];
  for (const [d, fs] of groups) {
    let n = await nextCounter(d, name, fs.length);
    for (const f of fs) {
      const m = /\.([a-z0-9]{2,5})$/i.exec(f.path);
      ops.push({ src: f.path, dst_dir: dest, name: `${typed(expandDate(name, f.mtime))}_${n++}` + (m ? "." + m[1].toLowerCase() : "") });
    }
  }
  let r;
  try { r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "rename", mode, ops }); }
  catch (e) { r = { ok: false, output: hostError(String(e?.message || e)) }; }
  if (!r?.ok) { log("error", "rename", "failed: " + r?.output); notify("Jownloader: rename failed", r?.output); return r; }
  const done = r.results.filter((x) => x.ok).length, failed = r.results.filter((x) => !x.ok);
  log(failed.length ? "warn" : "info", "rename", `${mode}: ${done} ok, ${failed.length} failed${failed.length ? " — " + failed[0].output : ""}`);
  await chrome.storage.session.remove("renameFiles");
  if (failed.length) notify("Jownloader: rename", `${failed.length} failed — ${failed[0].output}`);
  return { ok: true, done, failed: failed.length, error: failed[0]?.output || "", ops };
}

// ---------- messages ----------
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    const tabId = msg.tabId ?? sender.tab?.id;
    if (msg.type === "download") {
      let replied = false;
      await downloadAll(msg.items, msg.kind, { dest: msg.dest, prefix: msg.prefix, tabId }, (q) => { replied = true; reply(q); });
      if (!replied) reply({ queued: 0, skipped: 0, unusable: 0 });
    } else if (msg.type === "spotifyList") {
      reply(await spotifyList(msg.url));
    } else if (msg.type === "spotifySync") {
      // what is already in the list's folder (renamed to the list's current order) or in an extra library folder
      let r;
      log("info", "spotify", `sync "${msg.folder}": ${msg.tracks?.length} song(s), dest=${msg.dst || "Downloads"}`);
      try { r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "sync", id: "y", dst: msg.dst || "", folder: msg.folder, tracks: msg.tracks, total: msg.total, libs: (await chrome.storage.sync.get({ spotifyLibs: [] })).spotifyLibs }); }
      catch (e) { r = { ok: false, output: hostError(String(e?.message || e)) }; }
      if (r?.ok) log("info", "spotify", `sync "${msg.folder}": ${r.have.length} already in the folder, ${r.inlib.length} in a library, ${r.renamed} renamed`, r.notes?.length ? r.notes : undefined);
      else log("error", "spotify", `sync "${msg.folder}" FAILED: ${r?.output || "native host did not answer"}`);
      reply(r?.ok ? r : { ok: false, output: hostError(r?.output) || "native host did not answer" });
    } else if (msg.type === "addSpotifyLib") {
      reply(await withPicker("choose_dir", async (r) => {
        const { spotifyLibs } = await chrome.storage.sync.get({ spotifyLibs: [] });
        if (!spotifyLibs.includes(r.path)) await chrome.storage.sync.set({ spotifyLibs: [...spotifyLibs, r.path] });
      }));
    } else if (msg.type === "getProgress") {
      reply({ ...progress });
    } else if (msg.type === "stop") {
      reply(stopAll());
    } else if (msg.type === "clearProgress") {      // the dock's dismiss: the last run's verdict is put away
      if (!progress.active) { const hadFail = progress.failed > 0; Object.assign(progress, { total: 0, done: 0, failed: 0, bytes: 0, error: "", errors: [], fails: [], now: [], stopping: false, stopped: false, notStarted: 0, cut: 0 }); if (hadFail && !progress.crawl) setBadge(""); }
      reply({ ok: true });
    } else if (msg.type === "log") {                  // popup / content script events: one file holds every side
      logFrom(sender.url?.startsWith("chrome-extension://") ? "popup" : "content", msg.entries); reply({ ok: true });
    } else if (msg.type === "openLog") {
      await logFlushNow(); reply(await hostOnce({ cmd: "openlog" }));
    } else if (msg.type === "tailLog") {
      await logFlushNow(); reply(await hostOnce({ cmd: "taillog", lines: msg.lines || 200 }));
    } else if (msg.type === "getLog") {                // the in-memory ring (tests, support)
      await logReady; reply(logRing.slice(-(msg.n || 200)));
    } else if (msg.type === "chooseDir") {
      reply(await withPicker("choose_dir", (r) => chrome.storage.sync.set({ dest: r.path })));
    } else if (msg.type === "chooseRenameDest") {
      reply(await withPicker("choose_dir", (r) => chrome.storage.sync.set({ renameDest: r.path })));
    } else if (msg.type === "chooseFiles") {
      reply(await withPicker("choose_files", (r) => chrome.storage.session.set({ renameFiles: r.files })));
    } else if (msg.type === "getRenameFiles") {
      reply(await getRenameFiles());
    } else if (msg.type === "clearRenameFiles") {
      await chrome.storage.session.remove("renameFiles"); reply({ ok: true });
    } else if (msg.type === "rename") {
      reply(await renameBatch(msg.name, msg.dest, msg.mode));
    } else if (msg.type === "finish") {
      let r;
      try { r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "finish", paths: msg.paths || [], strip: !!msg.strip, webp: msg.webp || "" }); }
      catch (e) { r = { ok: false, output: hostError(String(e?.message || e)) }; }
      reply(r);
    } else if (msg.type === "pickerOpen") {
      reply(pickerOpen);
    } else if (msg.type === "clearDir") {
      await chrome.storage.sync.set({ dest: "" }); reply({ ok: true });
    } else if (msg.type === "collect") {
      await addCollected(msg); reply({ ok: true });
    } else if (msg.type === "getCollected") {
      reply(Object.values(await getCollected()));
    } else if (msg.type === "clearCollected") {
      await clearCollected(); reply({ ok: true });
    } else if (msg.type === "downloadCollected") {
      // the batch is what was collected; the list is cleared once it is queued, so nothing saves twice
      const items = Object.values(await getCollected());
      let replied = false;
      await downloadAll(items, "image", { dest: msg.dest, prefix: msg.prefix }, (q) => { replied = true; clearCollected(); reply(q); });
      if (!replied) reply({ queued: 0, skipped: 0, unusable: 0 });
    } else if (msg.type === "forgetSaved") {
      await chrome.storage.local.remove(await savedKeys()); reply({ ok: true });
    } else if (msg.type === "savedCount") {
      reply((await savedKeys()).length);
    } else if (msg.type === "getMedia") {
      reply(await mediaList(tabId));
    } else if (msg.type === "manifestText") {
      await storeManifestText(sender, msg.url, msg.text);
      reply({ ok: true });
    } else if (msg.type === "streamCandidates") {
      reply(await streamsFor(tabId));
    } else if (msg.type === "scan") {
      // ask every frame of the tab for its images/videos/docs, merge
      const images = new Map(), videos = new Map(), docs = new Map();
      let host = "";
      for (const f of await chrome.webNavigation.getAllFrames({ tabId })) {
        let r;
        try { r = await chrome.tabs.sendMessage(tabId, { type: "scanFrame" }, { frameId: f.frameId }); } catch { continue; }
        if (!r) continue;
        if (f.frameId === 0) host = r.host;
        for (const im of r.images) if (!images.has(im.url) || (im.date && !images.get(im.url).date)) images.set(im.url, im);
        for (const v of r.videos) videos.set(v.url, v);
        for (const d of r.docs) docs.set(d.url, d);
      }
      if (!host) { try { host = new URL((await chrome.tabs.get(tabId)).url).hostname; } catch { host = "page"; } }
      reply({ host, images: [...images.values()], videos: [...videos.values()], docs: [...docs.values()], captured: await mediaList(tabId) });
    } else if (msg.type === "getSettings") {
      reply(await chrome.storage.sync.get({ autoVideos: false, showButton: true, collect: false, strip: false, webp: "", dest: "", prefix: "" }));
    } else if (msg.type === "deps") {
      reply(await getDeps());
    } else if (msg.type === "setup") {
      setBadge("…");
      const r = await runSetup();
      setBadge(r.missing?.length ? "!" : "", r.missing?.length ? "#d33" : undefined);
      reply(r);
    } else if (msg.type === "getSetupResult") {
      const { setupResult } = await chrome.storage.session.get("setupResult");
      await chrome.storage.session.remove("setupResult");
      reply(setupResult || null);
    } else {
      reply(null);
    }
  })().catch((e) => { console.error("jownloader message failed", msg?.type, e); try { reply(null); } catch {} });
  return true;   // every branch replies asynchronously
});
