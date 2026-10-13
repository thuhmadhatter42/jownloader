# Spotify engine regression harness (0.1.43): the list sync (exact matching, never lose / overwrite a file), the
# track matcher, the output-template escaping, cover sniffing, tags and the yt-dlp run budget.
# No live network: yt-dlp only ever talks to a local http server (port 9621, override JOWNLOADER_TEST_PORT);
# searches, embed pages and cover fetches are stubs. Run with the host venv (has mutagen):
#   native/.venv/bin/python tests/spotify_engine_test.py
import hashlib, http.server, json, os, random, shutil, subprocess, sys, tempfile, threading
HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(os.path.dirname(HERE), "native")
sys.path.insert(0, NATIVE)
_LOGDIR = tempfile.mkdtemp(prefix="jown-spotlog-"); os.environ["JOWN_LOG_DIR"] = _LOGDIR   # the developer log goes to a temp folder
import engine
fails = 0
PORT = int(os.environ.get("JOWNLOADER_TEST_PORT", "9621"))


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:12]


def hashes(folder):   # every file, hidden ones too: {name: hash}
    return {n: sha(os.path.join(folder, n)) for n in sorted(os.listdir(folder)) if os.path.isfile(os.path.join(folder, n))}


def nothing_lost(before, after):   # every byte-content that was there is still there (names may change)
    return sorted(before.values()) == sorted(after.values())


def write(folder, name, data):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


