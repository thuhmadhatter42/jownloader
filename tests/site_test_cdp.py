# Real Brave + CDP: Whole-site crawl (its own local fixture site) → one merged PDF named by the first page title
# (or one Markdown file). NOTE: the installed native host answers, so install it from this tree first. Breadth-first same-origin crawl (external links, #fragments, asset links skipped), maxPages
# cap, never-overwrite naming, dedupe by canon(url)+"#site-"+mode regardless of maxPages.
# Run: python3 tests/site_test_cdp.py   (needs pypdf: python3 -m pip install --user pypdf)
# Trash tests/test-run after.
import os, sys, time, base64, subprocess, urllib.request, glob, http.server, threading, functools
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = ROOT + "/extension"
RUN = HERE + "/test-run"
PROF, FIX, OUT = RUN + "/prof-site", RUN + "/site", RUN + "/site-out"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
PORT = 9350
CDP_PORT = 9351

subprocess.run(["pkill", "-f", RUN + "/prof"])   # zombie test Braves from a previous run

os.makedirs(FIX, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

# 1x1 transparent PNG, well-known bytes
open(FIX + "/photo.png", "wb").write(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
open(FIX + "/index.html", "w").write(
    "<!doctype html><html><head><title>Docs Home</title></head><body>"
    "<h1>Docs Home</h1>"
    '<a href="a.html">Page A</a>'
    '<a href="b.html">Page B</a>'
    '<a href="c.html">Page C</a>'
    '<a href="https://example.invalid/should-not-be-visited">External</a>'
    '<a href="#section">Jump</a>'
    '<a href="photo.png">Photo</a>'
    "</body></html>")
for slug, title in [("a", "Page A"), ("b", "Page B"), ("c", "Page C")]:
    open(FIX + f"/{slug}.html", "w").write(
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f"<h1>{title}</h1><p>Some plain text on {title}.</p></body></html>")

srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIX))
srv.log_message = lambda *a: None; http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()

proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", f"--remote-debugging-port={CDP_PORT}",
    f"--load-extension={EXT}", "--no-first-run", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))

def ensure_pypdf():
    def importable():
        return subprocess.run([sys.executable, "-c",
            "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pypdf') else 1)"]).returncode == 0
    if importable():
        return True
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "pypdf"])
    return importable()

start = f"http://127.0.0.1:{PORT}/index.html"
try:
    for _ in range(60):
        try: urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}"); ctx = b.contexts[0]
        pop = ctx.new_page()
        ctx.new_cdp_session(pop).send("Browser.setDownloadBehavior", {"behavior": "default"})
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html"); pop.wait_for_timeout(2000)

        def batch(mode, max_pages, forget=False, follow=True):
            if forget:
                pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
            items = [{"url": start, "site": True, "mode": mode, "maxPages": max_pages, "follow": follow}]
            r = pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:'site', dest:a.dest, prefix:'', items:a.items})",
                              {"dest": OUT, "items": items})
            for _ in range(600):
                time.sleep(0.5)
                if not pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).active"): break
            return r, pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getProgress'})")

        # 1. PDF crawl, maxPages 50 -> one merged 4-page PDF (index, a, b, c)
        r, pr = batch("pdf", 50)
        check("pdf crawl queued", r, {"queued": 1, "skipped": 0, "unusable": 0})
        check("pdf crawl: none failed", pr["failed"], 0)
        if pr["failed"]:
            print("   progress.error:", pr.get("error"))
        import pypdf
        f1 = OUT + "/Docs Home.pdf"
        check("follow-links crawl: ONE merged PDF named by the first page title", sorted(os.path.basename(f) for f in glob.glob(OUT + "/*.pdf")), ["Docs Home.pdf"])
        check("merged PDF has 4 pages", len(pypdf.PdfReader(f1).pages) if os.path.exists(f1) else None, 4)
        check("a crawl-progress badge never lingers", pr.get("crawl"), None)

        # 2. Markdown crawl, maxPages 50 -> one .md with all four titles in crawl order, no leaked external domain
        r2, pr2 = batch("md", 50)
        check("md crawl queued", r2, {"queued": 1, "skipped": 0, "unusable": 0})
        check("md crawl: none failed", pr2["failed"], 0)
        mdpath = OUT + "/127.0.0.1.md"
        check("md file exists", os.path.exists(mdpath), True)
        if os.path.exists(mdpath):
            md = open(mdpath, encoding="utf-8").read()
            offsets = [md.find(t) for t in ["Docs Home", "Page A", "Page B", "Page C"]]
            check("all four page titles present", all(o >= 0 for o in offsets), True)
            check("titles appear in crawl order (index, a, b, c)", offsets, sorted(offsets))
            check("external domain text never leaked into the file", "example.invalid" in md, False)

        # 3. maxPages 2, after forgetSaved -> a NEW folder (127.0.0.1 is still on disk), 2 pages
        r3, pr3 = batch("pdf", 2, forget=True)
        check("maxPages=2 crawl queued", r3, {"queued": 1, "skipped": 0, "unusable": 0})
        check("maxPages=2 crawl: none failed", pr3["failed"], 0)
        f2 = OUT + "/Docs Home (2).pdf"
        check("second PDF (never overwrote the first), 2 pages", len(pypdf.PdfReader(f2).pages) if os.path.exists(f2) else None, 2)

        # 4. rerun of check 1 verbatim, no forgetSaved -> deduped against the still-on-disk folder
        r4, pr4 = batch("pdf", 50)
        check("rerun of check 1 is deduped, not a new crawl", r4, {"queued": 0, "skipped": 1, "unusable": 0})

        # 5. "This page only" (no follow): exactly the start page, as <title>.pdf; a second run never overwrites
        r5, pr5 = batch("pdf", 50, forget=True, follow=False)
        check("this-page-only crawl queued, none failed", [r5, pr5["failed"]], [{"queued": 1, "skipped": 0, "unusable": 0}, 0])
        check("page-only save is <title>.pdf, 1 page, next free name", [os.path.exists(OUT + "/Docs Home (3).pdf") and len(pypdf.PdfReader(OUT + "/Docs Home (3).pdf").pages)], [1])
        batch("pdf", 50, forget=True, follow=False)
        check("second page-only run -> (4)", os.path.exists(OUT + "/Docs Home (4).pdf"), True)

        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    srv.shutdown()
print("ALL GREEN" if not fails else f"{fails} FAILED")
