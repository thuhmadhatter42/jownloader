# content.js on a Spotify-like playlist page: the scroll-collect (spotifyRows) keeps document order for a row with
# no aria-rowindex, and spotifyCount reads the song total. Real Chromium + the extension, a local file page, no network.
# Run: PYTHONPATH=/opt/homebrew/lib/python3.14/site-packages /opt/homebrew/bin/python3.14 tests/spotify_rows_test.py
# (fresh tests/test-run first; this harness uses tests/test-run-rows and removes its profile at the end)
import os, shutil
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(os.path.dirname(HERE), "extension")
SP = os.environ.get("JOWNLOADER_TEST_DIR") or HERE + "/test-run-rows"
shutil.rmtree(SP, ignore_errors=True)
os.makedirs(SP + "/prof", exist_ok=True)
fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))

def row(i, idx, title):
    attr = f' aria-rowindex="{idx}"' if idx else ""
    return (f'<div role="row" data-testid="tracklist-row"{attr}><a href="/track/id{i}">{title}</a>'
            f'<a href="/artist/a{i}">Artist {i}</a><a href="/album/b{i}">Album {i}</a><div>3:0{i % 10}</div></div>')

def page(rows, rowcount, header=True):
    head = '<div role="row" aria-rowindex="1"><div role="columnheader">#</div></div>' if header else ""
    return f'<!doctype html><meta charset="utf-8"><title>pl</title><div role="grid" aria-rowcount="{rowcount}">{head}{"".join(rows)}</div>'

pages = {
    # row "Song 3" has no aria-rowindex and sits between rows 3 and 5 on the page
    "missing.html": page([row(1, 2, "Song 1"), row(2, 3, "Song 2"), row(3, 0, "Song 3"), row(4, 5, "Song 4"), row(5, 6, "Song 5")], 6),
    "first.html": page([row(1, 0, "Song 1"), row(2, 3, "Song 2")], 3),
    "none.html": page([row(1, 0, "Song 1"), row(2, 0, "Song 2"), row(3, 0, "Song 3")], 4),
    "count100.html": page([row(1, 2, "Song 1")], 101),
    "count130.html": page([row(1, 2, "Song 1")], 131),
    "nocount.html": '<!doctype html><title>x</title><div>' + row(1, 2, "Song 1") + "</div>",
}
for n, h in pages.items():
    open(f"{SP}/{n}", "w").write(h)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(SP + "/prof", headless=False, channel="chromium",
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--window-position=2000,2000", "--block-new-web-contents"])
    try:
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        ext_id = sw.url.split("/")[2]
        pop = ctx.new_page(); pop.goto(f"chrome-extension://{ext_id}/popup.html"); pop.wait_for_timeout(800)
        def ask(name, msg):
            pg = ctx.new_page(); pg.goto(f"file://{SP}/{name}", wait_until="domcontentloaded"); pg.wait_for_timeout(1500)
            try:
                return pop.evaluate("""async ([name, msg]) => { const t = (await chrome.tabs.query({})).find((x) => (x.url || '').endsWith('/' + name)); return await chrome.tabs.sendMessage(t.id, msg); }""", [name, msg])
            finally:
                pg.close()
        titles = lambda r: [x["title"] for x in r] if isinstance(r, list) else r
        check("39: a row with no aria-rowindex stays between its neighbours (not sorted to the front)", titles(ask("missing.html", {"type": "spotifyRows"})), ["Song 1", "Song 2", "Song 3", "Song 4", "Song 5"])
        check("39: ...also when it is the first row", titles(ask("first.html", {"type": "spotifyRows"})), ["Song 1", "Song 2"])
        check("39: ...and when no row has an index at all (page order)", titles(ask("none.html", {"type": "spotifyRows"})), ["Song 1", "Song 2", "Song 3"])
        check("40: spotifyCount: 101 grid rows with a header = 100 songs", ask("count100.html", {"type": "spotifyCount"}), 100)
        check("40: spotifyCount: 131 grid rows with a header = 130 songs", ask("count130.html", {"type": "spotifyCount"}), 130)
        check("40: spotifyCount: no grid count = null (the popup keeps '100+')", ask("nocount.html", {"type": "spotifyCount"}), None)
    finally:
        ctx.close()
shutil.rmtree(SP, ignore_errors=True)
print("ALL GREEN" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)