T = lambda title, artists="Artist", n=1, **kw: {"title": title, "artists": artists, "number": n, **kw}
d = tempfile.mkdtemp(prefix="spotify-engine-", dir=os.environ.get("JOWNLOADER_TEST_DIR") or HERE)
srv = None
try:
    # ---------------------------------------------------------------- sync: exact "artists - title" matching (1, 10)
    L = os.path.join(d, "s1", "L")
    write(L, "01 - Artist - Song.m4a", "STUDIO")
    b = hashes(L)
    r = engine.spotify_sync(os.path.join(d, "s1"), "L", [T("Song - Extended Mix")], [], 1)
    check("1: a ' - Extended Mix' track does not claim the plain-title file (not relabeled, not 'have')", [r["have"], r["renamed"], sorted(os.listdir(L))], [[], 0, ["01 - Artist - Song.m4a"]])
    L = os.path.join(d, "s1b", "L")
    write(L, "01 - Artist - Song.m4a", "STUDIO"); write(L, "02 - Artist - Song - Extended Mix.m4a", "REMIX")
    r = engine.spotify_sync(os.path.join(d, "s1b"), "L", [T("Song - Extended Mix", n=1), T("Song", n=2)], [], 2)
    got = {n: open(os.path.join(L, n)).read() for n in os.listdir(L)}
    check("1: both on disk, list order flipped: each file keeps its own audio under its own title", [r["have"], got], [[0, 1], {"01 - Artist - Song - Extended Mix.m4a": "REMIX", "02 - Artist - Song.m4a": "STUDIO"}])
    lib = os.path.join(d, "s1c", "lib"); write(os.path.join(lib, "L"), "Artist - Song.mp3", "X")
    r = engine.spotify_sync(os.path.join(d, "s1c"), "L", [T("Song - Radio Edit")], [lib], 1)
    check("1: a library 'Artist - Song.mp3' is not the 'Song - Radio Edit' track", r["inlib"], [])
    r = engine.spotify_sync(os.path.join(d, "s1c"), "L", [T("Song")], [lib], 1)
    check("1: ...but it is the plain 'Song' track", r["inlib"], [0])
    F = os.path.join(d, "s10"); write(F, "01 - Band - Song.m4a", "STUDIO")
    check("10: 'Song - Live at Wembley' is not found in a folder holding only the studio 'Song'", engine.find_existing(F, T("Song - Live at Wembley", "Band", 2)), None)
    check("10: ...nor 'Song - Remastered 2011'", engine.find_existing(F, T("Song - Remastered 2011", "Band", 2)), None)
    check("10: the studio track still finds its file", os.path.basename(engine.find_existing(F, T("Song", "Band")) or ""), "01 - Band - Song.m4a")
    write(F, "02 - Band - Song - Live at Wembley.m4a", "LIVE")
    check("10: and the live file is not the plain track's (vice versa)", [os.path.basename(engine.find_existing(F, T("Song", "Band")) or ""), os.path.basename(engine.find_existing(F, T("Song - Live at Wembley", "Band")) or "")], ["01 - Band - Song.m4a", "02 - Band - Song - Live at Wembley.m4a"])
    check("37: '(feat. X)' is still the same song for the matcher", os.path.basename(engine.find_existing(F, T("Song (feat. Z)", "Band")) or ""), "01 - Band - Song.m4a")

    # ---------------------------------------------------------------- sync: temp names, crash leftovers, two syncs at once (6)
    base = os.path.join(d, "s6"); L = os.path.join(base, "L")
    write(L, ".jown-sync-0.m4a", "OLDLEFTOVER")
    for i in (1, 2, 3):
        write(L, f"0{i} - Artist - Song {i}.m4a", f"SONG{i}")
    b = hashes(L)
    r = engine.spotify_sync(base, "L", [T("Song 3", n=1), T("Song 1", n=2), T("Song 2", n=3)], [], 3)
    a = hashes(L)
    check("6: a stale hidden .jown-sync-0 file is not overwritten (nothing lost), the renumber happened", [nothing_lost(b, a), r["renamed"], sorted(n for n in a if not n.startswith(".") and not n.startswith("Recovered"))], [True, 3, ["01 - Artist - Song 3.m4a", "02 - Artist - Song 1.m4a", "03 - Artist - Song 2.m4a"]])
    check("6: the leftover is visible again (recovered), not hidden forever", [n for n in a if n.startswith(".jown-sync")], [])
    base = os.path.join(d, "s6c"); L = os.path.join(base, "L"); names = [f"Song {i}" for i in range(1, 13)]
    for i, nme in enumerate(names, 1):
        write(L, f"{i:02d} - Artist - {nme}.m4a", f"CONTENT {nme}")
    b = hashes(L)
    prog = "import sys,json;sys.path.insert(0,sys.argv[1]);import engine;print(json.dumps(engine.spotify_sync(sys.argv[2],'L',json.loads(sys.argv[3]),[],12)))"
    bad, rng = 0, random.Random(7)
    for run in range(25):
        procs = []
        for _ in range(2):
            order = names[:]; rng.shuffle(order)
            procs.append(subprocess.Popen([sys.executable, "-c", prog, NATIVE, base, json.dumps([T(n, n=i + 1) for i, n in enumerate(order)])], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        outs = [p.communicate() for p in procs]
        a = hashes(L)
        if any(p.returncode for p in procs) or not nothing_lost(b, a) or len(a) != 12:
            bad += 1
    check("6: two syncs on one folder at once, 25 shuffled runs: no crash, no lost or extra file", bad, 0)

    # ---------------------------------------------------------------- symbol-only titles (28, 38)
    base = os.path.join(d, "s28"); L = os.path.join(base, "L")
    write(L, "01 - Artist - ★.m4a", "STAR"); write(L, "02 - Artist - ♥.m4a", "HEART")
    r = engine.spotify_sync(base, "L", [T("♥", n=1), T("★", n=2)], [], 2)
    got = {n: open(os.path.join(L, n)).read() for n in os.listdir(L)}
    check("28: symbol-only titles do not swap contents on a reorder", got, {"01 - Artist - ♥.m4a": "HEART", "02 - Artist - ★.m4a": "STAR"})
    base = os.path.join(d, "s28b"); L = os.path.join(base, "L")
    write(L, "01 - Artist - ★.m4a", "STAR")
    check("28: a symbol-only track does not claim a file of a different symbol", engine.spotify_sync(base, "L", [T("♥", n=1)], [], 1)["have"], [])

    # ---------------------------------------------------------------- long names (29)
    long_t = T("T" * 200, "Artist", 4, folder="L")
    base = os.path.join(d, "s29"); L = os.path.join(base, "L")
    write(L, engine.spotify_stem(long_t) + ".m4a", "LONG")
    check("29: a 200-char title is recognised on re-run (find_existing)", bool(engine.find_existing(L, long_t)), True)
    check("29: ...and by the list sync, under the same name", [engine.spotify_sync(base, "L", [long_t], [], 4)["have"], len(os.listdir(L))], [[0], 1])
    old = os.path.join(d, "s29b", "L"); write(old, ("04 - Artist - " + "T" * 200)[:180] + ".m4a", "OLDLONG")
    check("29: a long file saved by 0.1.42 (cut at 180) still matches", bool(engine.find_existing(old, long_t)), True)

    # ---------------------------------------------------------------- normalisation + scoring (37, 11, 12, 38)
    H = lambda title, ch, dur, url="u": {"title": title, "channel": ch, "duration": dur, "url": url}
    fake = lambda hits: (lambda q: (hits, []))
    for want, got_t in (("Don’t Stop Believin’", "Don't Stop Believin'"), ("Déjà Vu", "Deja Vu"), ("Rock & Roll", "Rock and Roll"), ("Song (Part 1)", "Song Pt. 1"), ("Beyoncé Song", "Beyonce Song")):
        check(f"37: '{want}' matches the upload '{got_t}'", engine.spotify_match({"title": want, "artists": "Artist", "duration_ms": 200000}, fake([H(f"Artist - {got_t}", "Artist", 200, "ok")]))[0], "ok")
    check("37: sync folds curly quotes, accents, & / and: 'Rock & Roll' finds 'Rock and Roll.m4a'", bool(engine.find_existing(os.path.join(d, "s37"), T("x"))), False)
    write(os.path.join(d, "s37"), "01 - Artist - Rock and Roll.m4a", "R"); write(os.path.join(d, "s37"), "02 - Artist - Don't Stop.m4a", "D")
    check("37: ...file side", [os.path.basename(engine.find_existing(os.path.join(d, "s37"), T("Rock & Roll")) or ""), os.path.basename(engine.find_existing(os.path.join(d, "s37"), T("Don’t Stop")) or "")], ["01 - Artist - Rock and Roll.m4a", "02 - Artist - Don't Stop.m4a"])
    hurt = {"title": "Hurt", "artists": "Nine Inch Nails", "duration_ms": 372000}
    check("11: a Johnny Cash upload of 'Hurt' (Topic channel, right length) is not the Nine Inch Nails track", engine.spotify_match(hurt, fake([H("Hurt - Johnny Cash", "Johnny Cash - Topic", 372)]))[0], None)
    check("11: the real artist's Topic upload is", engine.spotify_match(hurt, fake([H("Hurt", "Nine Inch Nails - Topic", 372, "nin")]))[0], "nin")
    check("11: the artist in the upload title is enough", engine.spotify_match(hurt, fake([H("Nine Inch Nails - Hurt (Official Audio)", "Some Label", 372, "nin2")]))[0], "nin2")
    check("11: candidates with no channel info at all are not refused for it", engine.spotify_match(hurt, fake([H("Hurt", "", 372, "anon")]))[0], "anon")
    check("12: 'Love' does not match 'Glove Story'", engine.spotify_match({"title": "Love", "artists": "Artist", "duration_ms": 200000}, fake([H("Glove Story", "Artist - Topic", 200)]))[0], None)
    check("12: 'Her' does not match 'Together Again'", engine.spotify_match({"title": "Her", "artists": "Artist", "duration_ms": 200000}, fake([H("Together Again", "Artist - Topic", 200)]))[0], None)
    kl = {"title": "i", "artists": "Kendrick Lamar", "duration_ms": 232000}
    check("12: 'i' does not match 'Kendrick Lamar - Alright'", engine.spotify_match(kl, fake([H("Kendrick Lamar - Alright", "Kendrick Lamar", 233)]))[0], None)
    check("12: 'i' matches the exact-title upload", engine.spotify_match(kl, fake([H("i", "Kendrick Lamar - Topic", 232, "i1")]))[0], "i1")
    check("12: ...and 'Kendrick Lamar - i (Official Audio)'", engine.spotify_match(kl, fake([H("Kendrick Lamar - i (Official Audio)", "Kendrick Lamar", 232, "i2")]))[0], "i2")
    check("12: a 4-letter title matches as a word: 'Artist - Love (Official Audio)'", engine.spotify_match({"title": "Love", "artists": "Artist", "duration_ms": 200000}, fake([H("Artist - Love (Official Audio)", "Artist", 200, "w")]))[0], "w")
    bang = {"title": "!!!", "artists": "Band", "duration_ms": 200000}
    check("38: a symbol-only title does not accept another song by the same artist", engine.spotify_match(bang, fake([H("Something else entirely", "Band - Topic", 200)]))[0], None)
    check("38: ...it needs the symbols in the upload's title", engine.spotify_match(bang, fake([H("Band - !!! (Official Audio)", "Band", 200, "b")]))[0], "b")

    # ---------------------------------------------------------------- the list reader: episodes, local files, album (14, 13, 65)
    emb = os.path.join(d, "embed"); os.makedirs(emb)
    def page(entity):
        nd = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(nd) + "</script></html>"
    row = lambda uri, title, dur: {"uri": uri, "title": title, "subtitle": "Art", "duration": dur}
    open(emb + "/playlist.html", "w").write(page({"name": "Mixed", "uri": "spotify:playlist:p", "trackList": [
        row("spotify:track:T1", "Song One", 1000), row("spotify:episode:EP1", "Podcast ep", 3000), row("spotify:local:Art:Alb:Title:215", "Local file", 215000), row("spotify:track:T1", "Song One", 1000), row("spotify:track:T2", "Song Two", 2000)]}))
    open(emb + "/track.html", "w").write(page({"name": "Solo", "uri": "spotify:track:SOLO", "artists": [{"name": "Art"}], "duration": 5000}))
    open(emb + "/album.html", "w").write(page({"name": "The Album", "uri": "spotify:album:a", "trackList": [row("spotify:track:A1", "Alpha", 1000)]}))
    os.environ[engine.EMBED_DIR_ENV] = emb
    r = engine.spotify_list("https://open.spotify.com/playlist/p")
    check("14: episode and local-file rows are not songs; the two real rows stay", [t["url"] for t in r["tracks"]], ["https://open.spotify.com/track/T1", "https://open.spotify.com/track/T1", "https://open.spotify.com/track/T2"])
    check("14: ...and are reported as skipped by name", sorted(r.get("skipped") or []), ["Local file (local file)", "Podcast ep (podcast episode)"])
    check("14: ...in the note the popup shows", ("Podcast ep" in r.get("note", "")) and ("Local file" in r.get("note", "")), True)
    check("13: a single-song list carries no album (never its own title)", engine.spotify_list("https://open.spotify.com/track/SOLO")["tracks"][0]["album"], "")
    check("13: an album's songs carry the album name", engine.spotify_list("https://open.spotify.com/album/a")["tracks"][0]["album"], "The Album")
    open(emb + "/track.html", "w").write(page({"name": "Solo", "uri": "spotify:track:SOLO", "artists": [{"name": "Art"}], "duration": 5000, "album": {"name": "Real Album"}}))
    check("65: a track page that names its album gives that album", engine.spotify_list("https://open.spotify.com/track/SOLO")["tracks"][0]["album"], "Real Album")
    del os.environ[engine.EMBED_DIR_ENV]

    # ---------------------------------------------------------------- yt-dlp run budget (36), second candidate (66)
    saved_yt, saved_strats = engine._yt, engine.YT_STRATEGIES
    ORIG = {k: getattr(engine, k) for k in ("_yt_search", "_yt_info", "_analyze", "_spotify_embed", "_yt", "_fetch_bytes")}
    def restore():
        for k, v in ORIG.items():
            setattr(engine, k, v)
    real_strats = list(saved_strats)
    calls = []
    def stub(code, out="", err=""):
        return lambda *a: (calls.append(a) or (code, out, err))
    trk = {"title": "Nothing Here", "artists": "Artist", "duration_ms": 200000}
    for label, args in (("no search results", (0, "", "")), ("offline", (1, "", "ERROR: urlopen error [Errno 8] nodename nor servname provided, or not known")), ("a bot gate on every strategy", (1, "", "ERROR: Sign in to confirm you're not a bot"))):
        calls.clear(); engine._yt, engine.YT_STRATEGIES, engine._yt_working = stub(*args), real_strats, None
        engine.spotify_match(trk)
        check(f"36: yt-dlp runs for one unmatched track ({label}) stay under the cap", len(calls) <= engine.YT_RUNS_PER_TRACK if hasattr(engine, "YT_RUNS_PER_TRACK") else len(calls), True if hasattr(engine, "YT_RUNS_PER_TRACK") else "21 runs (no cap)")
        if label != "a bot gate on every strategy":
            check(f"36: ...({label}) at most one run per search phrasing", len(calls) <= 3, True)
    engine._yt, engine.YT_STRATEGIES, engine._yt_working = saved_yt, saved_strats, None

    # ---------------------------------------------------------------- real files, real yt-dlp, a local server only
    audio = os.path.join(d, "srv"); os.makedirs(audio)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", audio + "/a.m4a"], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1", "-frames:v", "1", audio + "/c.jpg"], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1", "-frames:v", "1", audio + "/c.png"], check=True)
    open(audio + "/notimg.jpg", "w").write("<html><body>Access denied captcha</body></html>")
    class Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    import functools
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Q, directory=audio))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{PORT}/a.m4a"
    engine.YT_STRATEGIES, engine._yt_working = [[]], None      # never --cookies-from-browser in a test

    cover_urls = {"cover": f"http://127.0.0.1:{PORT}/c.jpg", "html": f"http://127.0.0.1:{PORT}/notimg.jpg", "png": f"http://127.0.0.1:{PORT}/c.png"}
    check("35: a cover response that is HTML is refused", engine._cover_bytes(cover_urls["html"]), None)
    check("35: a JPEG and a PNG are accepted", [(engine._cover_bytes(cover_urls[k]) or b"")[:2] for k in ("cover", "png")], [b"\xff\xd8", b"\x89P"])

    state = {"album": None, "cover": cover_urls["cover"]}
    def fake_embed(kind, sid):
        e = {"name": "x", "releaseDate": {"isoString": "1975-11-21T00:00:00Z"}, "visualIdentity": {"image": [{"url": state["cover"], "maxWidth": 300}]}}
        if state["album"]:
            e["album"] = {"name": state["album"]}
        return e
    engine._spotify_embed = fake_embed
    def run_track(meta, hits, out):
        engine._yt_search = lambda q, n=5: (hits, [])
        return engine.spotify_track(meta, out, False, False)
    def tags(path):
        from mutagen.mp4 import MP4
        f = MP4(path)
        return {"album": f.get("\xa9alb"), "title": f.get("\xa9nam"), "covr": [bytes(c)[:2] for c in f.get("covr", [])], "trkn": f.get("trkn")}
    meta = lambda title, **kw: {"title": title, "artists": "Art", "album": "", "number": 1, "total": 1, "duration_ms": 1000, "cover": "", "url": "https://open.spotify.com/track/abc", "folder": "", **kw}
    hit = lambda url=URL, title="X": [H(f"Art - {title}", "Art", 1, url)]
    try:
        import mutagen  # noqa: F401
        have_mutagen = True
    except ImportError:
        have_mutagen = False; print("skip tag checks (run with native/.venv/bin/python: no mutagen here)")

    out = os.path.join(d, "t13"); state.update(album=None, cover=cover_urls["cover"])
    r = run_track(meta("X"), hit(), out)
    check("track download works end to end against the local server", [r.get("ok"), os.path.basename(r.get("path", ""))], [True, "Art - X.m4a"]) if r.get("ok") else check("track download works end to end against the local server", r, "ok")
    if r.get("ok") and have_mutagen:
        t = tags(r["path"])
        check("13/65: a single song with no album anywhere has no album tag at all", t["album"], None)
        check("35: the JPEG cover is embedded", t["covr"], [b"\xff\xd8"])
        state["album"] = "Real Album"
        r2 = run_track(meta("Y"), hit(title="Y"), out)
        check("65: the album comes from the track's own page when the link has none", tags(r2["path"])["album"] if r2.get("ok") else r2, ["Real Album"])
        state["album"] = None; state["cover"] = cover_urls["html"]
        r3 = run_track(meta("Z"), hit(title="Z"), out)
        check("35: an HTML 'cover' is not embedded", tags(r3["path"])["covr"] if r3.get("ok") else r3, [])
        state["cover"] = cover_urls["cover"]

    # templates (30, 34, 59): the file lands where it was meant to, under the name it was meant to have
    out = os.path.join(d, "t30")
    for title, folder in (("100%(feat. X)", "Mix %(id)s"), ("%(title)s", "Plain"), ("Pure $HOME Love", "a%%b"), ("${HOME}", "50% Hits"), ("100% Pure", "")):
        m = meta(title, folder=folder, number=3, total=12)
        want = os.path.join(out, folder, engine.spotify_stem(m) + ".m4a")
        r = run_track(m, hit(title=title), out)
        check(f"34/59: title {title!r} in folder {folder!r} lands at exactly that name", [r.get("ok"), os.path.exists(want), r.get("path") == want], [True, True, True])
    check("34/59: nothing was created but the intended folders and the one file at the top", sorted(os.listdir(out)), sorted(["Mix %(id)s", "Plain", "a%%b", "50% Hits", engine.spotify_stem(meta("100% Pure", number=3, total=12)) + ".m4a"]))
    # the single-link path
    yd = os.path.join(d, "t30y"); os.makedirs(yd)
    engine._yt_info = lambda url, extra: ({"title": "100% (Live) %(id)s $HOME"}, [])
    engine._analyze = lambda p: None
    r = engine.youtube(URL, "audio", yd)
    check("30: a single link whose title holds %(id)s and $HOME is saved under that exact title", [r.get("ok"), sorted(n for n in os.listdir(yd) if n.endswith(".m4a"))], [True, ["100% (Live) %(id)s $HOME.m4a"]])
    yd2 = os.path.join(d, "Dir %(id)s")
    os.makedirs(yd2)
    r = engine.youtube(URL, "audio", yd2)
    check("30: ...and a destination folder named 'Dir %(id)s' is the folder it lands in", [r.get("ok"), sorted(n for n in os.listdir(yd2) if n.endswith(".m4a")), sorted(n for n in os.listdir(d) if n.startswith("Dir"))], [True, ["100% (Live) %(id)s $HOME.m4a"], ["Dir %(id)s"]])

    # 32: a BPM-named file already there
    yd3 = os.path.join(d, "t32"); os.makedirs(yd3)
    engine._yt_info = lambda url, extra: ({"title": "Plain Song"}, [])
    engine._analyze = lambda p: ("120.0", ["Am", "Em", "A"])
    engine.youtube(URL, "audio", yd3)
    first = sorted(n for n in os.listdir(yd3) if n.endswith(".m4a"))
    r = engine.youtube(URL, "audio", yd3)
    second = sorted(n for n in os.listdir(yd3) if n.endswith(".m4a"))
    check("32: first run saves 'Plain Song (120.0 BPM Am Em A).m4a'", first, ["Plain Song (120.0 BPM Am Em A).m4a"])
    check("32: a re-download leaves one file, the BPM-named one, no duplicate", [r.get("ok"), second], [True, ["Plain Song (120.0 BPM Am Em A).m4a"]])
    check("32: ...and reports that file", os.path.basename(r.get("path", "")), "Plain Song (120.0 BPM Am Em A).m4a")

    # 66: the best upload fails, the second best is tried
    out = os.path.join(d, "t66")
    bad_url = f"http://127.0.0.1:{PORT}/missing.m4a"
    hits = [H("Art - X (Official Audio)", "Art - Topic", 1, bad_url), H("Art - X", "Art", 1, URL)]
    r = run_track(meta("X"), hits, out)
    check("66: the best-scored upload is gone; the next acceptable one is downloaded", [r.get("ok"), os.path.basename(r.get("path", ""))], [True, "Art - X.m4a"])
    # 36 (whole track): search + download together stay under the cap even when everything fails
    restore()
    engine._spotify_embed = fake_embed
    engine.YT_STRATEGIES, engine._yt_working = real_strats, None
    calls.clear(); engine._yt = stub(1, "", "ERROR: HTTP Error 403: Forbidden")
    r = engine.spotify_track(meta("Nobody Home"), os.path.join(d, "t36"), False, False)
    cap = getattr(engine, "YT_RUNS_PER_TRACK", None)
    check("36: one failing track costs at most the per-track cap of yt-dlp runs (whole track, search + download)", [r.get("ok"), len(calls) <= (cap or 0), len(calls)], [False, True, len(calls)])
    restore()
    engine.YT_STRATEGIES = [[]]

    # 28 (by id): two songs of one artist whose titles are the same symbol are told apart by the Spotify track id in the file
    if have_mutagen:
        base = os.path.join(d, "s28id"); L = os.path.join(base, "L"); os.makedirs(L)
        for nme, freq in (("01 - Art - \u2605.m4a", 440), ("02 - Art - \u2605.m4a", 660)):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-c:a", "aac", os.path.join(L, nme)], check=True)
        tk = lambda tid, n: {"title": "\u2605", "artists": "Art", "number": n, "url": "https://open.spotify.com/track/" + tid}
        engine.spotify_tag(os.path.join(L, "01 - Art - \u2605.m4a"), {**tk("IDONE", 1), "total": 2})
        engine.spotify_tag(os.path.join(L, "02 - Art - \u2605.m4a"), {**tk("IDTWO", 2), "total": 2})
        one, two = hashes(L)["01 - Art - \u2605.m4a"], hashes(L)["02 - Art - \u2605.m4a"]
        r = engine.spotify_sync(base, "L", [tk("IDTWO", 1), tk("IDONE", 2)], [], 2)
        a = hashes(L)
        check("28: same symbol title, two ids: the reorder moves each file to its own track (matched by track id)", [r["have"], a.get("01 - Art - \u2605.m4a") == two, a.get("02 - Art - \u2605.m4a") == one], [[0, 1], True, True])

    # 6: a leftover from a sync that died between its renames is put back under its own name
    base = os.path.join(d, "s6d"); L = os.path.join(base, "L")
    write(L, ".jown-sync-0a1b2c3d-0~01 - Artist - Gone.m4a", "HIDDEN")
    write(L, "01 - Artist - Here.m4a", "HERE")
    engine.spotify_sync(base, "L", [T("Here")], [], 1)
    check("6: a crashed sync's hidden temp comes back under its original name", sorted(os.listdir(L)), ["01 - Artist - Gone.m4a", "01 - Artist - Here.m4a"])

    # 57: the committed embed pages carry no live token, client id or correlation id, and still parse
    import re as _re
    fx = os.path.join(HERE, "fixtures")
    leaks = [f for f in sorted(os.listdir(fx)) if f.endswith(".html") and _re.search(r'BQ[A-Za-z0-9_-]{20,}|ab9ad0d96a62|"correlationId":"(?!REDACTED)|sp_cid=(?!REDACTED)|sentry-public_key=(?!REDACTED)', open(os.path.join(fx, f), encoding="utf-8").read())]
    check("57: no fixture holds an access token, client id, correlation id or sentry key", leaks, [])
    os.environ[engine.EMBED_DIR_ENV] = fx
    check("57: ...and all four still parse as embed pages", [len(engine.spotify_list("https://open.spotify.com/" + k + "/x").get("tracks", [])) > 0 for k in ("track", "album", "playlist", "artist")], [True] * 4)
    del os.environ[engine.EMBED_DIR_ENV]

    # 31: strip on .opus
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        op = os.path.join(d, "t31.opus")
        mk = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", "-metadata", "title=SECRET", "-metadata", "artist=Who", op], capture_output=True)
        if mk.returncode == 0:
            ok, msg = engine.strip_metadata(op)
            tg = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream_tags:format_tags", "-of", "json", op], capture_output=True, text=True).stdout
            check("31: strip on .opus removes the tags", [ok, "SECRET" in tg, "Who" in tg, os.path.exists(op)], [True, False, False, True])
        else:
            print("skip .opus strip check (no libopus in ffmpeg)")
finally:
    if srv:
        srv.shutdown()
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(_LOGDIR, ignore_errors=True)
print("ALL GREEN" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
