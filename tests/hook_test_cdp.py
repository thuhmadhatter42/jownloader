# Real Brave + CDP: the page-world hook (hook.js + hookbridge.js) and the manifest-copy cache in background.js.
# Covers: sandboxed iframes (opaque origin), srcdoc / about:blank frames, the hook being invisible to the page
# (native toString/name/length, one unhandledrejection, no window message), one XHR load listener per object,
# and the byte cap of the copy cache (it must never starve the other session-storage users).
# Forged manifests and the retry on transient segment errors are in streams_test_cdp.py (they need ffmpeg).
# Run: JOWN_HTTP_PORT=9634 JOWN_CDP_PORT=9635 python3 tests/hook_test_cdp.py     Trash tests/test-run after.
import os, sys, time, json, subprocess, urllib.request, http.server, threading
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.environ.get("JOWN_EXT", ROOT + "/extension")
RUN = HERE + "/test-run"
PROF = RUN + "/prof-hook"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
PORT = int(os.environ.get("JOWN_HTTP_PORT", 9634))
CDP = int(os.environ.get("JOWN_CDP_PORT", 9635))
os.makedirs(RUN, exist_ok=True)
MT = "application/vnd.apple.mpegurl"
def manifest(tag, pad=0):
    return "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n" + tag + ".ts\n" + ("#EXTINF:1,\nseg-%s-0123456789abcdef.ts\n" % tag) * pad + "#EXT-X-ENDLIST\n"
PAGES = {
    "/blank.html": "<!doctype html><title>blank</title>",
    "/sandframe.html": "<!doctype html><script>fetch('/m/sand.m3u8').then(r => r.text())</script>",
    "/sand.html": "<!doctype html><title>sand</title><iframe sandbox='allow-scripts' src='/sandframe.html'></iframe>",
    "/srcdoc.html": "<!doctype html><title>srcdoc</title><iframe srcdoc=\"<script>fetch('/m/srcdoc.m3u8').then(r => r.text())</script>\"></iframe>",
    "/sandsrcdoc.html": "<!doctype html><title>sandsrcdoc</title><iframe sandbox='allow-scripts' srcdoc=\"<script>fetch('http://127.0.0.1:%d/m/sandsrcdoc.m3u8').then(r => r.text())</script>\"></iframe>" % PORT,
}
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in PAGES: body, ct = PAGES[path].encode(), "text/html"
        elif path.startswith("/m/big"): body, ct = manifest(path[3:-5], 38000).encode(), MT     # ~1.5 MB
        elif path.startswith("/m/"): body, ct = manifest(path[3:-5]).encode(), MT
        else: self.send_error(404); return
        self.send_response(200); self.send_header("content-type", ct); self.send_header("content-length", str(len(body))); self.send_header("access-control-allow-origin", "*")
        self.end_headers(); self.wfile.write(body)
http.server.ThreadingHTTPServer.request_queue_size = 128
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", f"--remote-debugging-port={CDP}",
    f"--load-extension={EXT}", "--block-new-web-contents", "--no-first-run", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
