# Real Brave + CDP: the streaming save path end to end (native host, chosen folder, [date] names, dedupe,
# progress). Brave's default "Ask where to save" is left ON — no dialog may appear.
# Run: python3 tests/stream_test_cdp.py [dest-dir] [extra-url]     Trash tests/test-run after.
import os, sys, time, json, subprocess, urllib.request, glob
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = ROOT + "/extension"
PROF = HERE + "/test-run/prof-stream"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
dest = sys.argv[1] if len(sys.argv) > 1 else ""
extra = sys.argv[2] if len(sys.argv) > 2 else ""
where = dest or os.path.expanduser("~/Downloads")
proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", "--remote-debugging-port=9335",
    f"--load-extension={EXT}", "--no-first-run", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
try:
    for _ in range(60):
        try: urllib.request.urlopen("http://127.0.0.1:9335/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9335"); ctx = b.contexts[0]
        pop = ctx.new_page()
        ctx.new_cdp_session(pop).send("Browser.setDownloadBehavior", {"behavior": "default"})   # Playwright would hijack downloads
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html"); pop.wait_for_timeout(2500)
        items = [{"url": "https://picsum.photos/id/40/300/200.jpg", "date": "2025-03-07"}, {"url": "https://picsum.photos/id/41/300/200.jpg"}]
        if extra: items.append({"url": extra})
        def batch():
            r = pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:'image', dest:a.dest, prefix:'jtest-[yy-mm-dd]', items:a.items})", {"dest": dest, "items": items})
            for _ in range(600):
                time.sleep(0.5)
                if not pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).active"): break
            return r, pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getProgress'})")
        r, pr = batch()
        check("batch queued", r, {"queued": len(items), "skipped": 0, "unusable": 0})
        check("all written, none failed", [pr["done"], pr["failed"], pr["error"]], [len(items), 0, ""])
        files = sorted(glob.glob(where + "/jtest-*"))
        print("files:", [f"{os.path.basename(f)} {os.path.getsize(f)}" for f in files])
        check("date-named file present", any(os.path.basename(f).startswith("jtest-25-03-07_") for f in files), True)
        check("no .jownloading left", glob.glob(where + "/jtest-*.jownloading"), [])
        r2, _ = batch()
        check("same batch again → all skipped (files still in the folder)", r2, {"queued": 0, "skipped": len(items), "unusable": 0})
        # trash one file: the next batch saves exactly that one again
        subprocess.run(["/usr/bin/trash", files[0]])
        r3, pr3 = batch()
        check("trashed file → saved again, the other still skipped", [r3, pr3["failed"]], [{"queued": 1, "skipped": len(items) - 1, "unusable": 0}, 0])
        files = sorted(glob.glob(where + "/jtest-*"))
        for f in files: subprocess.run(["/usr/bin/trash", f])
        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    time.sleep(1)
# the real claim: Brave's own download history got nothing (its History DB is readable once Brave has exited)
import sqlite3
n = sqlite3.connect(PROF + "/Default/History").execute("select count(*) from downloads").fetchone()[0]
check("Brave's own download history untouched", n, 0)
print("ALL GREEN" if not fails else f"{fails} FAILED")
