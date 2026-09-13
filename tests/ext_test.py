# Jownloader harness — Playwright Chromium with the extension loaded. No native host can be reached from a
# Playwright browser, so this covers scanning, naming, dedupe and the loud no-host failure; actual writes
# are covered by stream_test_cdp.py (real Brave). Run: python3 tests/ext_test.py   Trash tests/test-run after.
import os, json
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(os.path.dirname(HERE), "extension")
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
    check("popup buttons", [b.text_content().split(" (")[0] for b in pop.query_selector_all("button")][:11],
          ["⧉ sidebar", "Download", "Batch", "📁 Downloads", "×", "Download from this page", "Audio only → MP3 + BPM/key", "Save site as PDF", "Save site as Markdown", "Download all images", "Download all videos"])   # (+ ×, forget saved, rename tab…)

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
    check("popup collected button", pop.evaluate("async () => { await new Promise(r => setTimeout(r, 300)); const b = document.getElementById('coll'); return [b.textContent, b.disabled]; }"), ["Download collected (4)", False])
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
    ]""")
    check("filenames", names, ['300.jpg', 'clip.mp4', 'manual.pdf', 'clip_2.mp4',
          'shoot-25-03-07_3.jpg', '03-7-2025_1.jpg', 'mar-7-2025_1.jpg', '25-march-07_1.jpg', 'Mar 7 2025_1.jpg', True])

    # a batch with no reachable host: queued, then every file fails with the install hint — never silent
    r = pop.evaluate("""async () => await chrome.runtime.sendMessage({type:'download', kind:'image', prefix:'beat', items:[
      {url:'https://picsum.photos/id/30/300/200.jpg'}, {url:'https://picsum.photos/id/30/300/200.jpg?token=abc'}, {url:'blob:x'}]})""")
    check("batch reply (dup + blob)", r, {"queued": 1, "skipped": 1, "unusable": 1})
    pr = pop.evaluate("async () => { for (let i = 0; i < 40; i++) { const p = await chrome.runtime.sendMessage({type:'getProgress'}); if (!p.active) return p; await new Promise(r => setTimeout(r, 250)); } return 'still active'; }")
    check("no host → loud failure", [pr["done"], pr["failed"], "install.sh" in (pr.get("error") or "")], [1, 1, True])
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
    check("rename tab present", pop.evaluate("() => [!!document.getElementById('tabRn'), document.getElementById('rnGo').disabled, document.getElementById('rnDest').disabled]"), [True, True, True])
    check("YouTube tracks are never whole files", sw.evaluate("() => [isWhole('https://rr1---sn-abc.googlevideo.com/videoplayback?itag=18', 'video/mp4'), isWhole('https://cdn.x/a.mp4', 'video/mp4')]"), [False, True])
    check("sniffed extensions", sw.evaluate("() => [sniffExt(new Uint8Array([0,0,0,0x18,0x66,0x74,0x79,0x70,0x69,0x73,0x6f,0x6d,0,0])), sniffExt(new Uint8Array([0xff,0xd8,0xff,0xe0,0,0,0,0,0,0,0,0,0])), sniffExt(new Uint8Array([0x1a,0x45,0xdf,0xa3,0,0,0,0,0,0,0,0,0])), sniffExt(new Uint8Array(13))]"), ["mp4", "jpg", "webm", ""])
    ctx.close()
print("ALL GREEN" if not fails else f"{fails} FAILED")