base = f"http://127.0.0.1:{PORT}"
try:
    for _ in range(60):
        try: urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP}"); ctx = b.contexts[0]
        mine = [0]
        def closer(pg):
            if not mine[0]:
                try: pg.close()
                except Exception: pass
        ctx.on("page", closer)
        mine[0] = 1; pop = ctx.new_page(); mine[0] = 0
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html"); pop.wait_for_timeout(1500)
        def stored():
            return pop.evaluate("async () => Object.entries((await chrome.storage.session.get('manifestTexts')).manifestTexts || {}).map(([k, v]) => [k, v.url || k, v.text.length])")
        def has(name): return any(u.endswith("/m/" + name + ".m3u8") for _, u, _ in stored())
        def page(path):
            mine[0] = 1; pg = ctx.new_page(); mine[0] = 0
            pg.goto(base + path); pg.wait_for_load_state("load"); return pg
        # --- frames the hook must reach
        for path, name, label in (("/sand.html", "sand", "sandboxed iframe (opaque origin, loaded from a URL)"), ("/srcdoc.html", "srcdoc", "srcdoc iframe"), ("/sandsrcdoc.html", "sandsrcdoc", "sandboxed srcdoc iframe")):
            pg = page(path); pg.wait_for_timeout(1500)
            check(f"copy kept from a {label}", has(name), True)
            pg.close()
        pg = page("/blank.html")
        pg.evaluate("() => { const f = document.createElement('iframe'); document.body.appendChild(f); f.contentWindow.fetch('/m/dynblank.m3u8').then(r => r.text()); }")
        pg.wait_for_timeout(1500)
        check("copy kept from a script-created about:blank iframe", has("dynblank"), True)
        # --- the hook is invisible to the page
        pg.evaluate("() => { window.__msgs = []; window.__rej = 0; addEventListener('message', (e) => window.__msgs.push(e.data)); addEventListener('unhandledrejection', () => window.__rej++); }")
        pg.evaluate("() => { fetch('http://127.0.0.1:1/nope'); }"); pg.evaluate("async () => { await fetch('/m/seen.m3u8').then(r => r.text()); }"); pg.wait_for_timeout(1200)
        obs = pg.evaluate("""() => ({
          fetch: [fetch.name, fetch.length, /\\[native code\\]/.test(fetch.toString()), (() => { try { new fetch('/'); return 'constructed'; } catch (e) { return e.constructor.name; } })()],
          open: [XMLHttpRequest.prototype.open.name, XMLHttpRequest.prototype.open.length, /\\[native code\\]/.test(XMLHttpRequest.prototype.open.toString())],
          rej: window.__rej, msgs: window.__msgs.length })""")
        check("fetch looks native (name, length, toString, not constructible)", obs["fetch"], ["fetch", 1, True, "TypeError"])
        check("XHR open looks native", obs["open"], ["open", 2, True])
        check("a failing fire-and-forget fetch raises one unhandledrejection (as without the extension)", obs["rej"], 1)
        check("the page's window gets no message from the hook", obs["msgs"], 0)
        check("...yet the manifest the page fetched was copied", has("seen"), True)
        side = pg.evaluate("() => { let n = 0; const o = EventTarget.prototype.addEventListener; EventTarget.prototype.addEventListener = function (t) { if (t === 'load' && this instanceof XMLHttpRequest) n++; return o.apply(this, arguments); }; const x = new XMLHttpRequest(); for (let i = 0; i < 30; i++) x.open('GET', '/m/xhr.m3u8'); EventTarget.prototype.addEventListener = o; return n; }")
        check("one reused XMLHttpRequest, 30 open() calls -> at most 1 load listener", side <= 1, True)
        # --- the cache's byte cap
        pg.evaluate("async () => { for (let i = 0; i < 6; i++) await (await fetch('/m/big' + i + '.m3u8')).text(); }"); pg.wait_for_timeout(2500)
        ent = stored(); total = sum(n for _, _, n in ent)
        check("six ~1.5 MB manifests -> cache holds at most 2 MB (newest kept)", [total <= 2 * 1024 * 1024, any(u.endswith("big5.m3u8") for _, u, _ in ent), len(ent) < 6 + 6], [True, True, True])
        used = pop.evaluate("async () => await chrome.storage.session.getBytesInUse()")
        check("session storage nowhere near the 10 MB quota", used < 4 * 1024 * 1024, True)
        ok = pop.evaluate("async () => { try { await chrome.storage.session.set({probe: 'x'.repeat(3 * 1024 * 1024)}); await chrome.storage.session.remove('probe'); return true; } catch (e) { return String(e); } }")
        check("another writer can still store 3 MB", ok, True)
        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    srv.shutdown()
print("ALL GREEN" if not fails else f"{fails} FAILED")
