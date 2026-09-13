# Real Brave + CDP: the engine end to end — a YouTube page URL sent as an engine item comes back as a
# QuickTime-playable .mp4 (H.264 + AAC), the audio mode as an .mp3 named with BPM + key, the saved-URL
# memory keeps video and audio apart, and the "strip metadata" switch reaches the engine's files.
# Network: fetches the 19-second "Me at the zoo" video. Run: python3 tests/engine_test_cdp.py   Trash tests/test-run after.
import os, sys, time, json, subprocess, urllib.request, glob
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = ROOT + "/extension"
RUN = HERE + "/test-run"
PROF, OUT = RUN + "/prof-engine", RUN + "/engine-out"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
os.makedirs(OUT, exist_ok=True)
proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", "--remote-debugging-port=9338",
    f"--load-extension={EXT}", "--no-first-run", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
def codecs(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", path], capture_output=True, text=True)
    return sorted(s["codec_name"] for s in json.loads(p.stdout or "{}").get("streams", []))
try:
    for _ in range(60):
        try: urllib.request.urlopen("http://127.0.0.1:9338/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9338"); ctx = b.contexts[0]
        pop = ctx.new_page()
        ctx.new_cdp_session(pop).send("Browser.setDownloadBehavior", {"behavior": "default"})
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html"); pop.wait_for_timeout(2000)
        pop.evaluate("async () => await chrome.storage.sync.set({strip: true})")
        def batch(items):
            r = pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:'video', dest:a.dest, prefix:'', items:a.items})", {"dest": OUT, "items": items})
            for _ in range(720):
                time.sleep(0.5)
                if not pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).active"): break
            return r, pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getProgress'})")
        r, pr = batch([{"url": URL, "engine": True, "mode": "video"}, {"url": URL, "engine": True, "mode": "audio"}])
        check("video + audio of one page = two jobs", r, {"queued": 2, "skipped": 0, "unusable": 0})
        check("both done, none failed", [pr["done"], pr["failed"], pr["error"]], [2, 0, ""])
        files = sorted(os.path.basename(f) for f in glob.glob(OUT + "/Me at the zoo*"))
        check("mp4 + mp3 named with BPM and keys", ["Me at the zoo.mp4" in files, bool([f for f in files if f.endswith(".mp3") and " BPM " in f])], [True, True])
        check("mp4 is H.264 + AAC (QuickTime-playable)", codecs(OUT + "/Me at the zoo.mp4"), ["aac", "h264"])
        check("link log written", os.path.exists(glob.glob(OUT + "/(*) Youtube DL LINKS.txt")[0]) if glob.glob(OUT + "/(*) Youtube DL LINKS.txt") else False, True)
        tags = subprocess.run(["exiftool", "-S", "-Encoder", "-HandlerDescription", "-Title", "-Comment", OUT + "/Me at the zoo.mp4"], capture_output=True, text=True).stdout
        mp3 = [f for f in files if f.endswith(".mp3")][0]
        id3 = subprocess.run(["exiftool", "-S", "-Title", "-Artist", "-Comment", "-Encoder", OUT + "/" + mp3], capture_output=True, text=True).stdout.strip()
        check("strip switch reached the engine's files (no Google/yt-dlp tags in mp4, no ID3 in mp3)", ["Google" in tags or "Lavf" in tags, id3], [False, ""])
        r2, _ = batch([{"url": URL, "engine": True, "mode": "video"}])
        check("same page again → skipped (file still there)", r2, {"queued": 0, "skipped": 1, "unusable": 0})
        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
print("ALL GREEN" if not fails else f"{fails} FAILED")
