// extension/spotify.js (the popup's Spotify block) run in a bare vm with a fake DOM and a fake chrome:
// the list label, the album tag each kind of link gets, what the list check and the queue are sent.
// Run: node tests/spotify_popup_test.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const SRC = fs.readFileSync(path.join(__dirname, "..", "extension", "spotify.js"), "utf8");
let fails = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? "ok  " : "FAIL"} ${label}: ${JSON.stringify(got)}` + (ok ? "" : `   want ${JSON.stringify(want)}`));
};

async function run({ url, list, count, rows }) {
  const els = {};
  const el = (id) => els[id] || (els[id] = { id, textContent: "", disabled: false, hidden: true, checked: false, children: [], append() {} });
  const sent = [], tabMsgs = [];
  const sandbox = {
    document: { getElementById: el, createElement: () => ({ append() {}, set className(v) {}, style: {} }) },
    chrome: {
      storage: { sync: { get: (d, cb) => { const r = Object.assign({}, d); return cb ? cb(r) : Promise.resolve({ dest: "" }); }, set() {} }, onChanged: { addListener() {} } },
      tabs: {
        query: async () => [{ id: 7, url }],
        sendMessage: async (id, m) => { tabMsgs.push(m.type); if (m.type === "spotifyCount") return count; if (m.type === "spotifyRows") return rows || []; return null; },
      },
      runtime: {
        sendMessage: async (m) => {
          sent.push(m);
          if (m.type === "spotifyList") return list;
          if (m.type === "spotifySync") return { ok: true, have: [], inlib: [], renamed: 0, notes: [] };
          if (m.type === "download") return { queued: m.items.length, skipped: 0 };
          return null;
        },
      },
    },
    setTimeout, Promise, Set, console, Number, JSON, parseInt,
  };
  vm.runInNewContext(SRC, sandbox);
  await new Promise((r) => setTimeout(r, 30));
  const go = el("spGo");
  const label = go.textContent;
  await go.onclick();
  return { label, sent, tabMsgs, items: (sent.find((m) => m.type === "download") || {}).items || [], sync: sent.find((m) => m.type === "spotifySync") };
}

const mk = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({ title: "S" + i, artists: "A", album: "", number: i + 1, duration_ms: 1000, cover: "", url: "https://open.spotify.com/track/id" + i, ...extra }));
const PL = "https://open.spotify.com/playlist/abc";

(async () => {
  let r = await run({ url: PL, list: { ok: true, kind: "playlist", name: "Rock", truncated: true, tracks: mk(100) }, count: 100 });
  check("40: a playlist the page says has exactly 100 songs is labeled without the +", r.label, "Download this list as audio (100 songs)");
  check("40: ...and does not scroll the page for more", r.tabMsgs.includes("spotifyRows"), false);
  r = await run({ url: PL, list: { ok: true, kind: "playlist", name: "Rock", truncated: true, tracks: mk(100) }, count: 130, rows: mk(130) });
  check("40: a playlist of 130 is still '100+' until read, and then it is read off the page", [r.label, r.tabMsgs.includes("spotifyRows"), r.items.length], ["Download this list as audio (100+ songs)", true, 130]);
  r = await run({ url: PL, list: { ok: true, kind: "playlist", name: "Rock", truncated: true, tracks: mk(100) }, count: null, rows: mk(100) });
  check("40: when the page can't say, it stays '100+' (safe)", r.label, "Download this list as audio (100+ songs)");

  r = await run({ url: "https://open.spotify.com/track/zzz", list: { ok: true, kind: "track", name: "Bohemian Rhapsody", truncated: false, tracks: mk(1) } });
  check("13/65: a single song's album tag is not its own title", r.items.map((i) => i.spotify.album), [""]);
  check("13/65: ...and a single song goes to no list folder", r.items.map((i) => i.spotify.folder), [""]);
  r = await run({ url: "https://open.spotify.com/artist/zzz", list: { ok: true, kind: "artist", name: "Queen", truncated: false, tracks: mk(3) } });
  check("13: an artist link's songs are not tagged with the artist's name as the album", r.items.map((i) => i.spotify.album), ["", "", ""]);
  r = await run({ url: "https://open.spotify.com/album/zzz", list: { ok: true, kind: "album", name: "Abbey Road", truncated: false, tracks: mk(2, { album: "Abbey Road" }) } });
  check("an album's songs carry the album", r.items.map((i) => i.spotify.album), ["Abbey Road", "Abbey Road"]);
  r = await run({ url: PL, list: { ok: true, kind: "playlist", name: "Rock", truncated: false, tracks: mk(2) } });
  check("a playlist's songs keep the playlist name as album (unchanged)", r.items.map((i) => i.spotify.album), ["Rock", "Rock"]);
  check("the list check is sent each song's link too (symbol-only titles are matched by track id)", r.sync.tracks.map((t) => t.url), ["https://open.spotify.com/track/id0", "https://open.spotify.com/track/id1"]);
  console.log(fails ? `${fails} FAILED` : "ALL GREEN");
  process.exit(fails ? 1 : 0);
})();
