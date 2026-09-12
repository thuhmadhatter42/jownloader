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

// ---------- filenames ----------
function sanitize(s) { return s.replace(/[^\w.\-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 120); }
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

// ---------- native host ----------
// One persistent port streams file bytes (open / chunk… / close per file, several files interleaved);
// it is closed when no batch is running so the host process exits. One-shot jobs (the Finder picker, the
// exists check) use sendNativeMessage, which spawns their own host process — the picker would otherwise
// block the streaming port while the dialog is up.
let hostPort = null, hostSeq = 0;
const hostWaiters = new Map();   // message id -> resolve
function hostConnect() {
  if (hostPort) return hostPort;
  const p = chrome.runtime.connectNative(NATIVE_HOST);
  p.onMessage.addListener((m) => { const w = hostWaiters.get(m.id); if (w) { hostWaiters.delete(m.id); w(m); } });
  p.onDisconnect.addListener(() => {
    hostPort = null;
    const err = chrome.runtime.lastError?.message || "native host disconnected";
    for (const [id, w] of hostWaiters) { hostWaiters.delete(id); w({ id, ok: false, output: err }); }
  });
  return (hostPort = p);
}
function hostRelease() { if (hostPort && !progress.active) { try { hostPort.disconnect(); } catch {} hostPort = null; } }
function hostAsk(port, msg) {
  return new Promise((resolve) => {
    hostWaiters.set(msg.id, resolve);
    try { port.postMessage(msg); } catch (e) { hostWaiters.delete(msg.id); resolve({ ok: false, output: String(e?.message || e) }); }
  });
}
const NO_HOST = /not found|forbidden|disconnected/i;
const hostError = (out) => NO_HOST.test(out || "") ? "native host not installed — run native/install.sh" : out;

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
// Fetch url with the browser session and stream it to the host as name inside dest ("" = ~/Downloads).
// Flow-controlled: one chunk in flight per file, the host acks each before the next is sent, so memory
// stays bounded and every message resets the worker's idle timer. Resolves {ok, path} or {ok:false, output}.
// Never throws.
async function saveViaHost(url, name, dest, kind) {
  const id = "s" + (++hostSeq);
  let stallTimer = 0;
  const read = (reader) => Promise.race([
    reader.read(),
    new Promise((_, rej) => { stallTimer = setTimeout(() => rej(new Error(`stalled: no data for ${STALL_MS / 1000} s`)), STALL_MS); }),
  ]).finally(() => clearTimeout(stallTimer));
  let res, reader, first;
  try {
    res = await fetch(url, { credentials: "include" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    reader = res.body.getReader();
    first = await read(reader);   // first chunk before "open": it decides the extension when nothing else does
  } catch (e) { try { reader?.cancel(); } catch {} return { ok: false, output: String(e?.message || e) }; }
  if (first.value && looksEncrypted(first.value)) { try { reader.cancel(); } catch {} return { ok: false, output: "protected video (DRM) — encrypted file, not saved" }; }
  if (!hasExt(name)) {
    const ext = extForMime(res.headers.get("content-type")) || (first.value ? sniffExt(first.value) : "") || { video: "mp4", image: "jpg", doc: "pdf" }[kind] || "";
    if (ext) name += "." + ext;
  }
  const port = hostConnect();
  const opened = await hostAsk(port, { cmd: "open", id, name, dst: dest || "" });
  if (!opened.ok) { try { reader.cancel(); } catch {} return { ok: false, output: hostError(opened.output) }; }
  try {
    let buf = new Uint8Array(0);
    const send = async (part) => {
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
    const closed = await hostAsk(port, { cmd: "close", id });
    return closed.ok ? closed : { ok: false, output: hostError(closed.output) };
  } catch (e) {
    try { reader.cancel(); } catch {}
    try { port.postMessage({ cmd: "abort", id }); } catch {}
    return { ok: false, output: String(e?.message || e) };
  }
}

// ---------- progress (the popup's bar) ----------
const progress = { total: 0, done: 0, failed: 0, bytes: 0, active: 0, where: "", kind: "", error: "" };
let progTimer = 0;
function pushProgress(force) {
  if (progTimer && !force) return;
  progTimer = setTimeout(() => { progTimer = 0; chrome.runtime.sendMessage({ type: "progress", ...progress }).catch(() => {}); }, force ? 0 : 150);
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
  const settings = await chrome.storage.sync.get({ dest: "", prefix: "" });
  const dest = opts.dest ?? settings.dest, prefix = opts.prefix ?? settings.prefix;
  // dedupe within the batch by canonical URL, and against everything ever saved
  const jobs = [], seen = new Set();
  let skipped = 0, unusable = 0;
  await serial(async () => {
    const cands = [];
    for (const raw of items) {
      const it = typeof raw === "string" ? { url: raw } : raw;
      if (!it.url || !/^https?:/.test(it.url)) { unusable++; continue; }
      const c = canon(it.url);
      if (seen.has(c) || inFlight.has(c)) { skipped++; continue; }
      seen.add(c); cands.push({ url: it.url, mime: it.ctype || "", date: it.date || "", c });
    }
    // saved before AND the file is still there → skip; gone from the folder → save again
    const saved = await getSaved(cands.map((j) => j.c));
    const prior = cands.filter((j) => saved[j.c]);
    const there = await stillOnDisk(prior.map((j) => saved[j.c].path || ""));
    const keep = new Set(prior.filter((j, i) => there[i] || !saved[j.c].path).map((j) => j.c));
    for (const j of cands) { if (keep.has(j.c)) skipped++; else { jobs.push(j); inFlight.add(j.c); } }
    let n = prefix ? await nextCounter(dest, typed(prefix), jobs.length) : 0;
    for (const j of jobs) j.filename = filenameFor(j.url, kind, j.mime, { prefix, n: n++, date: j.date });
  });
  onQueued({ queued: jobs.length, skipped, unusable });
  if (!jobs.length) return;

  if (!progress.active) Object.assign(progress, { total: 0, done: 0, failed: 0, bytes: 0, error: "" });
  progress.total += jobs.length; progress.active++; progress.where = dest || "Downloads"; progress.kind = kind;
  pushProgress(true);
  const queue = jobs.slice();
  try {
    await Promise.all(Array.from({ length: 4 }, async () => {   // four files at a time
      while (queue.length) {
        const j = queue.shift();
        let r;
        try { r = await saveViaHost(j.url, j.filename, dest, kind); if (r.ok) await markSaved(j.c, r.path); }
        catch (e) { r = { ok: false, output: String(e?.message || e) }; }
        inFlight.delete(j.c);
        if (!r.ok) { progress.failed++; progress.error = r.output; console.warn("save failed", j.url, r.output); }
        progress.done++; pushProgress(false);
      }
    }));
  } finally {
    for (const j of queue) inFlight.delete(j.c);
    progress.active--; pushProgress(true);
    hostRelease();
  }
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
  let r;
  try { r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd }); }
  catch (e) { r = { ok: false, output: hostError(String(e?.message || e)) }; }
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
  if (!r?.ok) { notify("Jownloader: rename failed", r?.output); return r; }
  const done = r.results.filter((x) => x.ok).length, failed = r.results.filter((x) => !x.ok);
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
      await downloadAll(msg.items, msg.kind, { dest: msg.dest, prefix: msg.prefix }, (q) => { replied = true; reply(q); });
      if (!replied) reply({ queued: 0, skipped: 0, unusable: 0 });
    } else if (msg.type === "getProgress") {
      reply({ ...progress });
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
      reply(await chrome.storage.sync.get({ autoVideos: false, showButton: true, collect: false, dest: "", prefix: "" }));
    } else {
      reply(null);
    }
  })().catch((e) => { console.error("jownloader message failed", msg?.type, e); try { reply(null); } catch {} });
  return true;   // every branch replies asynchronously
});
