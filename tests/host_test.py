# Native-host rename test: drives native/jownloader_host.py over its stdio protocol with real files in a
# temp folder (no Finder dialog, no browser). Run: python3 tests/host_test.py
import json, os, select, struct, subprocess, sys, tempfile, time, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "native", "jownloader_host.py")
BROWSER_ENV = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}   # how Brave launches the host: login PATH, HOME, nothing else
# the interpreter the browser's PATH resolves (Xcode CLT python3) — proves the host re-execs into the venv itself
BOOT_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
fails = 0
def engine_audio_args(mp3):   # the literal contract: default = no conversion, mp3 = V0
    return ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"] if mp3 else ["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "best"]
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
def call(msg):
    data = json.dumps(msg).encode()
    p = subprocess.run([BOOT_PY, HOST], input=struct.pack("<I", len(data)) + data,
                        capture_output=True, env=BROWSER_ENV)
    n = struct.unpack("<I", p.stdout[:4])[0]
    return json.loads(p.stdout[4:4 + n])
def _read_n(stream, n, deadline):
    buf = b""
    while len(buf) < n:
        remaining = deadline - time.time()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise TimeoutError("host did not reply in time")
        chunk = os.read(stream.fileno(), n - len(buf))   # unbuffered: a buffered read() would slurp the
        if not chunk:                                      # rest of the reply and starve the next select()
            raise EOFError("host closed without replying")
        buf += chunk
    return buf
