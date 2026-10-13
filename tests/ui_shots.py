# Popup / side-panel screenshots of every state, light and dark, with the extension loaded in Playwright's Chromium.
# The page's own messages to the worker (scan, deps, the music list) are stubbed per state, so each shot shows one
# state exactly; nothing reaches the network or a native host. Run: python3 tests/ui_shots.py [out-dir]
# Shots land in design/screens/ (or the folder given). Trash tests/test-run after.
import json, os, sys
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.environ.get("JOWNLOADER_EXT") or os.path.join(ROOT, "extension")
SP = os.environ.get("JOWNLOADER_TEST_DIR") or HERE + "/test-run"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "design", "screens")
os.makedirs(SP + "/shots-prof", exist_ok=True); os.makedirs(OUT, exist_ok=True)

ALL_DEPS = {k: {"present": True} for k in ("yt-dlp", "ffmpeg", "gallery-dl", "exiftool", "pypdf", "analyzer")}
def scan(host, ni, nv, nd):
    return {"host": host, "images": [{"url": f"https://{host}/p/{i}.jpg", "date": ""} for i in range(ni)],
            "videos": [{"url": f"https://{host}/v/{i}.mp4", "date": ""} for i in range(nv)],
            "docs": [{"url": f"https://{host}/d/{i}.pdf"} for i in range(nd)], "captured": []}
GALLERY = {"url": "https://photos.example.org/album/harbour", "scan": scan("photos.example.org", 48, 3, 2), "deps": {"ok": True, "deps": ALL_DEPS, "hints": {}}, "collected": 6}
VIDEO = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "scan": scan("www.youtube.com", 12, 0, 0), "deps": {"ok": True, "deps": ALL_DEPS, "hints": {}}, "collected": 0}
LIST = {"url": "https://open.spotify.com/playlist/37i9dQZF1DX0XUsuxWHRQd", "scan": scan("open.spotify.com", 31, 0, 0), "deps": {"ok": True, "deps": ALL_DEPS, "hints": {}}, "collected": 0,
        "list": {"ok": True, "kind": "playlist", "name": "Late Night Drive", "truncated": False, "tracks": [{"title": f"Song {i}", "artists": "Artist", "number": i + 1, "url": f"https://open.spotify.com/track/{i}"} for i in range(42)]}}
NOHOST = {**GALLERY, "deps": {"ok": False, "output": "native host not installed — run native/install.sh"}}
MISSING = {**VIDEO, "deps": {"ok": True, "deps": {**ALL_DEPS, "yt-dlp": {"present": False}, "analyzer": {"present": False}},
           "hints": {"yt-dlp": "not installed: click Install missing dependencies, or by hand: pipx install yt-dlp", "analyzer": "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"}}}

RUNNING = {"total": 48, "done": 19, "failed": 2, "bytes": 61_300_000, "active": 1, "where": "/data/Pictures/Harbour shoot", "kind": "image", "error": "",
           "errors": ["file IMG_0042.jpg: HTTP 403", "file IMG_0057.jpg: stalled: no data for 60 s"], "now": ["file IMG_0060.jpg", "file IMG_0061.jpg", "file IMG_0062.jpg", "file IMG_0063.jpg"], "crawl": None}
FAILED = {"total": 9, "done": 9, "failed": 8, "bytes": 8_100_000, "active": 0, "where": "", "kind": "song", "error": "x", "now": [], "crawl": None,
          "errors": [f"song {i} Track {i}: YouTube refused the download (format not available)." for i in range(1, 8)] + ["song 9 Track 9: no upload is within 15 s of the song's length"],
          "fails": [{"who": f"song {i} Track {i}", "why": "YouTube refused the download (format not available)."} for i in range(1, 8)] + [{"who": "song 9 Track 9", "why": "no upload is within 15 s of the song's length"}]}
STOPPED = {"total": 48, "done": 20, "failed": 1, "bytes": 64_000_000, "active": 0, "where": "/data/Pictures/Harbour shoot", "kind": "image", "error": "",
           "errors": ["file IMG_0042.jpg: HTTP 403"], "now": [], "crawl": None, "stopped": True, "notStarted": 25, "cut": 2}
CRAWL = {"total": 1, "done": 0, "failed": 0, "bytes": 0, "active": 1, "where": "", "kind": "site", "error": "", "errors": [], "now": [], "crawl": {"n": 7, "m": 20, "url": "https://docs.example.org/guide/install"}}
BIGRUN = {**RUNNING, "total": 412, "done": 233, "failed": 9, "errors": RUNNING["errors"]}

