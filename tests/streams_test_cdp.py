# Real Brave + CDP: HLS/DASH streams end to end — the tab's manifests are found (masters only), the
# best variant's segments are fetched (plain TS, AES-128 TS, fMP4 with a separate audio rendition, DASH
# video+audio), the host joins them with ffmpeg into one .mp4 each, and the saved-URL memory skips a rerun.
# Run: python3 tests/streams_test_cdp.py     (needs ffmpeg + ffprobe)   Trash tests/test-run after.
import os, sys, time, json, subprocess, urllib.request, glob, http.server, threading, functools
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = ROOT + "/extension"
RUN = HERE + "/test-run"
PROF, FIX, OUT = RUN + "/prof-streams", RUN + "/streams", RUN + "/streams-out"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
PORT = 9336
subprocess.run([HERE + "/make_streams.sh", FIX], check=True)
os.makedirs(OUT, exist_ok=True)
open(FIX + "/page.html", "w").write("""<!doctype html><title>Stream Page - Test Site</title><video id=v></video><script>
(async () => { for (const u of ["hls_fmp4/master.m3u8", "hls_fmp4/v1.m3u8", "hls_fmp4/v10.m4s", "dash/stream.mpd", "dash/init-stream0.m4s"]) await fetch(u);
document.title = "loaded " + document.title; })();</script>""")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIX))
srv.log_message = lambda *a: None; http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", "--remote-debugging-port=9337",
    f"--load-extension={EXT}", "--no-first-run", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
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
        try: urllib.request.urlopen("http://127.0.0.1:9337/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9337"); ctx = b.contexts[0]
        pop = ctx.new_page()
        ctx.new_cdp_session(pop).send("Browser.setDownloadBehavior", {"behavior": "default"})
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html"); pop.wait_for_timeout(2000)
        page = ctx.new_page(); page.goto(base + "page.html"); page.wait_for_function("document.title.startsWith('loaded')"); page.wait_for_timeout(800)
        tab_id = pop.evaluate("async () => (await chrome.tabs.query({url: '*://127.0.0.1/*'}))[0].id")
        cands = pop.evaluate("async id => await chrome.runtime.sendMessage({type:'streamCandidates', tabId:id})", tab_id)
        check("tab's streams = the masters only (variant playlist dropped, segments ignored)", sorted(c["url"].split("/")[-2] + "/" + c["url"].split("/")[-1] for c in cands), ["dash/stream.mpd", "hls_fmp4/master.m3u8"])
        check("candidates flagged as streams", all(c.get("stream") for c in cands), True)
        items = [{"url": base + u, "stream": True, "title": "Stream Page - Test Site"} for u in ["hls_ts/index.m3u8", "hls_aes/index.m3u8", "hls_fmp4/master.m3u8", "dash/stream.mpd"]]
        def batch(prefix):
            r = pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:'video', dest:a.dest, prefix:a.prefix, items:a.items})", {"dest": OUT, "prefix": prefix, "items": items})
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
        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    srv.shutdown()
print("ALL GREEN" if not fails else f"{fails} FAILED")