def call_port(msg, timeout=180, env=None):
    # setup/fetch reply asynchronously from a background thread on the persistent port — closing
    # stdin right after the request (like call() does) races the daemon thread's send() against
    # process exit, so keep stdin open and read exactly one framed reply instead.
    data = json.dumps(msg).encode()
    p = subprocess.Popen([BOOT_PY, HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env or BROWSER_ENV)
    try:
        p.stdin.write(struct.pack("<I", len(data)) + data); p.stdin.flush()
        deadline = time.time() + timeout
        n = struct.unpack("<I", _read_n(p.stdout, 4, deadline))[0]
        return json.loads(_read_n(p.stdout, n, deadline))
    finally:
        try: p.stdin.close()
        except Exception: pass
        p.terminate()
        try: p.wait(timeout=5)
        except Exception: p.kill()

d = tempfile.mkdtemp(prefix="jown-rename-")
try:
    src, dst = os.path.join(d, "src"), os.path.join(d, "dst")
    os.makedirs(src)
    for n in ("a.mp4", "b.JPG", "c.mp4"): open(os.path.join(src, n), "w").write(n)
    open(os.path.join(src, "clip_2.mp4"), "w").write("taken")
    r = call({"cmd": "rename", "mode": "move", "ops": [
        {"src": src + "/a.mp4", "dst_dir": "", "name": "clip_1.mp4"},
        {"src": src + "/c.mp4", "dst_dir": "", "name": "clip_2.mp4"},       # taken → clip_2 (2).mp4
        {"src": src + "/missing.mp4", "dst_dir": "", "name": "clip_3.mp4"}]})
    check("rename in place: ok, collision suffixed, missing file fails on its own",
          [(x["ok"], os.path.basename(x.get("path", ""))) for x in r["results"]],
          [(True, "clip_1.mp4"), (True, "clip_2 (2).mp4"), (False, "")])
    check("originals gone, taken file untouched", sorted(os.listdir(src)), ["b.JPG", "clip_1.mp4", "clip_2 (2).mp4", "clip_2.mp4"])
    check("taken file content intact", open(src + "/clip_2.mp4").read(), "taken")
    r = call({"cmd": "rename", "mode": "copy", "ops": [{"src": src + "/b.JPG", "dst_dir": dst, "name": "pic_1.jpg"}]})
    check("copy to new folder", (r["results"][0]["ok"], sorted(os.listdir(dst)), os.path.exists(src + "/b.JPG")), (True, ["pic_1.jpg"], True))
    r = call({"cmd": "rename", "mode": "move", "ops": [{"src": src + "/clip_1.mp4", "dst_dir": dst, "name": "clip_1.mp4"}]})
    check("move to folder", (r["results"][0]["ok"], sorted(os.listdir(dst)), os.path.exists(src + "/clip_1.mp4")), (True, ["clip_1.mp4", "pic_1.jpg"], False))
    check("unknown cmd still refused", call({"cmd": "nope"})["ok"], False)

    # deps: fast presence check, never installs anything (setup actually installs — not run in a test)
    r = call({"cmd": "deps"})
    names = ["yt-dlp", "ffmpeg", "gallery-dl", "exiftool", "pypdf", "analyzer"]
    check("deps reply ok", r["ok"], True)
    check("deps reply carries a hints dict (empty unless a known cause)", isinstance(r.get("hints"), dict), True)
    check("deps reply has all six names, each a bool",
          [n in r.get("deps", {}) and isinstance(r["deps"][n].get("present"), bool) for n in names], [True] * 6)

    # the host re-execs into native/.venv when it exists (deps.sh's ensure_venv) — launched here the
    # way the browser launches it (env PATH=/usr/bin:/bin only), so this is the real path, not a guess.
    venv_py = os.path.join(os.path.dirname(HOST), ".venv", "bin", "python3")   # literal, not realpath: the
    # venv symlink resolves to the same binary the venv was built from, which would pass without any re-exec
    if os.path.exists(venv_py):
        check("deps reply names the venv python", r.get("python", ""), venv_py)
        check("pypdf present when the venv has it", r["deps"]["pypdf"]["present"], True)

        # --deps CLI mode (used by setup_async to re-check from a fresh process when the port
        # process itself launched too early to have re-execed): prints json and exits, no framing.
        cp = subprocess.run([BOOT_PY, HOST, "--deps"], capture_output=True, text=True,
                             env=BROWSER_ENV, timeout=30)
        info = json.loads(cp.stdout)
        check("--deps CLI mode answers from the venv", info.get("python", ""), venv_py)
        check("--deps CLI mode: pypdf present", info["deps"]["pypdf"]["present"], True)

        # setup over the persistent port, venv already fully populated: ensure_deps has nothing left
        # to install, so this should come back quickly with nothing missing and the venv as "python".
        r = call_port({"cmd": "setup", "id": "s1"})
        check("setup reply ok", r.get("ok"), True)
        check("setup reply: nothing missing (venv already populated)", r.get("missing"), [])
        check("setup reply names the venv python", r.get("python", ""), venv_py)
    else:
        print("skip venv python check (native/.venv not built — run: source native/deps.sh && ensure_deps)")

    # finishing steps: WebP -> JPEG (sips) and strip metadata (exiftool), on files already on disk
    import importlib.util
    fin = os.path.join(d, "fin"); os.makedirs(fin)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=1", "-frames:v", "1", fin + "/pic.jpg"], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Comment=secret", "-Artist=me", fin + "/pic.jpg"], check=True)
    if importlib.util.find_spec("PIL"):
        from PIL import Image
        Image.new("RGB", (8, 8), (200, 30, 30)).save(fin + "/pic.webp", "WEBP")
        # pic.jpg already exists, so the converted webp must land as pic (2).jpg — never overwrite
        Image.new("RGB", (8, 8), (30, 30, 200)).save(fin + "/pic2.webp", "WEBP")
        r = call({"cmd": "finish", "paths": [fin + "/pic2.webp"], "webp": "png"})
        check("webp -> png", [(os.path.basename(x["path"]), x["ok"]) for x in r["results"]] + [open(fin + "/pic2.png", "rb").read(4) == b"\x89PNG"], [("pic2.png", True), True])
        r = call({"cmd": "finish", "paths": [fin + "/pic.webp", fin + "/pic.jpg"], "webp": "jpg", "strip": True})
        got = [(os.path.basename(x["path"]), x["ok"]) for x in r["results"]]
        check("webp -> jpg (name taken -> (2)), jpg stripped, both ok", got, [("pic (2).jpg", True), ("pic.jpg", True)])
        check("original .webp gone, the new jpg is a JPEG", [os.path.exists(fin + "/pic.webp"), open(fin + "/pic (2).jpg", "rb").read(2) == b"\xff\xd8"], [False, True])
    else:
        print("skip webp check (no PIL to write a fixture)")
        r = call({"cmd": "finish", "paths": [fin + "/pic.jpg"], "strip": True})
        check("jpg stripped ok", r["results"][0]["ok"], True)
    tags = subprocess.run(["exiftool", "-S", "-Comment", "-Artist", fin + "/pic.jpg"], capture_output=True, text=True).stdout.strip()
    check("metadata gone after strip", tags, "")
    r = call({"cmd": "finish", "paths": [fin + "/missing.jpg"], "strip": True})
    check("missing file fails on its own", r["results"][0]["ok"], False)

    # ---- Spotify links: the track list from saved embed pages (no network), the flag that reaches the engine,
    # the match scoring, the tags and the never-overwrite rule. Fixtures: tests/fixtures/{track,album,playlist,artist}.html
    FIX = os.path.join(HERE, "fixtures")
    FENV = {**BROWSER_ENV, "JOWNLOADER_EMBED_DIR": FIX}
    def lst(url): return call_port({"cmd": "list", "id": "l1", "url": url}, env=FENV)
    r = lst("https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN?si=x")
    t0 = r.get("tracks", [{}])[0]
    check("list album: name, kind, 17 tracks, not truncated, id echoed", [r.get("ok"), r.get("name"), r.get("kind"), len(r.get("tracks", [])), r.get("truncated"), r.get("id")],
          [True, "Abbey Road (Remastered 2009)", "album", 17, False, "l1"])
    check("list album: first track fields", [t0.get("title"), t0.get("artists"), t0.get("album"), t0.get("number"), t0.get("duration_ms"), t0.get("url"), bool(t0.get("cover"))],
          ["Come Together - Remastered 2009", "The Beatles", "Abbey Road (Remastered 2009)", 1, 259946, "https://open.spotify.com/track/2EqlS6tkEnglzr7tkKAAYD", True])
    r = lst("https://open.spotify.com/intl-de/playlist/37i9dQZF1DWXRqgorJj26U")
    check("list playlist: 100 tracks (the embed's cap) = truncated, numbered 1..100; the embed has no per-track cover, so each track carries the list's", [r.get("ok"), len(r["tracks"]), r.get("truncated"), [t["number"] for t in r["tracks"]] == list(range(1, 101)), bool(r["cover"]) and r["tracks"][0]["cover"] == r["cover"]], [True, 100, True, True, True])
    r = lst("https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv")
    check("list track: one song, artists joined, length", [r.get("kind"), [(t["title"], t["artists"], t["duration_ms"]) for t in r["tracks"]]], ["track", [("Bohemian Rhapsody - Remastered 2011", "Queen", 354320)]])
    r = lst("https://open.spotify.com/artist/1dfeR4HaWDbWqFHLkxsg1d")
    check("list artist: top tracks only, said so", [r.get("kind"), len(r["tracks"]), "top tracks" in r.get("note", "")], ["artist", 10, True])
    r = lst("https://example.com/album/abc")
    check("list: not a Spotify link fails loud", [r.get("ok"), "Spotify" in r.get("output", "")], [False, True])

    DRY = {**BROWSER_ENV, "JOWNLOADER_DRYRUN": "1"}
    meta = {"title": "T", "artists": "A", "number": 7, "total": 12, "duration_ms": 1000, "url": "https://open.spotify.com/track/abc", "folder": "My List"}
    r = call_port({"cmd": "fetch", "id": "f1", "url": meta["url"], "mode": "audio", "dst": d, "meta": meta, "analyze": True}, env=DRY)
    check("fetch spotify: analyze=true, folder and number reach the engine", r.get("output"), 'dry-run {"analyze": true, "mp3": false, "args": %s, "folder": "My List", "number": 7}' % json.dumps(engine_audio_args(False)))
    r = call_port({"cmd": "fetch", "id": "f2", "url": meta["url"], "mode": "audio", "dst": d, "meta": meta}, env=DRY)
    check("fetch spotify: analyze defaults off", r.get("output"), 'dry-run {"analyze": false, "mp3": false, "args": %s, "folder": "My List", "number": 7}' % json.dumps(engine_audio_args(False)))
    r = call_port({"cmd": "fetch", "id": "f2b", "url": meta["url"], "mode": "audio", "dst": d, "meta": meta, "mp3": True}, env=DRY)
    check("fetch spotify: mp3=true reaches the engine with the conversion args", r.get("output"), 'dry-run {"analyze": false, "mp3": true, "args": %s, "folder": "My List", "number": 7}' % json.dumps(engine_audio_args(True)))
    yt = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    r = call_port({"cmd": "fetch", "id": "f2c", "url": yt, "mode": "audio", "dst": d}, env=DRY)
    check("fetch single-link audio: default = AAC as delivered, no conversion args", json.loads(r["output"][len("dry-run "):]), {"mp3": False, "args": engine_audio_args(False)})
    r = call_port({"cmd": "fetch", "id": "f2d", "url": yt, "mode": "audio", "dst": d, "mp3": True}, env=DRY)
    check("fetch single-link audio: mp3=true -> MP3 V0 conversion args", json.loads(r["output"][len("dry-run "):]), {"mp3": True, "args": engine_audio_args(True)})
    r = call_port({"cmd": "fetch", "id": "f3", "url": meta["url"], "mode": "audio", "dst": d}, env=DRY)
    check("fetch spotify without a list entry fails loud", [r.get("ok"), "list" in r.get("output", "")], [False, True])

    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "native"))
    import engine
    # matching: a stand-in for the search (the real one is yt-dlp); scores decide, not search order
    trk = {"title": "Bohemian Rhapsody - Remastered 2011", "artists": "Queen", "duration_ms": 354320}
    def fake(hits): return lambda q: (hits, [])
    H = lambda title, ch, dur, url: {"title": title, "channel": ch, "duration": dur, "url": url}
    live = H("Queen - Bohemian Rhapsody (Live Aid 1985)", "Live Aid", 165, "u-live")
    vid = H("Queen - Bohemian Rhapsody (Official Video Remastered)", "Queen Official", 360, "u-video")
    topic = H("Bohemian Rhapsody - Remastered 2011", "Queen - Topic", 355, "u-topic")
    remix = H("Queen - Bohemian Rhapsody (Remix)", "Queen Official", 354, "u-remix")
    other = H("Some Cover Band - Bohemian Rhapsody", "Band", 354, "u-other")
    check("match: Topic upload within 3 s beats an official video 6 s off", engine.spotify_match(trk, fake([live, vid, topic]))[0], "u-topic")
    check("match: a remix at the right length loses to the 6-s-off original", engine.spotify_match(trk, fake([remix, vid]))[0], "u-video")
    check("match: wrong length (live, 165 s) is rejected outright", engine.spotify_match(trk, fake([live])), (None, "no matching upload found"))
    check("match: a title that does not contain the track's is rejected", engine.spotify_match(trk, fake([other]))[0], None)
    live_trk = {"title": "Live Forever", "artists": "Oasis", "duration_ms": 276000}
    check("match: 'live' in the track's own title is not a penalty", engine.spotify_match(live_trk, fake([H("Oasis - Live Forever", "Oasis", 277, "u1")]))[0], "u1")
    sym = {"title": "\u22c6.\u2729 \u02da", "artists": "Someone", "duration_ms": 120000}
    check("match: a symbol-only title still matches on length", engine.spotify_match(sym, fake([H("someone - whatever", "Someone", 121, "u2")]))[0], "u2")
    check("match: queries run full, then audio, then short", engine.spotify_queries({"title": "Song (feat. X) - Remastered", "artists": "A, B"}), ["A, B - song", "A song audio", "A song"])
    check("sites: spotify routes to its engine branch", [engine.site_of("https://open.spotify.com/album/x"), engine.site_of("https://open.spotify.com/intl-es/track/x"), engine.site_of("https://open.spotify.com/user/x")], ["spotify", "spotify", None])

    # the args that reach yt-dlp, both download paths, default and mp3 (yt-dlp itself stubbed)
    seen = []
    orig_yt = engine._yt
    engine._yt = lambda *a: (seen.append(list(a)) or (0, "", ""))
    for mp3 in (False, True):
        seen.clear(); engine._yt_download_native("https://example.test/v", d + "/x.%(ext)s", [], mp3)
        a = seen[0]; has_conv = "mp3" in a or "--audio-quality" in a
        check(f"_yt_download_native mp3={mp3}: conversion args {'present' if mp3 else 'absent'}", [has_conv, a[a.index("--audio-format") + 1]], [mp3, "mp3" if mp3 else "best"])
        seen.clear(); engine._yt_download("https://example.test/v", "audio", d + "/x.%(ext)s", [], mp3)
        a = seen[0]
        check(f"_yt_download audio mp3={mp3}: conversion args {'present' if mp3 else 'absent'}", [("--audio-quality" in a), a[a.index("--audio-format") + 1], a[a.index("-f") + 1]], [mp3, "mp3" if mp3 else "best", "bestaudio/best" if mp3 else "bestaudio[ext=m4a]/bestaudio"])
    seen.clear(); engine._yt_download("https://example.test/v", "video", d + "/x.%(ext)s", [], True)
    check("_yt_download video: the mp3 flag never touches video", "--audio-format" in seen[0], False)
    engine._yt = orig_yt

    # the file side needs mutagen (native/.venv): tags written into a real m4a, never overwritten
    if os.path.exists(venv_py) and subprocess.run([venv_py, "-c", "import mutagen"], capture_output=True).returncode == 0:
        sp = os.path.join(d, "sp"); os.makedirs(sp)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac", sp + "/s.m4a"], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1", "-frames:v", "1", sp + "/c.jpg"], check=True)
        probe = """
import sys, json; sys.path.insert(0, sys.argv[1]); import engine
from mutagen.mp4 import MP4
m = {"title": "Song", "artists": "A, B", "album": "Alb", "number": 3, "total": 12}
cover = open(sys.argv[2] + "/c.jpg", "rb").read()
note = engine.spotify_tag(sys.argv[2] + "/s.m4a", m, cover, "1975-11-21")
f = MP4(sys.argv[2] + "/s.m4a")
print(json.dumps([note, f["\\xa9nam"], f["\\xa9ART"], f["\\xa9alb"], f["trkn"], f["\\xa9day"], len(f["covr"][0]) > 100, f.info.codec]))
"""
        out = subprocess.run([venv_py, "-c", probe, os.path.join(os.path.dirname(HERE), "native"), sp], capture_output=True, text=True)
        check("tags: title, artist, album, track n/total, year, cover in an m4a, still AAC",
              json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else out.stderr[-300:],
              ["", ["Song"], ["A, B"], ["Alb"], [[3, 12]], ["1975-11-21"], True, "mp4a.40.2"])
        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", "-q:a", "0", sp + "/s.mp3"], check=True)
            probe3 = """
import sys, json; sys.path.insert(0, sys.argv[1]); import engine
from mutagen.id3 import ID3
m = {"title": "Song", "artists": "A, B", "album": "Alb", "number": 3, "total": 12}
cover = open(sys.argv[2] + "/c.jpg", "rb").read()
note = engine.spotify_tag(sys.argv[2] + "/s.mp3", m, cover, "1975-11-21")
f = ID3(sys.argv[2] + "/s.mp3")
print(json.dumps([note, str(f["TIT2"]), str(f["TPE1"]), str(f["TALB"]), str(f["TRCK"]), str(f["TDRC"]), len(f.getall("APIC")[0].data) > 100]))
"""
            out = subprocess.run([venv_py, "-c", probe3, os.path.join(os.path.dirname(HERE), "native"), sp], capture_output=True, text=True)
            check("tags (mp3 switch): ID3 title, artist, album, track n/total, year, cover in an .mp3",
                  json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else out.stderr[-300:],
                  ["", "Song", "A, B", "Alb", "3/12", "1975-11-21", True])
            os.makedirs(sp + "/Mp3 List")
            open(sp + "/Mp3 List/03 - A, B - Song.mp3", "w").write("x")
            r = engine.spotify_track({"title": "Song", "artists": "A, B", "number": 3, "total": 12, "folder": "Mp3 List", "url": "https://open.spotify.com/track/x"}, sp, False, True)
            check("an existing .mp3 counts as the song too (matching ignores the extension), mp3 on or off", [r.get("ok"), r.get("skipped")], [True, 1])
        os.makedirs(sp + "/My List")
        open(sp + "/My List/03 - A, B - Song (71 BPM A# D# Cm).m4a", "w").write("x")
        r = engine.spotify_track({"title": "Song", "artists": "A, B", "number": 3, "total": 12, "folder": "My List", "url": "https://open.spotify.com/track/x"}, sp)
        check("an existing song (even renamed with BPM/key) is skipped, nothing fetched, nothing overwritten",
              [r.get("ok"), r.get("skipped"), os.listdir(sp + "/My List")], [True, 1, ["03 - A, B - Song (71 BPM A# D# Cm).m4a"]])
    else:
        print("skip tag checks (native/.venv has no mutagen — run: source native/deps.sh && ensure_deps)")

    # ---- matching rules learned the hard way: length is essential, symbol-only titles need artist + length, the
    # short last-resort query is never a blind first hit, cookie fallbacks reach the search, a dead cover link is harmless
    check("score: a hit with no length is refused (live streams)", engine.spotify_score(trk, H("Queen - Bohemian Rhapsody", "Queen", None, "x")), None)
    check("score: a 77-minute upload is refused", engine.spotify_score(trk, H("Queen - Bohemian Rhapsody (full concert)", "Queen", 4620, "x")), None)
    check("score: symbol-only title, artist absent from title and channel → refused", engine.spotify_score(sym, H("whatever", "Other Channel", 120, "x")), None)
    check("score: symbol-only title, artist present but 5 s off → refused", engine.spotify_score(sym, H("someone - x", "Someone", 125, "x")), None)
    short_hit = H("Queen - Bohemian Rhapsody (Official Video)", "Queen Official", 360, "u-short")   # 6 s off
    only_short = lambda hits: (lambda q: (hits, []) if q == "Queen bohemian" else ([], []))
    check("match: the shortened query refuses a hit 6 s off", engine.spotify_match(trk, only_short([short_hit]))[0], None)
    check("match: the shortened query takes a hit within 3 s", engine.spotify_match(trk, only_short([H("Queen - Bohemian Rhapsody", "Queen Official", 356, "u-short")]))[0], "u-short")
    saved_yt, calls = engine._yt, []
    def age_gate(*a):
        calls.append(a)
        if "youtube:player_client=android" in a:
            return 0, json.dumps({"id": "abc", "title": "Queen - Bohemian Rhapsody", "channel": "Queen", "duration": 354, "url": "https://www.youtube.com/watch?v=abc"}) + "\n", ""
        return 1, "", "ERROR: Sign in to confirm your age"
    engine._yt, engine._yt_working = age_gate, None
    try:
        hits, err = engine._yt_search("q")
        first = calls[0]
        check("search: Brave's cookies are the first thing tried", ("--cookies-from-browser", "brave") == (first[first.index("--cookies-from-browser")], first[first.index("--cookies-from-browser") + 1]), True)
        check("search: an age-gate failure falls through the cookie strategies to one that works", [len(hits), hits[0]["url"], len(calls) > 3], [1, "https://www.youtube.com/watch?v=abc", True])
        calls.clear(); engine._yt_search("q2")
        check("search: the strategy that worked goes first next time", "youtube:player_client=android" in calls[0], True)
    finally:
        engine._yt, engine._yt_working = saved_yt, None
    import http.server, threading
    class Dead(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_error(404)
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 9361), Dead); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        check("cover: a 404 link gives no picture instead of an error; no link likewise", [engine._cover_bytes("http://127.0.0.1:9361/gone.jpg"), engine._cover_bytes("")], [None, None])
    finally:
        srv.shutdown(); srv.server_close()

    # ---- running a list again: what is there is kept, renumbered to Spotify's current order, nothing deleted
    sy = os.path.join(d, "sy"); os.makedirs(sy + "/L")
    T = lambda a, t, n: {"artists": a, "title": t, "number": n}
    for n, (a, t) in enumerate([("A", "One"), ("B", "Two"), ("C", "Three")], 1):
        open(f"{sy}/L/{n:02d} - {a} - {t}.m4a", "w").write(t)
    sync = lambda tracks, **kw: call({"cmd": "sync", "dst": sy, "folder": "L", "tracks": tracks, "total": len(tracks), **kw})
    r = sync([T("A", "One", 1), T("B", "Two", 2), T("C", "Three", 3)])
    check("re-run: all three found, nothing renamed, nothing to download", [r["ok"], r["have"], r["renamed"], r["inlib"]], [True, [0, 1, 2], 0, []])
    r = sync([T("C", "Three", 1), T("A", "One", 2), T("B", "Two", 3)])
    check("reordered list: the files take the new numbers (a rotation, no collisions)", [r["renamed"], sorted(os.listdir(sy + "/L"))], [3, ["01 - C - Three.m4a", "02 - A - One.m4a", "03 - B - Two.m4a"]])
    check("...and keep their own content", [open(sy + "/L/01 - C - Three.m4a").read(), open(sy + "/L/02 - A - One.m4a").read()], ["Three", "One"])
    r = sync([T("C", "Three", 1), T("A", "One", 2), T("D", "Four", 3)])
    check("a song that left the list: its file stays; the new song is the only one missing", [r["have"], r["renamed"], sorted(os.listdir(sy + "/L"))], [[0, 1], 0, ["01 - C - Three.m4a", "02 - A - One.m4a", "03 - B - Two.m4a"]])
    os.makedirs(sy + "/M")
    for n in ("05_b - TWO.mp3", "002_A - One (71 BPM A# D# Cm).flac", "Z - Unrelated.m4a"): open(f"{sy}/M/{n}", "w").write(n)
    r = call({"cmd": "sync", "dst": sy, "folder": "M", "tracks": [T("A", "One", 1), T("B", "Two", 2)], "total": 2})
    check("match ignores case, the NN- / NN_ prefix, the extension and a BPM/key suffix; numbers and BPM text are kept right",
          [r["have"], sorted(os.listdir(sy + "/M"))], [[0, 1], ["01 - A - One (71 BPM A# D# Cm).flac", "02 - B - Two.mp3", "Z - Unrelated.m4a"]])
    lib = os.path.join(d, "lib"); os.makedirs(lib + "/Hits- Vol 1-")
    open(lib + "/Hits- Vol 1-/003_Artist - Title One.flac", "w").write("x")
    r = call({"cmd": "sync", "dst": sy, "folder": "Hits: Vol 1?", "tracks": [T("Artist", "Title One", 1), T("Artist", "Other", 2)], "total": 2, "libs": [lib, d + "/not-mounted"]})
    check("library folder: found by the library's own folder-name rule and NN_ names; a missing one is a note, not a failure",
          [r["ok"], r["inlib"], r["have"], [n.startswith("library folder not found") for n in r["notes"]], os.path.exists(sy + "/Hits_ Vol 1_")], [True, [0], [], [True], False])

    # site PDFs are named by page title, never 001.pdf, never overwritten
    import base64
    def session(msgs, host=HOST):
        blob = b"".join(struct.pack("<I", len(json.dumps(m).encode())) + json.dumps(m).encode() for m in msgs)
        p = subprocess.run([BOOT_PY, host], input=blob, capture_output=True, env=BROWSER_ENV)
        out, i = [], 0
        while i < len(p.stdout):
            n = struct.unpack("<I", p.stdout[i:i + 4])[0]; out.append(json.loads(p.stdout[i + 4:i + 4 + n])); i += 4 + n
        return out
    VENV_PY = os.path.join(os.path.dirname(HERE), "native", ".venv", "bin", "python3")
    def tracks(n, pdfd, real=False):
        m = []
        for k in range(n):
            data = b"%PDF-1.4 page" + bytes([48 + k])
            if real:   # a genuine one-page PDF, made by the venv's pypdf
                data = subprocess.run([VENV_PY, "-c", "import sys,io;from pypdf import PdfWriter;w=PdfWriter();w.add_blank_page(100,100);b=io.BytesIO();w.write(b);sys.stdout.buffer.write(b.getvalue())"], capture_output=True).stdout
            m += [{"cmd": "open", "id": f"t{k}", "name": f"x.{k}.pdf", "dst": pdfd, "temp": True},
                  {"cmd": "chunk", "id": f"t{k}", "data": base64.b64encode(data).decode()},
                  {"cmd": "close", "id": f"t{k}"}]
        return m
    pdfd = os.path.join(d, "pdf"); os.makedirs(pdfd)
    ids = lambda n: [f"t{k}" for k in range(n)]
    merge = lambda n, name, titles, dst: {"cmd": "mergepdf", "id": "m", "ids": ids(n), "name": name, "dst": dst, "titles": titles}
    session(tracks(1, pdfd) + [merge(1, "site", ["Portal:Current events"], pdfd)])
    session(tracks(1, pdfd) + [merge(1, "site", ["Portal:Current events"], pdfd)])
    check("one page -> <title>.pdf, second run -> (2), no tracks left", sorted(os.listdir(pdfd)), ["Portal:Current events (2).pdf", "Portal:Current events.pdf"])
    # several pages, pypdf present -> ONE merged PDF named after the first page's title (else the host name)
    if os.path.exists(VENV_PY):
        mp = os.path.join(d, "merged"); os.makedirs(mp)
        session(tracks(3, mp, real=True) + [merge(3, "docs.example.org", ["Intro / Start", "Guide", ""], mp)])
        check("several pages -> one merged PDF named by the first title", sorted(os.listdir(mp)), ["Intro - Start.pdf"])
        n = subprocess.run([VENV_PY, "-c", "import sys;from pypdf import PdfReader;print(len(PdfReader(sys.argv[1]).pages))", mp + "/Intro - Start.pdf"], capture_output=True, text=True).stdout.strip()
        check("merged PDF has all 3 pages", n, "3")
        mp2 = os.path.join(d, "merged2"); os.makedirs(mp2)
        session(tracks(2, mp2, real=True) + [merge(2, "docs.example.org", ["", "B"], mp2)])
        check("no first title -> <host>.pdf", sorted(os.listdir(mp2)), ["docs.example.org.pdf"])
    else:
        print("skip merged-PDF checks (no native/.venv with pypdf)")
    # no pypdf (a copy of the host with no venv, run by the browser's bare python) -> per-page title-named PDFs, never 001.pdf
    nov = os.path.join(d, "novenv"); os.makedirs(nov)
    for f in ("jownloader_host.py", "engine.py", "analyze.py"): shutil.copy(os.path.join(os.path.dirname(HOST), f), nov)
    fb = os.path.join(d, "fallback"); os.makedirs(fb)
    r = session(tracks(3, fb) + [merge(3, "docs.example.org", ["Intro / Start", "Guide: Part 1", ""], fb)], host=nov + "/jownloader_host.py")
    check("no pypdf: one PDF per page named by title, never 001.pdf", sorted(os.listdir(fb + "/docs.example.org")), ["Guide: Part 1.pdf", "Intro - Start.pdf", "docs.example.org page 3.pdf"])
    check("no pypdf: reply says so", "pypdf not installed" in (r[-1].get("output") or ""), True)
    check("no pypdf: page content intact", open(fb + "/docs.example.org/Intro - Start.pdf", "rb").read(), b"%PDF-1.4 page0")

    # a venv built on the SAME interpreter the browser launches (Apple's python3): the venv's python3 then
    # realpaths to the launching binary, so an executable comparison never re-execs. Fresh venv in a temp
    # dir + a copy of the host, launched with the browser's env; sys.prefix must land inside the venv.
    # JOWN_HOST_UNDER_TEST overrides the host file (used to show this FAILS on the old code).
    ap = os.path.join(d, "applevenv"); os.makedirs(ap)
    for f in ("jownloader_host.py", "engine.py", "analyze.py", "deps.sh"):
        shutil.copy(os.path.join(os.path.dirname(HOST), f), ap)
    if os.environ.get("JOWN_HOST_UNDER_TEST"): shutil.copy(os.environ["JOWN_HOST_UNDER_TEST"], os.path.join(ap, "jownloader_host.py"))
    mk = subprocess.run([BOOT_PY, "-m", "venv", os.path.join(ap, ".venv")], capture_output=True, text=True, env=BROWSER_ENV)
    if mk.returncode == 0:
        cp = subprocess.run([BOOT_PY, os.path.join(ap, "jownloader_host.py"), "--deps"], capture_output=True, text=True, env=BROWSER_ENV, timeout=60)
        info = json.loads(cp.stdout)
        check("venv built on the launching python: host re-execs into it (python = venv's)", os.path.realpath(os.path.dirname(info.get("python", "/"))), os.path.realpath(os.path.join(ap, ".venv", "bin")))
        check("venv built on the launching python: sys.prefix is the venv", os.path.realpath(info.get("prefix") or "/none"), os.path.realpath(os.path.join(ap, ".venv")))
    else:
        print("skip same-interpreter venv check (could not build a venv from " + BOOT_PY + ")")
finally:
    shutil.rmtree(d)
print("ALL GREEN" if not fails else f"{fails} FAILED")