# stubs installed before popup.js runs: chrome.tabs.query → the state's tab, chrome.runtime.sendMessage → canned answers
STUB = """(() => {
  const M = JSON.parse(%s);
  if (!sessionStorage.getItem('shotInit')) { sessionStorage.setItem('shotInit', '1'); try { localStorage.clear(); } catch {} }
  const realSend = chrome.runtime.sendMessage.bind(chrome.runtime);
  chrome.tabs.query = async () => [{ id: 4242, url: M.url, title: 'Page', windowId: 1 }];
  chrome.tabs.sendMessage = async () => null;
  chrome.runtime.sendMessage = async (m) => {
    switch (m && m.type) {
      case 'scan': return M.scan;
      case 'deps': return M.deps;
      case 'getCollected': return Array.from({ length: M.collected || 0 }, (_, i) => ({ url: 'https://x/' + i }));
      case 'spotifyList': return M.list || { ok: false, output: 'no list' };
      case 'getProgress': return M.progress || { total: 0 };
      case 'savedCount': return 128;
      case 'pickerOpen': return false;
      case 'getSetupResult': return null;
      case 'getRenameFiles': return [];
      case 'streamCandidates': return [];
      case 'log': return { ok: true };
      default: return realSend(m).catch(() => null);
    }
  };
})();"""

def shot(ctx, ext_id, name, state, scheme="light", panel=False, tab=None, prog=None, open_details=False, width=360):
    page = ctx.new_page()
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 600})
    page.add_init_script(STUB % json.dumps(json.dumps({**state, "progress": prog})))
    page.goto(f"chrome-extension://{ext_id}/popup.html" + ("?panel=1" if panel else ""))
    page.wait_for_timeout(700)
    if tab: page.click("#" + tab); page.wait_for_timeout(150)
    if open_details: page.evaluate("() => document.querySelectorAll('details').forEach(d => d.open = true)")
    if prog: page.evaluate("pr => showProgress(pr)", prog)
    page.wait_for_timeout(400)
    if not panel:   # a popup is as tall as its content, at most 600
        page.set_viewport_size({"width": width, "height": 100}); page.wait_for_timeout(100)
        h = page.evaluate("() => document.documentElement.scrollHeight")
        page.set_viewport_size({"width": width, "height": min(600, max(120, h))})
    else:
        page.set_viewport_size({"width": width, "height": 860})
    page.wait_for_timeout(250)
    path = os.path.join(OUT, f"{name}-{scheme}.png")
    page.screenshot(path=path)
    page.close()
    print("shot", path)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(SP + "/shots-prof", headless=False, channel="chromium",
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--window-position=2000,2000", "--block-new-web-contents"])
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
    ext_id = sw.url.split("/")[2]
    for pg in ctx.pages:
        if not pg.url.startswith("about:blank"): pg.close()
    sw.evaluate("() => chrome.storage.sync.set({ dest: '/data/Pictures/Harbour shoot', spotifyLibs: ['/data/Music Library'], prefix: '' })")
    for scheme in ("light", "dark"):
        shot(ctx, ext_id, "01-idle-page", GALLERY, scheme)
        shot(ctx, ext_id, "02-video-link", VIDEO, scheme)
        shot(ctx, ext_id, "03-music-list", LIST, scheme)
        shot(ctx, ext_id, "04-running", GALLERY, scheme, prog=RUNNING)
        shot(ctx, ext_id, "05-failures", LIST, scheme, prog=FAILED)
        shot(ctx, ext_id, "06-stopped", GALLERY, scheme, prog=STOPPED)
        shot(ctx, ext_id, "07-no-host", NOHOST, scheme)
        shot(ctx, ext_id, "08-deps-missing", MISSING, scheme)
        shot(ctx, ext_id, "09-pdf-view", GALLERY, scheme, tab="tabPdf")
        shot(ctx, ext_id, "10-tools-view", MISSING, scheme, tab="tabTools")
        shot(ctx, ext_id, "11-options-open", GALLERY, scheme, open_details=True)
        shot(ctx, ext_id, "12-crawl", GALLERY, scheme, tab="tabPdf", prog=CRAWL)
        shot(ctx, ext_id, "13-side-panel-running", VIDEO, scheme, panel=True, prog=BIGRUN, width=420)
    ctx.close()
