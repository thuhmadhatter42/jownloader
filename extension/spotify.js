// Spotify links in the popup: on a track / album / playlist / artist page, one button turns the whole list
// into tagged audio files, one folder per list. The list itself comes from the host (the link's public embed
// page); a playlist the embed cuts short (it lists 100 at most) is read from the open page by scrolling it.
(() => {
  const SP = /^https?:\/\/open\.spotify\.com\/(?:intl-[a-z]{2,5}(?:-[a-z]{2,4})?\/)?(track|album|playlist|artist)\/[A-Za-z0-9]+/i;
  const el = (id) => document.getElementById(id);
  const say = (t, tone) => { if (typeof globalThis.say === "function") globalThis.say(t, tone); else el("status").textContent = t; };   // the popup's status line (popup.js); alone, the bare element
  const plog = (...a) => { if (typeof jlog === "function") jlog(...a); };   // the developer log (popup.js); absent when this file runs alone
  let list = null, tabId = null;

  chrome.storage.sync.get({ spotifyBpm: false }, (s) => { el("spBpm").checked = !!s.spotifyBpm; });
  el("spBpm").onchange = () => chrome.storage.sync.set({ spotifyBpm: el("spBpm").checked });

  // extra library folders: a song found in <folder>/<list name>/ counts as already downloaded (host side)
  const libName = (p) => (p || "").split("/").filter(Boolean).pop() || p;
  function showLibs(libs) {
    el("spLibs").textContent = "";
    for (const lib of libs) {
      const row = document.createElement("div"); row.className = "lib";
      const name = document.createElement("span"); name.textContent = libName(lib); name.title = lib;
      const x = document.createElement("button"); x.className = "x"; x.title = "Stop counting this folder"; x.setAttribute("aria-label", "Stop counting " + libName(lib));
      x.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-x"/></svg>';
      x.onclick = () => chrome.storage.sync.set({ spotifyLibs: libs.filter((l) => l !== lib) });
      row.append(name, x); el("spLibs").append(row);
    }
  }
  chrome.storage.sync.get({ spotifyLibs: [] }, (s) => showLibs(s.spotifyLibs));
  chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync" && ch.spotifyLibs) showLibs(ch.spotifyLibs.newValue || []); });
  el("spLibAdd").onclick = () => chrome.runtime.sendMessage({ type: "addSpotifyLib" });   // Finder takes focus: the popup closes, the choice is stored

  const label = () => {
    if (!list) return "Download this list as audio";
    const n = list.tracks.length;
    return list.kind === "track" ? "Download this song as audio" : `Download this list as audio (${n}${list.truncated ? "+" : ""} songs)`;
  };

  async function init() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!SP.test(tab?.url || "")) return;
    tabId = tab.id;
    el("spotify").hidden = false;
    el("spGo").textContent = "Reading the list…"; el("spGo").disabled = true;
    list = await chrome.runtime.sendMessage({ type: "spotifyList", url: tab.url }).catch(() => null);
    if (!list?.ok) { el("spGo").textContent = label(); say(list?.output || "native host not reachable — run native/install.sh", "warn"); plog("error", "spotify", "list failed: " + (list?.output || "host not reachable")); list = null; return; }
    el("spName").textContent = list.name || "Music list"; el("spName").title = list.name || "";
    plog("info", "spotify", `list shown: ${list.kind} "${list.name}", ${list.tracks.length} song(s)${list.truncated ? " (may be cut short)" : ""}`);
    if (list.truncated && list.kind === "playlist") {     // exactly 100 songs is not "100+": ask the open page how many it has
      const n = await chrome.tabs.sendMessage(tabId, { type: "spotifyCount" }).catch(() => null);
      if (Number.isInteger(n) && n <= list.tracks.length) list.truncated = false;
    }
    el("spGo").textContent = label(); el("spGo").disabled = false;
    el("spSub").textContent = { track: "Song", album: "Album", playlist: "Playlist", artist: "Artist" }[list.kind] || "";
    if (list.note) say(list.note);
  }

  el("spGo").onclick = async () => {
    el("spGo").disabled = true;
    try {
      let tracks = list.tracks;
      if (list.truncated) {   // the embed page stops at 100: read the rest off the open page
        say("reading the whole playlist off the page…");
        const rows = await chrome.tabs.sendMessage(tabId, { type: "spotifyRows" }).catch(() => null);
        if (rows?.length > tracks.length) tracks = rows.map((r, i) => ({ ...r, number: i + 1, cover: "" }));
        else if (!rows?.length) say("couldn't read the page — saving the first " + tracks.length);
        plog(rows?.length ? "info" : "warn", "spotify", rows?.length ? `read ${rows.length} rows off the open page (embed gave ${list.tracks.length})` : "could not read the rest of the playlist off the page");
      }
      const dest = (await chrome.storage.sync.get({ dest: "" })).dest;
      // songs already in the list's folder (renamed to this order) or in a library folder are not queued again
      let skip = new Set(), extra = "";
      if (list.kind !== "track") {
        const sync = await chrome.runtime.sendMessage({ type: "spotifySync", dst: dest, folder: list.name, total: tracks.length,
          tracks: tracks.map((t) => ({ title: t.title, artists: t.artists, number: t.number, url: t.url })) });
        if (!sync?.ok) { say(sync?.output || "couldn't check the folder"); plog("error", "spotify", "folder check failed: " + (sync?.output || "no answer")); return; }
        skip = new Set([...sync.have, ...sync.inlib]);
        extra = [sync.renamed ? `${sync.renamed} renamed to the new order` : "", sync.inlib.length ? `${sync.inlib.length} found in a library folder` : "", ...sync.notes].filter(Boolean).join(" · ");
      }
      if (list.skipped?.length) extra = [extra, `skipped, not songs: ${list.skipped.join("; ")}`].filter(Boolean).join(" · ");
      const todo = tracks.filter((_, i) => !skip.has(i));
      plog("info", "spotify", `go: ${tracks.length} song(s), ${skip.size} already there, ${todo.length} to download`, { dest: dest || "Downloads", skipped: todo.length ? undefined : "all" });
      if (!todo.length) { say(`all ${tracks.length} songs are already downloaded` + (extra ? `\n${extra}` : "")); return; }
      const items = todo.map((t) => ({
        url: t.url, engine: true, mode: "audio",
        spotify: { title: t.title, artists: t.artists, album: t.album || (list.kind === "album" || list.kind === "playlist" ? list.name : ""), number: t.number, total: tracks.length,
                   duration_ms: t.duration_ms, cover: t.cover || "", url: t.url, folder: list.kind === "track" ? "" : list.name },
      }));
      say(`queuing ${items.length} song${items.length === 1 ? "" : "s"}…` + (extra ? `\n${extra}` : ""));
      const r = await chrome.runtime.sendMessage({ type: "download", items, kind: "song", dest, prefix: "" });
      plog(r?.queued ? "info" : "warn", "spotify", `queued ${r?.queued || 0}, skipped ${r?.skipped || 0} as already saved before`);
      if (!r?.queued) say(r?.skipped ? "already saved before (forget saved to redo)" : "nothing to save");
      else say(`${r.queued} song${r.queued === 1 ? "" : "s"} queued → one at a time, 20 s apart` + (skip.size ? `, ${skip.size} already there` : "") + (extra ? `\n${extra}` : ""));
    } finally { el("spGo").disabled = false; }
  };

  init();
})();
