# Real Brave + CDP: HLS/DASH streams end to end — the tab's manifests are found (masters only), the
# best variant's segments are fetched (plain TS, AES-128 TS, fMP4 with a separate audio rendition, DASH
# video+audio), the host joins them with ffmpeg into one .mp4 each, and the saved-URL memory skips a rerun.
# Run: python3 tests/streams_test_cdp.py     (needs ffmpeg + ffprobe)   Trash tests/test-run after.
import re, os, sys, time, json, subprocess, urllib.request, glob, http.server, threading, functools
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.environ.get("JOWN_EXT", ROOT + "/extension")   # JOWN_EXT: run against another build of the extension
RUN = HERE + "/test-run"
PROF, FIX, OUT = RUN + "/prof-streams", RUN + "/streams", RUN + "/streams-out"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
PORT = int(os.environ.get("JOWN_HTTP_PORT", 9336))     # fixture server
CDP = int(os.environ.get("JOWN_CDP_PORT", 9337))        # Brave's debugging port
subprocess.run([HERE + "/make_streams.sh", FIX], check=True)
os.makedirs(OUT, exist_ok=True)
open(FIX + "/page.html", "w").write("""<!doctype html><title>Stream Page - Test Site</title><video id=v></video><script>
(async () => { for (const u of ["hls_fmp4/master.m3u8", "hls_fmp4/v1.m3u8", "hls_fmp4/v10.m4s", "dash/stream.mpd", "dash/init-stream0.m4s"]) await fetch(u);
document.title = "loaded " + document.title; })();</script>""")
# oneshot*/ playlists (.m3u8/.mpd) are signed-URL style: the first GET of a URL is answered, every later one
# (whatever its headers, from whoever) is a 403. hits[path] = [answered, refused].
hits = {}
flaky = {}   # /flaky/ segment path -> GETs seen: index1 = 503 then ok; index2 = 502, dropped connection, ok; index3 = dropped connection, ok
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/oneshot") and re.search(r"\.(m3u8|mpd)$", path):
            h = hits.setdefault(path, [0, 0])
            if h[0]:
                h[1] += 1; self.send_error(403, "token already used"); return
            h[0] += 1
        if path.startswith("/flaky/") and path.endswith(".ts"):
            n = flaky[path] = flaky.get(path, 0) + 1
            if path.endswith("index1.ts") and n == 1: self.send_error(503, "try later"); return
            if path.endswith("index2.ts") and n <= 2:
                if n == 1: self.send_error(502, "bad gateway"); return
                self.close_connection = True; self.connection.shutdown(2); return
            if path.endswith("index3.ts") and n == 1: self.close_connection = True; self.connection.shutdown(2); return
        super().do_GET()
http.server.ThreadingHTTPServer.request_queue_size = 128   # the default backlog of 5 drops connections under load
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Handler, directory=FIX))
threading.Thread(target=srv.serve_forever, daemon=True).start()
proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", f"--remote-debugging-port={CDP}",
    f"--load-extension={EXT}", "--block-new-web-contents", "--no-first-run", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", path], capture_output=True, text=True)
    j = json.loads(p.stdout or "{}")
    return round(float(j.get("format", {}).get("duration", 0)), 1), sorted(s["codec_type"] for s in j.get("streams", []))
