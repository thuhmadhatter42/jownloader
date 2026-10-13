# LIVE end to end, no fakes: real Brave + the extension, the real public Spotify playlist page, the popup's
# "Download this list as audio", the real native host + engine pulling from YouTube, then the popup's Stop.
# Needs network and the installed native host. Modest load: stops after MIN_SAVED songs.
# Run: python3 tests/spotify_live_cdp.py      Trash tests/test-run after.
import os, sys, re, time, glob, subprocess, urllib.request
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = ROOT + "/extension"
PROF = HERE + "/test-run/prof-spotify-live"
OUT = HERE + "/test-run/out"
EXT_ID = open(ROOT + "/native/EXTENSION_ID").read().strip()
PLAYLIST = "https://open.spotify.com/playlist/5mGKqDujgc48cQpqeZMxDv"
LOG = os.path.expanduser("~/Library/Logs/Jownloader/jownloader.log")
MIN_SAVED = 12
PORT = 9338
AUDIO = (".m4a", ".opus")
PARTIAL = (".part", ".jownloading", ".ytdl")
fails = 0
def check(label, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if detail != "" else ""))

def files_under(folder):
    out = []
    for r, _, fs in os.walk(folder):
        out += [os.path.join(r, f) for f in fs]
    return out

os.makedirs(OUT, exist_ok=True)
dest = OUT + "/run-" + time.strftime("%H%M%S"); os.makedirs(dest)
log_off = os.path.getsize(LOG) if os.path.exists(LOG) else 0
t_start = time.time()
proc = subprocess.Popen(["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", f"--user-data-dir={PROF}", f"--remote-debugging-port={PORT}",
    f"--load-extension={EXT}", "--no-first-run", "--block-new-web-contents", "--window-position=1200,100", "--window-size=1000,800", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
saved_n = 0; fail_msgs = []; per_song = None; t_dl = None; stop_prog = {}
try:
    for _ in range(60):
        try: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version"); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}"); ctx = b.contexts[0]
        sp = ctx.new_page()
        mine = {sp}
        # any target we did not open is closed the instant it appears (hard project rule)
        creating = [True]
        ctx.on("page", lambda p2: None if (creating[0] or p2 in mine) else p2.close())
        creating[0] = False
        for pg in list(ctx.pages):
            if pg not in mine: pg.close()
        ctx.new_cdp_session(sp).send("Browser.setDownloadBehavior", {"behavior": "default"})
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        # destination folder as the 📁 picker would have stored it; audio defaults: MP3 off, BPM off
        sw.evaluate("async d => await chrome.storage.sync.set({dest: d, mp3: false, spotifyBpm: false, spotifyLibs: []})", dest)
        sw.evaluate("async () => { const a = await chrome.storage.local.get(null); const k = Object.keys(a).filter(x => x.startsWith('saved:')); if (k.length) await chrome.storage.local.remove(k); }")

        sp.goto(PLAYLIST, wait_until="domcontentloaded")
        rows_ok = False
        for i in range(60):
            sp.wait_for_timeout(1000)
            for sel in ['button:has-text("Accept")', 'button:has-text("Accept cookies")', 'button:has-text("Accept all")', '#onetrust-accept-btn-handler', 'button:has-text("Close")[aria-label*="ookie"]']:
                try:
                    bt = sp.locator(sel).first
                    if bt.count() and bt.is_visible(): bt.click(timeout=1500); print("dismissed banner:", sel)
                except Exception: pass
            if sp.locator('[data-testid="tracklist-row"]').count() > 5: rows_ok = True; break
        check("playlist page shows track rows", rows_ok, sp.locator('[data-testid="tracklist-row"]').count())
        if not rows_ok:
            sp.screenshot(path=HERE + "/test-run/no-rows.png"); raise SystemExit("no rows")
        tab_id = sw.evaluate("async () => (await chrome.tabs.query({url: '*://open.spotify.com/*'}))[0].id")
        sp.bring_to_front()

        creating[0] = True; pop = ctx.new_page(); mine.add(pop); creating[0] = False
        # the popup is opened as a page: its "active tab" lookup must answer with the playlist tab, nothing else is stubbed
        pop.add_init_script("""(() => { const q = chrome.tabs.query.bind(chrome.tabs);
          chrome.tabs.query = async (o) => { const r = await q({url: '*://open.spotify.com/*'}); return o && o.active ? r.slice(0, 1) : r; }; })()""")
        pop.goto(f"chrome-extension://{EXT_ID}/popup.html")
        go = pop.locator("#spGo")
        for _ in range(90):
            pop.wait_for_timeout(1000)
            if go.is_enabled() and "songs" in (go.inner_text() or ""): break
        label = go.inner_text()
        print("button:", label)
        check("popup offers the list download", "Download this list as audio" in label and go.is_enabled(), label)
        go.click()
        t_dl = time.time()

        def prog():
            return pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getProgress'})")
        def saved_files():
            return [f for f in files_under(dest) if f.endswith(AUDIO)]
        deadline = time.time() + 40 * 60
        last = 0
        while time.time() < deadline:
            time.sleep(3)
            n = len(saved_files())
            if n != last: print(f"  {n} files on disk after {int(time.time() - t_dl)} s"); last = n
            pr = prog()
            if n >= MIN_SAVED and any(os.path.getsize(f) > 500_000 for f in saved_files()):
                break
            if pr and not pr.get("active") and pr.get("total"):
                print("run ended early:", pr.get("done"), pr.get("failed"), pr.get("errors")); break
        pr = prog()
        fail_msgs = [f"{f.get('who')}: {f.get('why')}" for f in (pr.get("fails") or [])] or list(pr.get("errors") or [])
        saved_n = (pr.get("done") or 0) - (pr.get("failed") or 0)
        per_song = (time.time() - t_dl) / max(1, len(saved_files()))
        # Stop, by the popup button
        stop = pop.locator("#stop")
        check("Stop button is showing during the run", stop.is_visible())
        t_stop = time.time()
        stop.click()
        pop.wait_for_timeout(1000)
        at_stop = set(files_under(dest))
        new_after = set();
        while time.time() - t_stop < 15:
            time.sleep(1)
            new_after |= set(files_under(dest)) - at_stop
        # the worker may legitimately finish tidying (removing partials) inside the window; files that APPEAR are what we count
        new_final = [f for f in new_after if os.path.exists(f)]
        stop_prog = prog()
        print("after stop:", {k: stop_prog.get(k) for k in ("active", "stopping", "stopped", "done", "failed", "notStarted", "cut")})
        files = files_under(dest)
        good = [f for f in files if f.endswith(AUDIO)]
        big = [f for f in good if os.path.getsize(f) > 500_000]
        names_ok = [f for f in good if re.match(r"^\d{2,3} - Foo Fighters - .+\.(m4a|opus)$", os.path.basename(f))]
        check(f"(1) >= {MIN_SAVED} .m4a/.opus saved", len(good) >= MIN_SAVED, len(good))
        check("(1) each > 500 KB", len(big) == len(good) and good != [], [(os.path.basename(f), os.path.getsize(f)) for f in good if f not in big] or "all")
        check("(1) names like 'NNN - Foo Fighters - Title.ext'", len(names_ok) == len(good) and good != [], [os.path.basename(f) for f in good if f not in names_ok] or "all")
        folders = {os.path.basename(os.path.dirname(f)) for f in good}
        check("(1) saved in a 'Foo Fighters' folder", "Foo Fighters" in folders, sorted(folders))
        check("(2) no new file in the 15 s after Stop", not new_final, [os.path.basename(f) for f in new_final])
        left = [f for f in files if f.endswith(PARTIAL)]
        check("(2) no .part/.jownloading/.ytdl left", not left, [os.path.basename(f) for f in left])
        check("(2) worker reports stopped, nothing active", stop_prog.get("stopped") and not stop_prog.get("active"), {k: stop_prog.get(k) for k in ("active", "stopped")})
        check(f"(3) songs failed before Stop: {len(fail_msgs)}", not fail_msgs, "")
        for m in fail_msgs: print("     failure:", m)
        b.close()
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill(); proc.wait()
    time.sleep(1)
tb = []
if os.path.exists(LOG):
    with open(LOG, "rb") as fh:
        fh.seek(log_off); chunk = fh.read().decode("utf-8", "replace")
    tb = [l for l in chunk.splitlines() if "Traceback" in l]
check("(4) host log lines written during the run have no 'Traceback'", not tb, tb[:3] or "")
print(f"summary: saved {saved_n}, files on disk {len(good) if 'good' in dir() else 0}, failed {len(fail_msgs)}"
      + (f", {per_song:.1f} s per song" if per_song else "") + f", {int(time.time() - t_start)} s total")
print("ALL GREEN" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
