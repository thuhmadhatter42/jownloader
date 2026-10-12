# Real Brave + CDP: Whole-site crawl (its own local fixture site) → one merged PDF named by the first page title
# (or one Markdown file). NOTE: the installed native host answers, so install it from this tree first. Breadth-first same-origin crawl (external links, #fragments, asset links skipped), maxPages
# cap, never-overwrite naming, dedupe by canon(url)+"#site-"+mode regardless of maxPages.
# Run: python3 tests/site_test_cdp.py   (needs pypdf: python3 -m pip install --user pypdf)
# Trash tests/test-run after.
import os, sys, time, base64, subprocess, urllib.request, glob, http.server, threading, functools
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.environ.get("JOWNLOADER_EXT") or ROOT + "/extension"
RUN = HERE + "/test-run"
PROF, FIX, OUT = RUN + "/prof-site", RUN + "/site", RUN + "/site-out"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
PORT = 9610
CDP_PORT = 9611

subprocess.run(["pkill", "-f", RUN + "/prof"])   # zombie test Braves from a previous run

os.makedirs(FIX, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
BROWSER_DL = RUN + "/browser-dl"   # where the browser's OWN download path would land a file (it must stay empty)
os.makedirs(BROWSER_DL, exist_ok=True)

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

HITS = []   # every path the server was asked for, in order
def page(title, body=""):
    return f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>".encode()
def links(*hrefs): return "".join(f'<a href="{h}">{h}</a> ' for h in hrefs)
ROUTES = {   # path -> (status, headers, body)
    "/dlpage": (200, {}, page("Download Page", links("/dl", "/okpage"))),
    "/dl": (200, {"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=evil.bin"}, b"DLSTART evil"),
    "/okpage": (200, {}, page("Ok Page", "<p>fine</p>")),
    "/errstart": (200, {}, page("Err Start", links("/gone", "/redir", "/p/5", "/loop"))),
    "/gone": (404, {}, page("Gone", "<p>nothing here</p>")),
    "/redir": (302, {"Location": "/p/5"}, b""),
    "/p/5": (200, {}, page("Page 5", "<p>five</p>")),
    "/loop": (302, {"Location": "/loop2"}, b""), "/loop2": (302, {"Location": "/loop"}, b""),
    "/danger": (200, {}, page("Danger", links("/logout", "/delete?id=1", "/x?action=drop", "/profile?_wpnonce=abc", "/safe"))),
    "/safe": (200, {}, page("Safe", "<p>safe</p>")),
    "/slash/": (200, {}, page("Slash Home", links("/slash/sub/"))),
    "/slash/sub/": (200, {}, page("Slash Sub", "<p>sub</p>")),
    "/para": (200, {}, page("Para", "<p>Click <a href='/okpage'>here</a> to <b>continue</b>, see <code>foo()</code>.</p><ul><li>one <i>item</i></li></ul>")),
}
for k in "ab":   # two crawls that run side by side, slowly: A has 2 pages, B has 5
    cnt = 2 if k == "a" else 5
    for i in range(cnt):
        ROUTES[f"/c{k}{i}"] = (200, {}, page(f"C{k.upper()}{i}", links(*[f"/c{k}{j}" for j in range(cnt)])))
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=FIX, **kw)
    def log_message(self, *a): pass
    def do_GET(self):
        HITS.append(self.path)
        r = ROUTES.get(self.path.split("?")[0])
        if self.path.startswith("/c") and r: time.sleep(0.8)
        p0 = self.path.split("?")[0]
        if r is None and p0 in ("/logout", "/delete", "/x", "/profile"): r = (200, {}, page("State Changed", "<p>done</p>"))
        if r is None and p0 in ("/slash", "/slash/sub"): r = (404, {}, page("Not here", "<p>nothing here</p>"))
        if r is None: return super().do_GET()
        code, hdrs, body = r
        self.send_response(code)
        if "Content-Type" not in hdrs: self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in hdrs.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", f"--remote-debugging-port={CDP_PORT}",
    f"--load-extension={EXT}", "--no-first-run", "--block-new-web-contents", "--window-position=1200,100", "--window-size=900,600", "about:blank"],
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
        ctx.new_cdp_session(pop).send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": BROWSER_DL})
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
        mdpath = OUT + "/Docs Home.md"
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

        # ---- crawl hardening (findings 23, 55, 54, 60/68, 25, 51, 52, 69): each crawl gets its own folder
        base = f"http://127.0.0.1:{PORT}"
        def crawl(path, mode="pdf", follow=True, sub="x", dest=None, max_pages=20, extra=None):
            d = dest or OUT + "/" + sub
            pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
            del HITS[:]
            items = [{"url": base + p, "site": True, "mode": mode, "maxPages": max_pages, "follow": follow} for p in ([path] if isinstance(path, str) else path)]
            t0 = time.time()
            r = pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:'site', dest:a.dest, prefix:'', items:a.items})", {"dest": d, "items": items})
            samples = []
            for _ in range(600):
                time.sleep(0.1 if extra else 0.5)
                pr = pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getProgress'})")
                if extra: samples.append((pr.get("crawl"), pop.evaluate("async () => await chrome.action.getBadgeText({})")))
                if not pr["active"]: break
            return r, pr, d, time.time() - t0, samples
        def pdf_text(f):
            rd = pypdf.PdfReader(f); return len(rd.pages), " ".join((pg.extract_text() or "") for pg in rd.pages)

        r, pr, d, _, _ = crawl("/dlpage", sub="dl")
        n, txt = pdf_text(d + "/Download Page.pdf") if os.path.exists(d + "/Download Page.pdf") else (None, "")
        check("#23 download link: not followed, 2 pages (page + ok page), no DLSTART, nothing via the browser's download path", [n, "DLSTART" in txt, os.listdir(BROWSER_DL)], [2, False, []])
        r, pr, d, _, _ = crawl("/errstart", sub="err")
        n, txt = pdf_text(d + "/Err Start.pdf") if os.path.exists(d + "/Err Start.pdf") else (None, "")
        check("#55 error pages: 404 + redirect loop skipped, /redir and /p/5 saved once -> 2 pages, no 'nothing here'", [n, "nothing here" in txt, txt.count("Page 5")], [2, False, 1])
        r, pr, d, _, _ = crawl("/danger", sub="danger")
        check("#60/#68 state-changing links (/logout, /delete?id=, ?action=, ?_wpnonce=) are never requested", [h for h in HITS if h != "/danger" and h != "/safe"], [])
        n, _ = pdf_text(d + "/Danger.pdf") if os.path.exists(d + "/Danger.pdf") else (None, "")
        check("#60/#68 ...the safe link still crawled (2 pages)", n, 2)
        r, pr, d, _, _ = crawl("/slash/", sub="slash")
        n, _ = pdf_text(d + "/Slash Home.pdf") if os.path.exists(d + "/Slash Home.pdf") else (None, "")
        check("#54 trailing slash kept: /slash/ and /slash/sub/ requested (never /slash), 2 real pages", [sorted(set(h for h in HITS if h.startswith("/slash"))), n], [["/slash/", "/slash/sub/"], 2])
        r, pr, d, _, _ = crawl("/para", mode="md", follow=False, sub="para")
        md = open(d + "/Para.md", encoding="utf-8").read() if os.path.exists(d + "/Para.md") else ""
        check("#25 markdown: inline elements stay in one paragraph", "Click here to continue, see foo()." in md, True)
        check("#25 markdown: a list item is one line", "- one item" in md, True)
        t0 = time.time()
        r, pr, d, took, _ = crawl("/ca0", mode="md", sub="nope", dest="/System/jown-nope")
        check("#51 markdown crawl into an unusable folder fails before any page is fetched", [pr["failed"], [h for h in HITS if h.startswith("/ca")]], [1, []])
        r, pr, d, _, _ = crawl("/ca0", mode="md", sub="mdname")
        check("#69 multi-page markdown is named by the first page title, like the PDF", sorted(os.listdir(d)), ["CA0.md"])
        r, pr, d, _, samples = crawl(["/ca0", "/cb0"], sub="two", extra=True)
        badges = [bt for _, bt in samples]
        first_blank = next((i for i, bt in enumerate(badges) if bt == ""), None)
        busy = [i for i, (c, _) in enumerate(samples) if c]
        check("#52 badge is never blank while a crawl is still running", [bt for bt in badges[(busy[0] if busy else 0): (busy[-1] + 1 if busy else 0)] if bt == ""], [])
        fr = [c["n"] / c["m"] for c, _ in samples if c]
        check("#52 the progress bar never moves backwards", all(b >= a - 1e-9 for a, b in zip(fr, fr[1:])), True)
        check("#52 both crawls finished", [sorted(os.listdir(d)), pr["failed"]], [["CA0.pdf", "CB0.pdf"], 0])

        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    srv.shutdown()
print("ALL GREEN" if not fails else f"{fails} FAILED")