base = f"http://127.0.0.1:{PORT}/"
try:
    for _ in range(60):
        try: urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP}"); ctx = b.contexts[0]
        mine = [0]   # 1 while this harness opens a page itself; any other new target (a pop-up) is closed at once
        def closer(pg):
            if not mine[0]:
                try: pg.close()
                except Exception: pass
        ctx.on("page", closer)
        mine[0] = 1; pop = ctx.new_page(); mine[0] = 0
        ctx.new_cdp_session(pop).send("Browser.setDownloadBehavior", {"behavior": "default"})
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html"); pop.wait_for_timeout(2000)
        mine[0] = 1; page = ctx.new_page(); mine[0] = 0; page.goto(base + "page.html"); page.wait_for_function("document.title.startsWith('loaded')"); page.wait_for_timeout(800)
        tab_id = pop.evaluate("async () => (await chrome.tabs.query({url: '*://127.0.0.1/*'}))[0].id")
        cands = pop.evaluate("async id => await chrome.runtime.sendMessage({type:'streamCandidates', tabId:id})", tab_id)
        check("tab's streams = the masters only (variant playlist dropped, segments ignored)", sorted(c["url"].split("/")[-2] + "/" + c["url"].split("/")[-1] for c in cands), ["dash/stream.mpd", "hls_fmp4/master.m3u8"])
        check("candidates flagged as streams", all(c.get("stream") for c in cands), True)
        items = [{"url": base + u, "stream": True, "title": "Stream Page - Test Site"} for u in ["hls_ts/index.m3u8", "hls_aes/index.m3u8", "hls_fmp4/master.m3u8", "dash/stream.mpd"]]
        def batch(prefix, tab=None):
            r = pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:'video', tabId:a.tab, dest:a.dest, prefix:a.prefix, items:a.items})", {"dest": OUT, "prefix": prefix, "items": items, "tab": tab})
            for _ in range(600):
                time.sleep(0.5)
                if not pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).active"): break
            return r, pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getProgress'})")
        r, pr = batch("jstream")
        check("batch queued", r, {"queued": 4, "skipped": 0, "unusable": 0})
        check("all joined, none failed", [pr["done"], pr["failed"], pr["error"]], [4, 0, ""])
        files = sorted(glob.glob(OUT + "/jstream_*"))
        check("four .mp4 files, prefix-numbered", [os.path.basename(f) for f in files], ["jstream_1.mp4", "jstream_2.mp4", "jstream_3.mp4", "jstream_4.mp4"])
        for f in files:
            d, st = probe(f)
            check(f"{os.path.basename(f)}: ~4 s, video + audio", [3.8 <= d <= 4.3, st], [True, ["audio", "video"]])
        check("no temp files left", sorted(os.path.basename(f) for f in glob.glob(OUT + "/*.jownloading") + glob.glob(OUT + "/*.video.*") + glob.glob(OUT + "/*.audio.*")), [])
        r2, _ = batch("jstream")
        check("same streams again → all skipped", r2, {"queued": 0, "skipped": 4, "unusable": 0})
        # no prefix: named from the manifest URL's meaningful path segment, never index/master
        items = items[2:3]; pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
        r3, pr3 = batch("")
        check("unprefixed name from the URL path", [r3["queued"], pr3["failed"], sorted(os.path.basename(f) for f in glob.glob(OUT + "/hls_fmp4*"))], [1, 0, ["hls_fmp4.mp4"]])
        # DRM is refused before a byte is written: a SAMPLE-AES playlist and an MPD with ContentProtection
        os.makedirs(FIX + "/drm", exist_ok=True)
        open(FIX + "/drm/index.m3u8", "w").write("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"skd://x\",KEYFORMAT=\"com.apple.streamingkeydelivery\"\n#EXTINF:1,\n../hls_ts/index0.ts\n#EXT-X-ENDLIST\n")
        open(FIX + "/drm/stream.mpd", "w").write(open(FIX + "/dash/stream.mpd").read().replace('<Representation id="0"', '<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/><Representation id="0"').replace("init-stream", "../dash/init-stream").replace("chunk-stream", "../dash/chunk-stream"))
        items = [{"url": base + "drm/index.m3u8", "stream": True}, {"url": base + "drm/stream.mpd", "stream": True}]
        r4, pr4 = batch("jdrm")
        check("DRM streams refused, nothing written", [r4["queued"], pr4["failed"], "DRM" in pr4["error"], glob.glob(OUT + "/jdrm*")], [2, 2, True, []])
        # one-shot manifests: the page's own player takes the only answer (a fetch() master + its playlists, and an
        # XHR arraybuffer media playlist, as hls.js does); the extension must save from the copy of what the page
        # received and never ask the server again (a second GET would be a 403 and a failed save)
        open(FIX + "/oneshot.html", "w").write("""<!doctype html><title>One Shot</title><script>
        (async () => { const m = await (await fetch("oneshot_fmp4/master.m3u8")).text();
          for (const u of m.match(/[\\w.\\/-]+\\.m3u8/g)) await (await fetch("oneshot_fmp4/" + u)).text();
          await new Promise((ok) => { const x = new XMLHttpRequest(); x.open("GET", "oneshot_ts/index.m3u8"); x.responseType = "arraybuffer"; x.onload = ok; x.send(); });
          document.title = "loaded " + document.title; })();</script>""")
        mine[0] = 1; page2 = ctx.new_page(); mine[0] = 0
        page2.goto(base + "oneshot.html"); page2.wait_for_function("document.title.startsWith('loaded')"); page2.wait_for_timeout(800)
        check("player took every one-shot playlist once", [len(hits) >= 3, all(v == [1, 0] for v in hits.values())], [True, True])
        tab2 = pop.evaluate("async () => (await chrome.tabs.query({url: '*://127.0.0.1/oneshot.html'}))[0].id")
        c2 = pop.evaluate("async id => await chrome.runtime.sendMessage({type:'streamCandidates', tabId:id})", tab2)
        check("one-shot streams classified (masters offered)", sorted(c["url"].split("/")[-2] for c in c2 if c["url"].endswith("master.m3u8")), ["oneshot_fmp4"])
        items = [{"url": base + "oneshot_fmp4/master.m3u8", "stream": True}, {"url": base + "oneshot_ts/index.m3u8", "stream": True}]
        pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
        r5, pr5 = batch("joneshot", tab2)
        check("one-shot batch queued", r5, {"queued": 2, "skipped": 0, "unusable": 0})
        check("one-shot: both saved, none failed", [pr5["done"], pr5["failed"], pr5["error"]], [2, 0, ""])
        check("one-shot files named", sorted(os.path.basename(f) for f in glob.glob(OUT + "/joneshot_*")), ["joneshot_1.mp4", "joneshot_2.mp4"])
        for f in sorted(glob.glob(OUT + "/joneshot_*")):
            d, st = probe(f)
            check(f"{os.path.basename(f)}: ~4 s, video + audio", [3.8 <= d <= 4.3, st], [True, ["audio", "video"]])
        check("extension never asked the server again (0 refused GETs)", sum(v[1] for v in hits.values()), 0)
        # forged copies (a page posting a fake manifest for a URL it does not own) must never reach a save
        segs = sorted(f for f in os.listdir(FIX + "/hls_ts") if f.endswith(".ts"))
        fake = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:%d/hls_ts/%s\n#EXT-X-ENDLIST\n" % (PORT, segs[0])
        legit = base + "hls_ts/index.m3u8"
        # tab X (127.0.0.1) played the real playlist; tab Y is another origin (localhost) and forges a copy for
        # X's URL: a plain window message, and a hand-made port after the real hand-off
        mine[0] = 1; tx = ctx.new_page(); mine[0] = 0
        tx.goto(base + "page.html"); tx.wait_for_function("document.title.startsWith('loaded')")
        tx.evaluate("fetch('hls_ts/index.m3u8').then(r => r.text())"); tx.wait_for_timeout(500)
        forge = "(a) => { postMessage({__jownManifest:1, url:a.u, text:a.t}, '*'); const c = new MessageChannel(); postMessage({__jownInit:1}, '*', [c.port2]); c.port1.postMessage({url:a.u, text:a.t}); }"
        tx.evaluate(forge, {"u": legit, "t": fake})   # same tab: a window message is not the channel
        mine[0] = 1; ty = ctx.new_page(); mine[0] = 0
        ty.goto(f"http://localhost:{PORT}/page.html"); ty.wait_for_function("document.title.startsWith('loaded')")
        ty.evaluate(forge, {"u": legit, "t": fake}); ty.wait_for_timeout(800)
        stored = pop.evaluate("async () => Object.values((await chrome.storage.session.get('manifestTexts')).manifestTexts || {}).map(e => e.text)")
        check("forged copies never stored (window message, hand-made port)", [t for t in stored if "\n#EXTINF:1,\n" in t], [])
        tid_x = pop.evaluate("async () => (await chrome.tabs.query({url: '*://127.0.0.1/page.html'})).pop().id")
        tid_y = pop.evaluate("async () => (await chrome.tabs.query({url: '*://localhost/*'}))[0].id")
        items = [{"url": legit, "stream": True}]
        for lbl, tb in (("saved from the real tab", tid_x), ("saved from the forging tab", tid_y)):
            for f in glob.glob(OUT + "/jforge*"): os.remove(f)
            pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
            rf, prf = batch("jforge", tb)
            fs = glob.glob(OUT + "/jforge_*")
            check(f"forged: {lbl} -> the real ~4 s stream", [prf["failed"], len(fs) == 1 and 3.8 <= probe(fs[0])[0] <= 4.3], [0, True])
        # transient segment failures: a 503; a 502 then a dropped connection; a dropped connection -> retried
        import shutil
        shutil.copytree(FIX + "/hls_ts", FIX + "/flaky", dirs_exist_ok=True)
        items = [{"url": base + "flaky/index.m3u8", "stream": True}]
        pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
        rk, prk = batch("jflaky")
        fk = glob.glob(OUT + "/jflaky_*")
        check("retry: transient 5xx / dropped connections -> saved whole", [prk["done"], prk["failed"], prk["error"], len(fk) == 1 and 3.8 <= probe(fk[0])[0] <= 4.3], [1, 0, "", True])
        check("retry: the flaky segments were asked for again", [flaky.get(k) for k in sorted(flaky) if k.endswith(("index1.ts", "index2.ts", "index3.ts"))], [2, 3, 2])
        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    srv.shutdown()
print("ALL GREEN" if not fails else f"{fails} FAILED")
