# Jownloader harness — Playwright Chromium with the extension loaded. No native host can be reached from a
# Playwright browser, so this covers scanning, naming, dedupe and the loud no-host failure; actual writes
# are covered by stream_test_cdp.py (real Brave). Run: python3 tests/ext_test.py   Trash tests/test-run after.
import os, json
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.environ.get("JOWNLOADER_EXT") or os.path.join(os.path.dirname(HERE), "extension")
SP = os.environ.get("JOWNLOADER_TEST_DIR") or HERE + "/test-run"
os.makedirs(SP + "/prof", exist_ok=True)
fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(SP + "/prof", headless=False, channel="chromium",
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--window-position=2000,2000"])
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
    ext_id = sw.url.split("/")[2]
    page = ctx.new_page()
    page.goto("file://" + HERE + "/testpage.html", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # overlay button: present before play, stays after pause, labeled with the best variant
    btn = page.query_selector("button[data-jownloader]")
    check("button before play (better variant offered = site's label)", btn.text_content() if btn else None, "⬇ Download \"1080p\" (site's label)")
    page.evaluate("document.getElementById('v').play()"); page.wait_for_timeout(2000)
    page.evaluate("document.getElementById('v').pause()"); page.wait_for_timeout(500)
    btn = page.query_selector("button[data-jownloader]")
    check("button after pause", btn.text_content() if btn else None, "⬇ Download \"1080p\" (site's label)")

    # a script variant that is the same file under a bigger name is not a better variant: label = decoded pixels
    page.evaluate("document.getElementById('same').play()"); page.wait_for_timeout(2500)
    labels = [b.text_content() for b in page.query_selector_all("button[data-jownloader]")]
    check("same-file '2160p' variant → real pixels of what plays", any(l.startswith("⬇ Download ") and "×" in l and "label" not in l for l in labels), True)
    pop = ctx.new_page(); pop.goto(f"chrome-extension://{ext_id}/popup.html"); pop.wait_for_timeout(1500)
    # (the popup opened as a tab scans itself, so counts are 0 here; the scan is checked against the test page below)
    check("popup controls present (0.1.45 layout)", pop.evaluate("""() => ['sidebar', 'dest', 'destClear', 'spGo', 'spLibAdd', 'tabDl', 'tabPdf', 'tabTools', 'site', 'siteAudio', 'imgs', 'vids', 'docs', 'coll', 'stop', 'run']
      .filter((id) => !document.getElementById(id))"""), [])
    check("save rows: label + count, the row is the button", pop.evaluate("() => ['imgs', 'vids', 'docs', 'coll'].map((id) => document.getElementById(id).querySelector('.t').textContent)"), ["Images", "Videos", "Documents", "Collected"])
    check("destination row shows the folder name", pop.evaluate("() => document.getElementById('destName').textContent"), "Downloads")
    # views: every control lives in its own view
    SHOT = os.environ.get("JOWNLOADER_SHOTS") or SP
    os.makedirs(SHOT, exist_ok=True)
    pop.set_viewport_size({"width": 320, "height": 720})
    where = lambda ids: pop.evaluate("ids => ids.map(id => { let e = document.getElementById(id); while (e && !/^pane/.test(e.id)) e = e.parentElement; return e ? e.id : null; })", ids)
    check("view 1 This page holds everything saved from the page + the finishing switches", where(["site", "siteAudio", "imgs", "vids", "docs", "coll", "collect", "autoVideos", "showButton", "prefix", "strip", "webp", "spGo", "spBpm"]), ["paneDl"] * 14)
    check("view 2 PDF / Markdown holds the crawl controls", where(["sitePdf", "siteMd", "sitePages"]), ["panePdf"] * 3)
    check("view 3 Tools holds rename, clean-up, dependencies, log", where(["rnName", "rnPick", "rnGo", "rnStrip", "rnWebp", "rnPng", "depsInstall", "logOpen", "forget"]), ["paneTools"] * 9)
    check("the run dock (with Stop) sits outside every view", where(["run", "stop", "bar", "runHead"]), [None] * 4)
    check("view labels", [pop.text_content(i) for i in ("#tabDl", "#tabPdf", "#tabTools")], ["This page", "PDF / Markdown", "Tools"])
    check("only This page shows first", pop.evaluate("() => ['paneDl','panePdf','paneTools'].map(i => document.getElementById(i).hidden)"), [False, True, True])
    check("view switcher fits on one line", pop.evaluate("() => [...document.querySelectorAll('.seg button')].map(b => Math.round(b.getBoundingClientRect().height) < 30)"), [True, True, True])
    check("views are a tablist (roles, aria-selected)", pop.evaluate("() => [document.querySelector('.seg').getAttribute('role'), ...['tabDl','tabPdf','tabTools'].map(i => document.getElementById(i).getAttribute('aria-selected'))]"), ["tablist", "true", "false", "false"])
    pop.focus("#tabDl"); pop.keyboard.press("ArrowRight"); pop.wait_for_timeout(100)
    check("arrow key moves to the next view and focuses it", pop.evaluate("() => [document.activeElement.id, document.getElementById('panePdf').hidden]"), ["tabPdf", False])
    pop.keyboard.press("Home"); pop.wait_for_timeout(100)
    check("Home goes back to the first view", pop.evaluate("() => document.activeElement.id"), "tabDl")
    pop.screenshot(path=SHOT + "/popup-tab1-video-images.png")
    pop.click("#tabPdf"); pop.wait_for_timeout(200)
    check("PDF tab shown, others hidden", pop.evaluate("() => ['paneDl','panePdf','paneTools'].map(i => document.getElementById(i).hidden)"), [True, False, True])
    check("Max pages has a visible label", pop.evaluate("""() => { const l = document.querySelector('label[for=sitePages]'); const r = l.getBoundingClientRect();
      return [l.textContent.trim().startsWith('Max pages'), r.width > 0 && r.height > 0 && getComputedStyle(l).visibility === 'visible']; }"""), [True, True])
    check("crawl toggle: This page only is the default", pop.evaluate("""() => [[...document.querySelectorAll('input[name=crawl]')].map(r => r.parentElement.textContent.trim()),
      document.querySelector('input[name=crawl]:checked').value, document.getElementById('sitePages').disabled]"""),
          [["This page only", "Follow links on this site"], "page", True])
    pop.click("input[name=crawl][value=site]"); pop.wait_for_timeout(200)
    check("Follow links enables Max pages", pop.evaluate("() => document.getElementById('sitePages').disabled"), False)
    pop.screenshot(path=SHOT + "/popup-tab2-pdf.png")
    pop.click("input[name=crawl][value=page]")
    pop.click("#tabTools"); pop.wait_for_timeout(200)
    check("Tools: section headings", pop.evaluate("() => [...document.querySelectorAll('#paneTools h2')].map(h => h.textContent)"), ["Rename files", "Clean up the selected files", "Dependencies", "Log"])
    pop.screenshot(path=SHOT + "/popup-tab3-extra-tools.png")
    pop.click("#tabDl"); pop.wait_for_timeout(200)
    check("Collect switch wording", pop.evaluate("() => document.querySelector('#collect').closest('label').querySelector('small').textContent"), "every picture shown on screen is remembered; thumbnails ignored")
    check("hidden attribute still wins over button display", pop.evaluate("() => { const b = document.getElementById('site'); return [b.hidden, getComputedStyle(b).display]; }"), [True, "none"])
    # crawl progress: a crawl in flight shows page N of M + the URL
    pop.evaluate("() => showProgress({total:1, done:0, failed:0, bytes:0, active:1, where:'Downloads', kind:'site', error:'', crawl:{n:3, m:12, url:'https://example.test/a/b'}})")
    check("crawl progress text", pop.evaluate("() => [document.getElementById('runHead').textContent, document.getElementById('runMeta').textContent, document.getElementById('run').hidden]"), ["Page 3 of 12", "https://example.test/a/b", False])
    check("crawl progress bar", pop.evaluate("() => document.getElementById('fill').style.width"), "25%")
    check("Stop shows while it runs", pop.evaluate("() => document.getElementById('stop').hidden"), False)
    pop.evaluate("() => { document.getElementById('run').hidden = true; }")

    # the popup's first message must answer (a throw here hung the popup at "scanning…" once)
    first = pop.evaluate("""async () => Promise.race([new Promise(r => setTimeout(() => r('TIMEOUT'), 3000)),
      chrome.runtime.sendMessage({type:'getSettings'}).then(s => typeof s.showButton)])""")
    check("first popup message answers", first, "boolean")

    tabs = sw.evaluate("async () => (await chrome.tabs.query({})).map(t => [t.id, t.url])")
    tab_id = [t for t in tabs if "testpage" in (t[1] or "")][0][0]
    res = pop.evaluate("async id => await chrome.runtime.sendMessage({type:'scan', tabId:id})", tab_id)
    check("scan counts", {k: len(res[k]) for k in ("images", "videos", "docs")}, {"images": 9, "videos": 4, "docs": 1})   # captured varies with cache
    check("image dates (nearest <time>, else none)", sorted((i["url"].split("/")[4], i["date"]) for i in res["images"]), [("10", "2025-03-07"), ("20", ""), ("40", ""), ("40", ""), ("50", ""), ("51", ""), ("52", ""), ("60", ""), ("70", "2021-07-28")])
    check("video date (page has none)", [v.get("date") for v in res["videos"]], ["", "", "", ""])
    check("videos = best variant per player (KVS flashvars, <source size>, JW-style sources) + the same-file one as it plays; the muted-loop preview is not a video",
          sorted(v["url"].split("/")[-1] for v in res["videos"]), ["clip.mp4", "flower.mp4", "friday.mp4?t=1", "movie.mp4"])
    check("four ⬇ buttons (none on the preview loop)", len(page.query_selector_all("button[data-jownloader]")), 4)
    check("<source size> and JW-style variants labeled with the site's claim", sorted(b.text_content() for b in page.query_selector_all("button[data-jownloader]"))[:3], ['⬇ Download "1080p" (site\'s label)'] * 3)

    # collect-as-you-scroll: only pictures rendered ≥ 300 px on screen, the wrapping full-size link preferred
    check("nothing collected while off", pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getCollected'})).length"), 0)
    page.bring_to_front()   # a background tab's timers are throttled; the page tab is the one being looked at
    page.wait_for_function("[...document.images].every(i => i.complete)", timeout=30000)   # picsum can be slow
    pop.evaluate("() => chrome.storage.sync.set({collect: true})"); page.wait_for_timeout(300)
    page.evaluate("scrollTo(0, 1)"); page.wait_for_timeout(600)
    page.evaluate("scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(600)
    coll = pop.evaluate("async () => await chrome.runtime.sendMessage({type:'getCollected'})")
    check("collected = big pictures only (no thumbs, no upscaled 200px one), link href for the wrapped one", sorted(c["url"].split("photos/")[1] for c in coll), ["id/10/400/300.jpg", "id/20/400/300.jpg", "id/40/1200/900.jpg", "id/70/400/300.jpg"])
    check("collected lightbox picture carries the header date", [c["date"] for c in coll if "id/70" in c["url"]], ["2021-07-28"])
    pop.evaluate("() => chrome.storage.sync.set({collect: false})")
    check("popup collected button", pop.evaluate("async () => { await new Promise(r => setTimeout(r, 300)); const b = document.getElementById('coll'); return [b.querySelector('.n').textContent, b.disabled, b.getAttribute('aria-label')]; }"), ["4", True, "Save all 4 collected pictures"])   # no host in this browser: every save control is off
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'clearCollected'})")
    # sized variants in one folder: the biggest wins in either arrival order
    got = pop.evaluate("""async () => { const send = (u) => chrome.runtime.sendMessage({type:'collect', url:u});
      await send('https://cdn.x/files/1/10/abc/300x300_h1.jpg?sig=1'); await send('https://cdn.x/files/1/10/abc/1536x2048_h2.jpg?sig=2');
      await send('https://cdn.x/files/2/20/def/1242x1659_h3.jpg'); await send('https://cdn.x/files/2/20/def/300x300_h4.jpg');
      await send('https://cdn.x/files/3/30/ghi/300x300_h5.jpg');
      return (await chrome.runtime.sendMessage({type:'getCollected'})).map(c => c.url.split('/').pop()).sort(); }""")
    check("biggest sized variant per folder", got, ["1242x1659_h3.jpg", "1536x2048_h2.jpg?sig=2", "300x300_h5.jpg"])
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'clearCollected'})")
    check("cleared", pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getCollected'})).length"), 0)
    pop.bring_to_front()

    # sidebar = the same page in the side panel; panel.html lands on popup.html?panel=1 in panel layout
    pan = ctx.new_page(); pan.goto(f"chrome-extension://{ext_id}/panel.html"); pan.wait_for_timeout(1500)
    check("panel page", [pan.url.split("/")[-1], pan.evaluate("document.body.className"), pan.evaluate("getComputedStyle(document.getElementById('sidebar')).display")], ["popup.html?panel=1", "panel", "none"])
    check("sidePanel API present", sw.evaluate("() => typeof chrome.sidePanel?.open"), "function")
    pan.close()

    names = sw.evaluate("""() => [
      filenameFor('https://picsum.photos/id/10/400/300.jpg','image',''),
      filenameFor('https://cdn.site.com/media/clip.mp4?x=1','video',''),
      filenameFor('https://host.com/dl/manual','doc','application/pdf'),
      filenameFor('https://site.example/get_file/3/x/123_1080p.mp4/?v-acctoken=1','video','',{prefix:'clip', n:2}),
      filenameFor('https://x.com/a/b.jpg','image','',{prefix:'shoot-[yy-mm-dd]', n:3, date:'2025-03-07'}),
      filenameFor('https://x.com/a/b.jpg','image','',{prefix:'[mm-d-yyyy]', n:1, date:'2025-03-07'}),
      filenameFor('https://x.com/a/b.jpg','image','',{prefix:'[jan-d-yyyy]', n:1, date:'2025-03-07'}),
      filenameFor('https://x.com/a/b.jpg','image','',{prefix:'[yy-january-dd]', n:1, date:'2025-03-07'}),
      filenameFor('https://x.com/a/b.jpg','image','',{prefix:'[Jan D YYYY]', n:1, date:'2025-03-07T12:00:00Z'}),
      filenameFor('https://x.com/a/b.jpg','image','',{prefix:'[yyyy]', n:1, date:''}) === (new Date().getFullYear() + '_1.jpg'),
      filenameFor('https://evil.example/.zshrc','doc',''),
      filenameFor('https://evil.example/..%2F..%2F.netrc','doc',''),
    ]""")
    check("filenames", names, ['300.jpg', 'clip.mp4', 'manual.pdf', 'clip_2.mp4',
          'shoot-25-03-07_3.jpg', '03-7-2025_1.jpg', 'mar-7-2025_1.jpg', '25-march-07_1.jpg', 'Mar 7 2025_1.jpg', True, 'zshrc', 'netrc'])

    # a batch with no reachable host: queued, then every file fails with the install hint — never silent
    r = pop.evaluate("""async () => await chrome.runtime.sendMessage({type:'download', kind:'image', prefix:'beat', items:[
      {url:'https://picsum.photos/id/30/300/200.jpg'}, {url:'https://picsum.photos/id/30/300/200.jpg?token=abc'}, {url:'blob:x'}]})""")
    check("batch reply (dup + blob)", r, {"queued": 1, "skipped": 1, "unusable": 1})
    pr = pop.evaluate("async () => { for (let i = 0; i < 40; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active) return p; await new Promise(r => setTimeout(r, 250)); } return 'still active'; }")
    check("no host → loud failure", [pr["done"], pr["failed"], "install.sh" in (pr.get("error") or "")], [1, 1, True])
    # the developer log saw it all: dedupe decisions, the job's start and its failure with the reason
    lg = sw.evaluate("async () => (await new Promise(r => setTimeout(r, 200)), logRing.map(e => `${e.l} ${e.a} ${e.m}`))")
    check("log: dedupe decisions (same item twice, non-http URL) are recorded", [any(l.startswith("info dedupe skip: the same item twice") for l in lg), any(l.startswith("info dedupe unusable") for l in lg)], [True, True])
    check("log: the failing job is recorded with its reason (error level)", any(l.startswith("error job FAILED file") and "install.sh" in l for l in lg), True)
    check("log: the batch start and queue verdict are recorded", [any(l.startswith("info batch start: 3 item(s), kind=image") for l in lg), any(l.startswith("info batch queued 1, skipped 1, unusable 1") for l in lg)], [True, True])
    check("counter advanced", sw.evaluate("async () => (await chrome.storage.local.get('counter:|beat'))['counter:|beat']"), 1)
    # saved-for-good memory: a URL in the saved list is skipped however long ago it was saved
    pop.evaluate("async () => chrome.storage.local.set({'saved:https://picsum.photos/id/31/300/200.jpg': {ts: 1, path: ''}})")
    r = pop.evaluate("async () => await chrome.runtime.sendMessage({type:'download', kind:'image', prefix:'beat', items:[{url:'https://picsum.photos/id/31/300/200.jpg?sig=new'}]})")
    check("saved long ago → skipped", r, {"queued": 0, "skipped": 1, "unusable": 0})
    # two batches at once never share a number
    r2 = pop.evaluate("""async () => { const go = (u) => chrome.runtime.sendMessage({type:'download', kind:'image', prefix:'beat', items:[{url:u}]});
      await Promise.all([go('https://picsum.photos/id/32/300/200.jpg'), go('https://picsum.photos/id/33/300/200.jpg')]);
      for (let i = 0; i < 40; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active) break; await new Promise(r => setTimeout(r, 250)); }
      return (await chrome.storage.local.get('counter:|beat'))['counter:|beat']; }""")
    check("concurrent batches → counter 3", r2, 3)
    check("forget saved", pop.evaluate("async () => { await chrome.runtime.sendMessage({type:'forgetSaved'}); return await chrome.runtime.sendMessage({type:'savedCount'}); }"), 0)
    enc = "AAAAGGZ0eXBpc29tAAAAAGlzb21kYXNoAAAA" # ftyp isom … then a pssh box name in the header
    check("DRM bytes refused", sw.evaluate("() => looksEncrypted(new TextEncoder().encode('....ftypisom........moov....pssh....'))"), True)
    check("DRM check survives a full-size chunk", sw.evaluate("() => looksEncrypted(new Uint8Array(3 * 1024 * 1024))"), False)
    check("plain mp4 accepted", sw.evaluate("() => looksEncrypted(new TextEncoder().encode('....ftypisom........moov....mvhd....'))"), False)
    # one folder picker at a time: with no host the first call fails fast and the second must not have overlapped
    pk = pop.evaluate("""async () => { const a = chrome.runtime.sendMessage({type:'chooseDir'}); const open = await chrome.runtime.sendMessage({type:'pickerOpen'});
      const ra = await a; return [open, ra.ok, await chrome.runtime.sendMessage({type:'pickerOpen'})]; }""")
    check("picker flag: up while the dialog call runs, clear after", pk, [True, False, False])
    pk2 = pop.evaluate("""async () => { const a = chrome.runtime.sendMessage({type:'chooseFiles'}); const b = await chrome.runtime.sendMessage({type:'chooseRenameDest'});
      const ra = await a; return [b.output, ra.ok, await chrome.runtime.sendMessage({type:'pickerOpen'})]; }""")
    check("file picker: a second Finder call while one is up is refused, not queued", pk2, ["picker already open", False, False])
    check("rename with nothing selected fails loud", pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'rename', name:'x', dest:'', mode:'rename'})).output"), "no files selected")
    check("rename controls present", pop.evaluate("() => [!!document.getElementById('tabTools'), document.getElementById('rnGo').disabled, document.getElementById('rnDest').disabled]"), [True, True, True])
    check("YouTube tracks are never whole files", sw.evaluate("() => [isWhole('https://rr1---sn-abc.googlevideo.com/videoplayback?itag=18', 'video/mp4'), isWhole('https://cdn.x/a.mp4', 'video/mp4')]"), [False, True])
    check("sniffed extensions", sw.evaluate("() => [sniffExt(new Uint8Array([0,0,0,0x18,0x66,0x74,0x79,0x70,0x69,0x73,0x6f,0x6d,0,0])), sniffExt(new Uint8Array([0xff,0xd8,0xff,0xe0,0,0,0,0,0,0,0,0,0])), sniffExt(new Uint8Array([0x1a,0x45,0xdf,0xa3,0,0,0,0,0,0,0,0,0])), sniffExt(new Uint8Array(13))]"), ["mp4", "jpg", "webm", ""])
    # Spotify links: the BPM/key checkbox (present, off by default, persists in sync storage), the song items
    # (one engine item per song, folder in the dedupe key) and the host message they ride on. No network, no host.
    check("spotify block hidden off Spotify pages; checkbox present and off by default",
          pop.evaluate("() => [document.getElementById('spotify').hidden, !!document.getElementById('spBpm'), document.getElementById('spBpm').checked]"), [True, True, False])
    pop.evaluate("() => { const c = document.getElementById('spBpm'); c.checked = true; c.dispatchEvent(new Event('change')); }")
    pop.wait_for_timeout(300)
    check("checkbox persists to chrome.storage.sync", pop.evaluate("async () => (await chrome.storage.sync.get({spotifyBpm: false})).spotifyBpm"), True)
    pop2 = ctx.new_page(); pop2.goto(f"chrome-extension://{ext_id}/popup.html"); pop2.wait_for_timeout(800)
    check("a reopened popup shows it on", pop2.evaluate("() => document.getElementById('spBpm').checked"), True)
    pop2.close()
    sw.evaluate("() => { globalThis.__sent = []; globalThis.__orig = saveEngineViaHost; saveEngineViaHost = async (...a) => { __sent.push(a); return { ok: true, path: '' }; }; }")
    song = lambda url, folder, n: {"url": url, "engine": True, "mode": "audio", "spotify": {"title": "T", "artists": "A", "album": "L", "number": n, "total": 2, "duration_ms": 1000, "cover": "", "url": url, "folder": folder}}
    r = pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'song', dest:'/d', prefix:'', items})",
                     [song("https://open.spotify.com/track/aaa", "List", 1), song("https://open.spotify.com/track/aaa?si=x", "List", 1)])
    check("song items: the same song twice in one list = one", r, {"queued": 1, "skipped": 1, "unusable": 0})
    pop.wait_for_timeout(500)
    sent = sw.evaluate("() => __sent")
    check("host message: audio mode, destination, meta (folder, number) and analyze=true ride on the song",
          [[a[0], a[1], a[2], a[4]["analyze"], a[4]["meta"]["folder"], a[4]["meta"]["number"]] for a in sent], [["https://open.spotify.com/track/aaa", "audio", "/d", True, "List", 1]])
    pop.evaluate("() => chrome.storage.sync.set({spotifyBpm: false})")
    r = pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'song', dest:'/d', prefix:'', items})", [song("https://open.spotify.com/track/aaa", "Other list", 1)])
    check("the same song in another list's folder is its own file", r["queued"], 1)
    pop.wait_for_timeout(500)   # (its turn starts when the 20 s gap after the first has passed — the stub answers at once either way)
    check("extra library folders: none by default", pop.evaluate("async () => (await chrome.storage.sync.get({spotifyLibs: null})).spotifyLibs"), None)
    pop.evaluate("() => chrome.storage.sync.set({spotifyLibs: ['/some/Music Library']})"); pop.wait_for_timeout(400)
    check("a stored library folder shows by its name, with a remove button", pop.evaluate("() => [document.getElementById('spLibs').textContent, document.querySelectorAll('#spLibs .x').length]"), ["Music Library", 1])
    pop.evaluate("() => document.querySelector('#spLibs .x').click()"); pop.wait_for_timeout(400)
    check("removing it empties the setting", pop.evaluate("async () => (await chrome.storage.sync.get({spotifyLibs: null})).spotifyLibs"), [])
    sy = pop.evaluate("async () => await chrome.runtime.sendMessage({type:'spotifySync', dst:'', folder:'L', tracks:[], total:0})")
    check("list check with no host fails loud (the popup then queues nothing)", [sy["ok"], "install.sh" in sy["output"]], [False, True])
    # "Download audio as MP3": two checkboxes (Spotify block + under Audio only), one sync key `mp3`, off by default
    check("mp3 switch: present twice, off by default", pop.evaluate("() => [...document.querySelectorAll('input.mp3')].map(b => [b.id, b.checked])"), [["siteMp3", False], ["spMp3", False]])
    check("mp3 switch: the video-link one sits right after the Audio only button", pop.evaluate("() => document.getElementById('siteAudio').nextElementSibling.id"), "mp3Row")
    pop.evaluate("() => { const c = document.getElementById('siteMp3'); c.checked = true; c.dispatchEvent(new Event('change')); }")
    pop.wait_for_timeout(300)
    check("mp3 switch: persists to sync storage under `mp3`", pop.evaluate("async () => (await chrome.storage.sync.get({mp3: false})).mp3"), True)
    check("mp3 switch: toggling one updates the other", pop.evaluate("() => document.getElementById('spMp3').checked"), True)
    pop.evaluate("() => { const c = document.getElementById('spMp3'); c.checked = false; c.dispatchEvent(new Event('change')); }")
    pop.wait_for_timeout(300)
    check("mp3 switch: ...and back, both ways", pop.evaluate("async () => [document.getElementById('siteMp3').checked, (await chrome.storage.sync.get({mp3: false})).mp3]"), [False, False])
    pop.evaluate("() => chrome.storage.sync.set({mp3: true})"); pop.wait_for_timeout(300)
    check("mp3 switch: a change made elsewhere reaches both boxes", pop.evaluate("() => [...document.querySelectorAll('input.mp3')].map(b => b.checked)"), [True, True])
    pop2 = ctx.new_page(); pop2.goto(f"chrome-extension://{ext_id}/popup.html"); pop2.wait_for_timeout(800)
    check("mp3 switch: a reopened popup shows it on in both places", pop2.evaluate("() => [...document.querySelectorAll('input.mp3')].map(b => b.checked)"), [True, True])
    pop2.close()
    # Dependencies row: a missing tool shows WHY and the by-hand how-to in the page (a tooltip alone is never seen)
    shown = pop.evaluate("""async () => {
      const real = chrome.runtime.sendMessage.bind(chrome.runtime);
      chrome.runtime.sendMessage = async (m) => m && m.type === "deps" ? { ok: true,
        deps: { "yt-dlp": { present: false }, ffmpeg: { present: true }, analyzer: { present: false } },
        hints: { "yt-dlp": "not installed: click Install missing dependencies, or by hand: pipx install yt-dlp", analyzer: "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again", venv: "native/.venv is unusable" } } : real(m);
      await showDeps();
      chrome.runtime.sendMessage = real;
      const w = document.getElementById("depsWhy");
      return [w.hidden, w.textContent, getComputedStyle(w).display];
    }""")
    check("deps row: per-tool reason + by-hand how-to are visible text, not a tooltip", [shown[0], shown[2] != "none", "✗ yt-dlp: not installed: click Install missing dependencies, or by hand: pipx install yt-dlp" in shown[1], "✗ BPM/key analyzer: BPM/key needs Homebrew Python" in shown[1], "native/.venv is unusable" in shown[1]], [False, True, True, True, True])
    sw.evaluate("() => { __sent.length = 0; }")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'download', kind:'video', dest:'/d', prefix:'', items:[{url:'https://www.youtube.com/watch?v=mp3on', engine:true, mode:'audio'}, {url:'https://www.youtube.com/watch?v=mp3vid', engine:true, mode:'video'}]})")
    pop.wait_for_timeout(500)
    check("mp3 on: the flag rides on a single-link audio job, never on video",
          sorted([a[0].split("=")[-1], a[1], a[4]["mp3"]] for a in sw.evaluate("() => __sent")), [["mp3on", "audio", True], ["mp3vid", "video", False]])
    sw.evaluate("() => { __sent.length = 0; }")
    pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'song', dest:'/d', prefix:'', items})", [song("https://open.spotify.com/track/mp3s", "MP3 list", 1)])
    pop.wait_for_timeout(42000)   # its turn waits out the 20 s Spotify gap behind the queued songs before it
    check("mp3 on: the flag rides on a Spotify song's host message", [[a[4]["meta"]["folder"], a[4]["mp3"]] for a in sw.evaluate("() => __sent") if a[4].get("meta") and a[4]["meta"]["folder"] == "MP3 list"], [["MP3 list", True]])
    pop.evaluate("() => chrome.storage.sync.set({mp3: false})"); pop.wait_for_timeout(300)
    sw.evaluate("() => { __sent.length = 0; }")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'download', kind:'video', dest:'/d', prefix:'', items:[{url:'https://www.youtube.com/watch?v=mp3off', engine:true, mode:'audio'}]})")
    pop.wait_for_timeout(500)
    check("mp3 off (default): single-link audio job carries mp3=false", [a[4]["mp3"] for a in sw.evaluate("() => __sent")], [False])
    # ---- dedupe keys (findings 2, 24, 64): the video / page identity, not origin+path
    dl = lambda items, kind="video": pop.evaluate("async a => await chrome.runtime.sendMessage({type:'download', kind:a.kind, dest:'/d', prefix:'', items:a.items})", {"items": items, "kind": kind})
    yt = lambda i: "https://www.youtube.com/watch?v=" + i
    eng = lambda url, mode="video": {"url": url, "engine": True, "mode": mode}
    sw.evaluate("() => { globalThis.__origSite = saveSiteViaHost; saveSiteViaHost = async () => ({ ok: true, path: '' }); globalThis.__origDisk = stillOnDisk; }")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
    check("#2 two different watch?v= videos in one batch are two jobs", dl([eng(yt("aaaaaaaaaaa")), eng(yt("bbbbbbbbbbb"))]), {"queued": 2, "skipped": 0, "unusable": 0})
    pop.wait_for_timeout(500)
    check("#2 a third video after those were saved is queued", dl([eng(yt("ccccccccccc"))]), {"queued": 1, "skipped": 0, "unusable": 0})
    pop.wait_for_timeout(500)
    check("#2 the same video again (also as youtu.be / shorts / tracking params) is skipped",
          dl([eng(yt("aaaaaaaaaaa")), eng("https://youtu.be/bbbbbbbbbbb?si=zzz"), eng("https://www.youtube.com/shorts/ccccccccccc"), eng(yt("aaaaaaaaaaa") + "&feature=share&t=9")]), {"queued": 0, "skipped": 4, "unusable": 0})
    check("#2 the same video as audio is its own job", dl([eng(yt("aaaaaaaaaaa"), "audio")]), {"queued": 1, "skipped": 0, "unusable": 0})
    pop.wait_for_timeout(500)
    check("#2 another engine site keeps its identifying query (?id=1 vs ?id=2), drops tracking",
          dl([eng("https://video.example/watch?id=1"), eng("https://video.example/watch?id=2"), eng("https://video.example/watch?id=2&utm_source=x")]), {"queued": 2, "skipped": 1, "unusable": 0})
    pop.wait_for_timeout(500)
    site = lambda url, extra={}: {"url": url, "site": True, "mode": "pdf", "maxPages": 1, "follow": False, **extra}
    check("#24 site jobs for /trap?i=3 and /trap?i=9 are two jobs", dl([site("https://forum.example/trap?i=3"), site("https://forum.example/trap?i=9")], "site"), {"queued": 2, "skipped": 0, "unusable": 0})
    pop.wait_for_timeout(500)
    check("#24 ...and the same page again is skipped", dl([site("https://forum.example/trap?i=3")], "site"), {"queued": 0, "skipped": 1, "unusable": 0})
    # #64: a record from before 0.1.41 ('<url>#audio', an .mp3 file) satisfies the MP3-switch key; an .m4a one does not
    sw.evaluate("() => { stillOnDisk = async (p) => p.map(() => true); }")
    pop.evaluate("""async () => { await chrome.storage.sync.set({mp3: true});
      await chrome.storage.local.set({'saved:https://youtu.be/oldmp3aaaaa#audio': {ts: 1, path: '/d/Old (120 BPM Am).mp3'}, 'saved:https://youtu.be/oldm4aaaaa#audio': {ts: 1, path: '/d/Old.m4a'}}); }""")
    pop.wait_for_timeout(300)
    check("#64 MP3 switch on: link saved as .mp3 before 0.1.41 is skipped, one saved as .m4a is queued",
          dl([eng("https://youtu.be/oldmp3aaaaa", "audio"), eng("https://youtu.be/oldm4aaaaa", "audio")]), {"queued": 1, "skipped": 1, "unusable": 0})
    pop.wait_for_timeout(500)
    pop.evaluate("async () => { await chrome.storage.sync.set({mp3: false}); await chrome.runtime.sendMessage({type:'forgetSaved'}); }")
    sw.evaluate("() => { stillOnDisk = __origDisk; saveSiteViaHost = __origSite; }")
    sw.evaluate("() => { saveEngineViaHost = __orig; }")

    # ---- 0.1.44: developer log (ring buffer, redaction, forwarding to the host), visible failures, the Logs row
    pop.click("#tabTools"); pop.wait_for_timeout(200)
    check("Logs row: Open log + Copy last 200 lines live in the Tools view",
          pop.evaluate("() => ['logOpen', 'logCopy'].map(id => document.getElementById(id).textContent)"), ["Open log", "Copy last 200 lines"])
    check("Logs row: the file's place is shown", pop.evaluate("() => document.getElementById('logPath').textContent"), "~/Library/Logs/Jownloader/jownloader.log")
    check("Logs row: the [hidden] rule is still in the popup CSS", pop.evaluate("() => [...document.styleSheets[0].cssRules].some(r => r.cssText.replace(/\\s+/g, ' ').includes('[hidden] { display: none !important; }'))"), True)
    pop.click("#logOpen"); pop.wait_for_function("() => /install.sh|opened/i.test(document.getElementById('toolStatus').textContent)", timeout=15000)
    check("Open log with no host: says why, in the popup", "install.sh" in pop.evaluate("() => document.getElementById('toolStatus').textContent"), True)
    pop.evaluate("() => { document.getElementById('toolStatus').textContent = ''; }")
    pop.click("#logCopy"); pop.wait_for_function("() => /install.sh|copied/i.test(document.getElementById('toolStatus').textContent)", timeout=15000)
    check("Copy last 200 lines with no host: says why, in the popup", "install.sh" in pop.evaluate("() => document.getElementById('toolStatus').textContent"), True)
    check("no host: the alert above the views says nothing can be saved, with the fix", pop.evaluate("async () => { await showDeps(); return [document.getElementById('alert').hidden, /install\\.sh/.test(document.getElementById('alertText').textContent)]; }"), [False, True])
    pop.click("#tabDl"); pop.wait_for_timeout(200)

    # ring buffer: newest 2000 kept in memory and in session storage (own key, byte cap), the page-hook cache untouched
    sw.evaluate("""async () => { await chrome.storage.session.set({ manifestTexts: { keep: { tabId: 1, url: 'u', text: '#EXTM3U', ts: Date.now() } } }); }""")
    n = sw.evaluate("""() => { for (let i = 0; i < 2600; i++) log('debug', 'ring', 'entry ' + i); return logRing.length; }""")
    check("ring: capped at 2000 entries", n, 2000)
    ring = sw.evaluate("() => [logRing[0].m, logRing[logRing.length - 1].m]")
    check("ring: the oldest fall out, the newest stay", ring, ["entry 600", "entry 2599"])
    sw.evaluate("() => logSave()"); pop.wait_for_timeout(500)
    st = sw.evaluate("""async () => { const r = await chrome.storage.session.get(['devLog', 'manifestTexts']); return [r.devLog.length, JSON.stringify(r.devLog).length < 450 * 1024, Object.keys(r.manifestTexts)]; }""")
    check("ring: persisted in chrome.storage.session under its own key, within the byte cap; the manifest cache is separate and intact", st, [2000, True, ["keep"]])
    sw.evaluate("""async () => { for (let i = 0; i < 400; i++) log('debug', 'big', 'x'.repeat(590), { pad: 'y'.repeat(700) }); logSave(); await new Promise(r => setTimeout(r, 300)); }""")
    st = sw.evaluate("""async () => { const r = await chrome.storage.session.get('devLog'); return [r.devLog.length < 2000, JSON.stringify(r.devLog).length < 450 * 1024]; }""")
    check("ring: a flood of large entries is cut to the byte cap, not the quota", st, [True, True])
    sw.evaluate("() => { logRing.length = 0; }")
    sw.evaluate("() => { log('info', 'test', 'Cookie: SID=EXTCOOKIE123; HSID=EXTCOOKIE456'); log('warn', 'test', 'fetch https://x.test/a?token=EXTTOKEN789&q=1', { authorization: 'Bearer EXTBEARER', sessionid: 'EXTSESSION' }); }")
    red = sw.evaluate("() => logRing.map(e => e.m + ' ' + (e.d || ''))")
    check("log(): cookie / token / authorization values never enter the ring", [any(x in " ".join(red) for x in ("EXTCOOKIE", "EXTTOKEN", "EXTBEARER", "EXTSESSION")), "Cookie: ***" in red[0], "token=***" in red[1]], [False, True, True])

    # forwarding: batched, fire-and-forget to the host (stubbed here), same entries; host down -> kept and retried
    fwd = sw.evaluate("""async () => {
      while (logBusy) await logBusy;                                               // no timer-started flush may hold the lock
      logRing.length = 0; logSent = logLastN; logDownUntil = 0;
      globalThis.__sentLogs = []; const orig = chrome.runtime.sendNativeMessage;
      chrome.runtime.sendNativeMessage = async (host, m) => { __sentLogs.push([host, m]); return { ok: true, n: m.entries.length }; };
      for (let i = 0; i < 5; i++) log('info', 'fwd', 'm' + i, { i });
      await logFlush();
      chrome.runtime.sendNativeMessage = orig;
      return __sentLogs.map(([h, m]) => [h, m.cmd, m.entries.map((e) => [e.a, e.m, e.l, typeof e.t])]);
    }""")
    check("forward: one batch, cmd=log to the host, entries carry time/level/area/message", fwd, [["com.jshriver.jownloader", "log", [["fwd", f"m{i}", "info", "number"] for i in range(5)]]])
    down = sw.evaluate("""async () => {
      while (logBusy) await logBusy;
      logRing.length = 0; logSent = logLastN; logDownUntil = 0;
      const orig = chrome.runtime.sendNativeMessage; let calls = 0;
      chrome.runtime.sendNativeMessage = async () => { calls++; throw new Error('Specified native messaging host not found.'); };
      log('info', 'down', 'kept while the host is away');
      await logFlush(); const afterFail = [calls, logRing.filter((e) => e.n > logSent).length, logDownUntil > Date.now()];
      await logFlush(); const noHammer = calls;                                   // within the 30 s pause: not asked again
      const sent = []; chrome.runtime.sendNativeMessage = async (h, m) => { sent.push(m.entries.map((e) => e.m)); return { ok: true }; };
      await logFlushNow();                                                         // the popup's Copy: asks regardless of the pause
      chrome.runtime.sendNativeMessage = orig;
      return [afterFail, noHammer, sent];
    }""")
    check("forward: host away -> entry kept, host left alone for a while, delivered when asked again", down, [[1, 1, True], 1, [["kept while the host is away"]]])
    ex = pop.evaluate("""async () => { await chrome.runtime.sendMessage({ type: 'log', entries: [{ t: Date.now(), l: 'info', a: 'click', m: 'Download all images', d: { n: 3 } }, { t: Date.now(), l: 'bogus', a: 'x'.repeat(50), m: 'token=POPTOKEN1' }] });
      return (await chrome.runtime.sendMessage({ type: 'getLog', n: 5 })).filter((e) => e.a.startsWith('popup.')).map((e) => [e.a, e.l, e.m]); }""")
    check("popup events enter the same ring (area prefixed popup., unknown level -> info, secrets cut)", ex, [["popup.click", "info", "Download all images"], ["popup." + "x" * 16, "info", "token=***"]])

    # visible failures: every failed song is named with its reason; a run where all failed ends on an error line
    sw.evaluate("""() => { globalThis.__notes = []; globalThis.__notify = notify; notify = (t, m) => __notes.push([t, m]); spotifyLast = 0;
      globalThis.__orig2 = saveEngineViaHost; saveEngineViaHost = async () => ({ ok: false, output: 'Floaty: YouTube refused the download (format not available).' }); }""")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
    song2 = lambda url, n, t: {"url": url, "engine": True, "mode": "audio", "spotify": {"title": t, "artists": "A", "album": "L", "number": n, "total": 7, "duration_ms": 1000, "cover": "", "url": url, "folder": "Fail list"}}
    pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'song', dest:'/d', prefix:'', items})", [song2("https://open.spotify.com/track/ffff1", 6, "Floaty")])
    pr = pop.evaluate("async () => { for (let i = 0; i < 160; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active && p.total) return p; await new Promise(r => setTimeout(r, 250)); } return 'still active'; }")
    check("visible: a failed song is listed with its number, title and reason", [pr["done"], pr["failed"], pr["errors"]], [1, 1, ["song 6 Floaty: YouTube refused the download (format not available)."]])
    txt = pop.evaluate("(pr) => progressText(pr)", pr)
    check("visible: all songs failed -> the popup ends on an error line, not a quiet 'saved'", [txt.splitlines()[0], txt.splitlines()[1]], ["Nothing was saved: the song failed", "✗ song 6 Floaty: YouTube refused the download (format not available)."])
    check("visible: a Spotify run with failures raises a notification naming them and badges the icon", [sw.evaluate("() => __notes"), sw.evaluate("async () => await chrome.action.getBadgeText({})")], [[["Jownloader: no song was saved", "song 6 Floaty: YouTube refused the download (format not available)."]], "!"])
    lg = sw.evaluate("() => logRing.map((e) => `${e.l} ${e.a} ${e.m}`)")
    check("log: the run's verdict and the failure reason are in the log", [any(l.startswith("error job FAILED song 6 Floaty after") and "format not available" in l for l in lg), any(l.startswith("warn batch run finished: 0 saved, 1 failed of 1") for l in lg)], [True, True])
    shown = pop.evaluate("""(pr) => { showProgress(pr); return [document.getElementById('runHead').textContent, document.getElementById('runHead').className, [...document.querySelectorAll('#runErrs li')].map(l => l.textContent), document.getElementById('stop').hidden, document.getElementById('runClose').hidden]; }""", pr)
    check("visible: the failed run's headline is in the failure colour, the failure listed, Stop gone, dismiss shown", shown, ["Nothing was saved: the song failed", "bad", ["song 6 Floaty: YouTube refused the download (format not available)."], True, False])
    many = {"total": 9, "done": 9, "failed": 8, "bytes": 0, "active": 0, "where": "Downloads", "kind": "song", "error": "x", "errors": [f"song {i} T{i}: reason {i}" for i in range(1, 9)], "now": []}
    t2 = pop.evaluate("(pr) => progressText(pr)", many).splitlines()
    check("visible: 1 saved of 9: head says so, the last 6 failures are listed, the older ones counted", [t2[0], t2[1], len(t2), t2[-1]], ["8 of 9 songs failed", "… 2 earlier failures in the log (Tools → Open log)", 8, "✗ song 8 T8: reason 8"])
    grp = pop.evaluate("""() => { showProgress({total: 9, done: 9, failed: 8, bytes: 0, active: 0, where: '', kind: 'song', error: 'x', now: [], crawl: null,
        errors: [], fails: [...Array(7)].map((_, i) => ({who: 'song ' + (i + 1) + ' T' + (i + 1), why: 'YouTube refused the download'})).concat([{who: 'song 9 T9', why: 'no upload matches'}])});
      return [document.getElementById('runHead').textContent, [...document.querySelectorAll('#runErrs li')].map(l => l.textContent), document.querySelector('#runErrs li').title.split('\\n').length]; }""")
    check("failures sharing a reason are one line with a count; the rest keep their own line", grp, ["8 of 9 songs failed", ["7 songs: YouTube refused the download", "song 9 T9: no upload matches"], 7])
    check("no host: the save rows and buttons are off (the alert says why)", pop.evaluate("() => ['imgs', 'coll', 'sitePdf', 'site'].map((id) => document.getElementById(id).disabled)"), [True, True, True, True])
    running = pop.evaluate("(pr) => progressText(pr)", {"total": 3, "done": 1, "failed": 1, "bytes": 0, "active": 1, "where": "Downloads", "kind": "song", "error": "", "errors": ["song 1 A: boom"], "now": ["song 2 B"]}).splitlines()
    check("visible: while running: counts, failures so far and what is in flight", running, ["1 of 3 songs", "✗ song 1 A: boom", "Now: song 2 B"])
    cells = pop.evaluate("""() => { showProgress({total: 6, done: 3, failed: 1, bytes: 0, active: 1, where: '/d/Shoot', kind: 'image', error: '', errors: ['file x.jpg: HTTP 403'], now: ['file y.jpg'], crawl: null});
      return [[...document.querySelectorAll('#bar i')].map(i => i.className).join(''), document.getElementById('runMeta').textContent, document.getElementById('bar').getAttribute('aria-valuenow')]; }""")
    check("the tally: a cell per file — saved, failed, in flight, waiting — and the folder in the dock", cells, ["ssfa", "Shoot1 failed", "3"])
    stp = pop.evaluate("""() => { showProgress({total: 6, done: 2, failed: 0, bytes: 0, active: 0, where: '', kind: 'image', error: '', errors: [], now: [], crawl: null, stopped: true, notStarted: 3, cut: 1});
      return [document.getElementById('runHead').textContent, [...document.querySelectorAll('#bar i')].map(i => i.className).join(','), document.getElementById('runMeta').textContent]; }""")
    check("stopped: 'Stopped — n saved, m not started', the rest struck through, cut-off files named", stp, ["Stopped — 2 saved, 3 not started", "s,s,x,x,x,x", "Downloads1 cut off, partial file removed"])
    # a single-link engine save that fails shows its reason too
    sw.evaluate("""() => { saveEngineViaHost = async () => ({ ok: false, output: 'YouTube is blocking the download. Sign in to YouTube in this browser, then retry.' }); }""")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'download', kind:'video', dest:'/d', prefix:'', items:[{url:'https://www.youtube.com/watch?v=single00001', engine:true, mode:'video'}]})")
    pr = pop.evaluate("async () => { for (let i = 0; i < 60; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active && p.total) return p; await new Promise(r => setTimeout(r, 250)); } return 'still active'; }")
    check("visible: a failed single-link save lists the page and the reason", [pr["failed"], pr["errors"], pr["error"]], [1, ["video youtube.com/watch?v=single00001: YouTube is blocking the download. Sign in to YouTube in this browser, then retry."], "YouTube is blocking the download. Sign in to YouTube in this browser, then retry."])
    sw.evaluate("() => { notify = __notify; saveEngineViaHost = __orig2; }")

    # ---- 0.1.45: Stop. Queued jobs never start, in-flight fetches abort, engine jobs are cancelled in the host.
    import threading, http.server, time as _t
    hits = []
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", "100000"); self.end_headers()
            try: self.wfile.flush()
            except Exception: return
            for _ in range(600):           # never sends a byte: a fetch that only Stop can end
                _t.sleep(0.1)
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 9661), Slow); srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
    items = [{"url": f"http://127.0.0.1:9661/slow{i}.jpg"} for i in range(6)]
    pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'image', dest:'/d', prefix:'', items})", items)
    pop.wait_for_function("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).now.length === 4", timeout=10000)
    pop.wait_for_timeout(400)
    st = pop.evaluate("async () => { const p = await chrome.runtime.sendMessage({type:'getProgress'}); return [p.active, p.total, p.now.length, await chrome.action.getBadgeText({})]; }")
    check("stop: before — 4 in flight of 6, the badge counts what is left", st, [1, 6, 4, "6"])
    check("stop: the dock shows Stop while it runs", pop.evaluate("() => [document.getElementById('run').hidden, document.getElementById('stop').hidden]"), [False, False])
    t0 = _t.time()
    pop.click("#stop")
    pr = pop.evaluate("async () => { for (let i = 0; i < 40; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active) return p; await new Promise(r => setTimeout(r, 100)); } return 'still active'; }")
    took = _t.time() - t0
    check("stop: the run ends within 3 s (in-flight fetches aborted, not waited out)", took < 3, True)
    check("stop: nothing saved, nothing failed, 6 not started (4 aborted before any byte, 2 never began)", [pr["stopped"], pr["done"], pr["failed"], pr["notStarted"], pr["cut"]], [True, 0, 0, 6, 0])
    check("stop: the 2 queued files were never requested", sorted(hits), sorted(f"/slow{i}.jpg" for i in range(4)))
    pop.wait_for_timeout(400)
    check("stop: the popup shows 'Stopped — 0 saved, 6 not started', Stop gone", pop.evaluate("() => [document.getElementById('runHead').textContent, document.getElementById('stop').hidden]"), ["Stopped — 0 saved, 6 not started", True])
    check("stop: the badge is cleared (nothing failed)", sw.evaluate("async () => await chrome.action.getBadgeText({})"), "")
    lg = sw.evaluate("() => logRing.map((e) => `${e.l} ${e.a} ${e.m}`)")
    check("stop: the log has the press and the verdict", [any(l.startswith("info stop Stop pressed") for l in lg), any("run stopped: 0 saved, 0 failed, 0 cut off, 6 not started of 6" in l for l in lg)], [True, True])
    # an engine job: the host is asked to cancel that fetch id; the reply comes back stopped (fake port stands in for the host)
    sw.evaluate("""() => { globalThis.__hostSent = []; globalThis.__origConnect = hostConnect;
      hostConnect = () => ({ postMessage(m) { __hostSent.push(m); if (m.cmd === 'cancel') for (const j of m.jobs || []) { const w = hostWaiters.get(j); if (w) { hostWaiters.delete(j); w({ id: j, ok: false, stopped: true, output: 'stopped' }); } } } }); }""")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'download', kind:'video', dest:'/d', prefix:'', items:[{url:'https://www.youtube.com/watch?v=stopme00001', engine:true, mode:'video'}]})")
    pop.wait_for_function("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).now.length === 1", timeout=5000)
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'stop'})")
    pr = pop.evaluate("async () => { for (let i = 0; i < 40; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active) return p; await new Promise(r => setTimeout(r, 100)); } return 'still active'; }")
    sent = sw.evaluate("() => __hostSent.map((m) => [m.cmd, m.jobs || null, m.id])")
    fid = [m[2] for m in sent if m[0] == "fetch"]
    check("stop: the engine fetch got a host cancel naming its id", [len(fid), [m[1] for m in sent if m[0] == "cancel"]], [1, [fid[:1]]])
    check("stop: the engine job counts as cut off, not failed", [pr["stopped"], pr["cut"], pr["failed"], pr["notStarted"]], [True, 1, 0, 0])
    # music list: the song waiting out the 20 s gap never starts, and the wait itself ends at once
    sw.evaluate("""() => { hostConnect = __origConnect; globalThis.__songs = []; spotifyLast = 0;
      globalThis.__orig3 = saveEngineViaHost; saveEngineViaHost = (url, mode, dest, fin, extra, sig) => new Promise((res) => { __songs.push(url);
        const t = setTimeout(() => res({ ok: true, path: '' }), 30000); sig.addEventListener('abort', () => { clearTimeout(t); res({ ok: false, stopped: true, output: 'stopped', cut: true }); }); }); }""")
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'forgetSaved'})")
    pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'song', dest:'/d', prefix:'', items})", [song2(f"https://open.spotify.com/track/stop{i}", i + 1, f"S{i}") for i in range(3)])
    pop.wait_for_timeout(800)
    t0 = _t.time()
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'stop'})")
    pr = pop.evaluate("async () => { for (let i = 0; i < 40; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active) return p; await new Promise(r => setTimeout(r, 100)); } return 'still active'; }")
    check("stop (music list): one song started and was cut off, the two waiting their turn never started, no 20 s wait", [len(sw.evaluate("() => __songs")), pr["cut"], pr["notStarted"], pr["failed"], _t.time() - t0 < 3], [1, 1, 2, 0, True])
    check("stop: Stop with nothing running answers idle", pop.evaluate("async () => await chrome.runtime.sendMessage({type:'stop'})"), {"ok": True, "idle": True})
    pop.evaluate("async () => await chrome.runtime.sendMessage({type:'clearProgress'})")
    check("dismiss: the verdict is put away", pop.evaluate("async () => (await chrome.runtime.sendMessage({type:'getProgress'})).total"), 0)
    sw.evaluate("() => { saveEngineViaHost = __orig3; }")

    # 0.1.46: the browser's own session goes to the host for a YouTube job (fake cookies in THIS test profile only), and
    # "no host" means Chrome's own errors - never text that came back inside a host reply.
    sw.evaluate("""async () => {
      const exp = Math.floor(Date.now() / 1000) + 86400, mk = (u, domain, name, value, extra = {}) => chrome.cookies.set({ url: u, domain, name, value, path: '/', secure: true, expirationDate: exp, ...extra });
      await mk('https://www.youtube.com/', '.youtube.com', 'SID', 'FAKEYT1');
      await mk('https://www.youtube.com/', '.youtube.com', 'HSID', 'FAKEYT2', { httpOnly: true });
      await mk('https://accounts.google.com/', '.google.com', 'NID', 'FAKEGOOG');
      await mk('https://example.test/', 'example.test', 'OTHER', 'FAKEOTHER');
      globalThis.__origHC = hostConnect; globalThis.__origHA = hostAsk; globalThis.__msgs = [];
      hostConnect = () => ({ postMessage() {} });
      hostAsk = async (port, msg) => { __msgs.push(msg); return globalThis.__reply || { id: msg.id, ok: true, path: '/d/x.mp4' }; };
    }""")
    first = lambda url, extra="{}": sw.evaluate(f"async () => {{ __msgs.length = 0; await saveEngineViaHost({json.dumps(url)}, 'video', '/d', {{}}, {extra}); return __msgs[0]; }}")
    m = first("https://www.youtube.com/watch?v=abc")
    ck = m.get("cookies", "")
    lines = ck.splitlines()
    check("cookies: a YouTube job carries a cookies.txt with a Netscape header", lines[:1], ["# Netscape HTTP Cookie File"])
    rows = sorted(l.split("\t") for l in lines[1:])
    check("cookies: YouTube and Google cookies in cookies.txt column order (domain, subdomains, path, secure, expiry, name, value)",
          [[r[0], r[1], r[2], r[3], r[5], r[6]] for r in rows], sorted([[".google.com", "TRUE", "/", "TRUE", "NID", "FAKEGOOG"], [".youtube.com", "TRUE", "/", "TRUE", "SID", "FAKEYT1"], ["#HttpOnly_.youtube.com", "TRUE", "/", "TRUE", "HSID", "FAKEYT2"]]))
    check("cookies: expiry is epoch seconds, other sites' cookies are not sent", [rows[0][4].isdigit() and int(rows[0][4]) > 1_000_000_000, "FAKEOTHER" in ck, "example.test" in ck], [True, False, False])
    check("cookies: sent on the fetch message itself (cmd fetch, same job)", [m.get("cmd"), m.get("url")], ["fetch", "https://www.youtube.com/watch?v=abc"])
    check("cookies: a music.youtube.com link and a youtu.be link get them too", [bool(first(u).get("cookies")) for u in ("https://music.youtube.com/watch?v=abc", "https://youtu.be/abc")], [True, True])
    check("cookies: a Spotify song (matched on YouTube) gets them", bool(first("https://open.spotify.com/track/abc", "{ meta: { number: 1, artists: 'A', title: 'T' } }").get("cookies")), True)
    check("cookies: any other site's engine job (no cookies stored for it) sends none", ["cookies" in first(u) for u in ("https://www.instagram.com/p/x/", "https://example.com/v")], [False, False])
    leak = sw.evaluate("""async () => JSON.stringify([logRing, await chrome.storage.local.get(null), await chrome.storage.sync.get(null), await chrome.storage.session.get(null)])""")
    check("cookies: only counts reach the log; no value is in the log ring or any chrome.storage area", [("FAKEYT1" in leak), ("FAKEGOOG" in leak), ("FAKEYT2" in leak), ("cookies.txt from the browser: 3 cookies for 2 domains" in leak)], [False, False, False, True])
    # a host reply's own words are shown as they are; Chrome's own errors are the "not installed" message
    shown = lambda text: sw.evaluate("t => hostError(t)", text)
    NI = "native host not installed — run native/install.sh"
    check("no-host: text from inside a host reply ('403: Forbidden', 'not found', 'disconnected') is shown as itself",
          [shown(t) for t in ("ERROR: unable to download: HTTP Error 403: Forbidden", "Download failed: file not found", "the site disconnected us")],
          ["ERROR: unable to download: HTTP Error 403: Forbidden", "Download failed: file not found", "the site disconnected us"])
    check("no-host: Chrome's own native-messaging errors still say the host is not installed",
          [shown(t) for t in ("Specified native messaging host not found.", "Access to the specified native messaging host is forbidden.", "Native host has exited.", "Error when communicating with the native messaging host.", "native host disconnected")], [NI] * 5)
    sw.evaluate("() => { globalThis.__reply = { id: 'x', ok: false, output: 'Clip: ERROR: HTTP Error 403: Forbidden' }; }")
    r403 = sw.evaluate("async () => (await saveEngineViaHost('https://www.youtube.com/watch?v=abc', 'video', '/d')).output")
    check("no-host: a failed job whose reply says 403: Forbidden shows that text, not the install hint", r403, "Clip: ERROR: HTTP Error 403: Forbidden")
    sw.evaluate("""async () => { globalThis.__reply = null; hostConnect = __origHC; hostAsk = __origHA;
      for (const [u, n, d] of [['https://www.youtube.com/', 'SID', '.youtube.com'], ['https://www.youtube.com/', 'HSID', '.youtube.com'], ['https://accounts.google.com/', 'NID', '.google.com'], ['https://example.test/', 'OTHER', 'example.test']]) await chrome.cookies.remove({ url: u, name: n }); }""")
    srv.shutdown()
    # last: two queued songs hold the queue (20 s Spotify gap), so nothing after this may wait on a job
    r = pop.evaluate("async items => await chrome.runtime.sendMessage({type:'download', kind:'song', dest:'/d', prefix:'', items})",
                     [song("https://open.spotify.com/track/bbb", "Dup list", 1), song("https://open.spotify.com/track/bbb", "Dup list", 2)])
    check("0.1.48: a song the list holds twice (positions 1 and 2) = two jobs", r, {"queued": 2, "skipped": 0, "unusable": 0})
    ctx.close()
print("ALL GREEN" if not fails else f"{fails} FAILED")
