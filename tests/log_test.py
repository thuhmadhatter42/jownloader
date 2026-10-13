# Developer-log test (0.1.44): the host over stdio, like host_test.py, plus the engine in-process.
# Everything is written to a temp log folder (JOWN_LOG_DIR), never ~/Library/Logs. No network, no browser cookies:
# yt-dlp is a stub script that shadows the real one, and the cookie strategies are replaced before it can run.
# Run: python3 tests/log_test.py
import json, os, re, select, shutil, struct, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(os.path.dirname(HERE), "native")
HOST = os.path.join(NATIVE, "jownloader_host.py")
BOOT_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
VENV_PY = os.path.join(NATIVE, ".venv", "bin", "python3")
RUN_PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))


def frame(m):
    b = json.dumps(m).encode()
    return struct.pack("<I", len(b)) + b


def parse(out):
    msgs, i = [], 0
    while i < len(out):
        n = struct.unpack("<I", out[i:i + 4])[0]
        msgs.append(json.loads(out[i + 4:i + 4 + n]))
        i += 4 + n
    return msgs


def env_for(logdir, **extra):
    return {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "JOWN_LOG_DIR": logdir, **extra}


def session(msgs, logdir, py=BOOT_PY, script=HOST, **extra):
    """All messages in, stdin closed, every reply out (sync commands only)."""
    p = subprocess.run([py, script], input=b"".join(frame(m) for m in msgs), capture_output=True, env=env_for(logdir, **extra), timeout=120)
    return parse(p.stdout)


def log_files(logdir):
    return sorted(f for f in os.listdir(logdir) if re.fullmatch(r"jownloader\.log(\.\d)?", f))


def read_log(logdir):
    """Every rotated file, oldest first."""
    names = sorted(log_files(logdir), key=lambda n: -int(n.rsplit(".", 1)[1]) if n[-1].isdigit() else 1)
    return "".join(open(os.path.join(logdir, n), encoding="utf-8").read() for n in names)


LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} (DEBUG|INFO |WARN |ERROR|CRIT ) \[\d+\] [\w.-]+: ")
root = tempfile.mkdtemp(prefix="jown-logtest-")
fresh = lambda name: (lambda d: (os.makedirs(d), d)[1])(os.path.join(root, name))
try:
    # ---- 1. the file is created; a message round trip is logged; file data is not
    L = fresh("basic")
    out = os.path.join(root, "out")
    B64 = "U0VDUkVUQklOQVJZUEFZTE9BRDEyMzQ1Njc4OTA="      # the bytes of a "file": must never appear in the log
    r = session([{"cmd": "exists", "paths": ["/etc/hosts", "/definitely/not/here"]},
                 {"cmd": "open", "id": "s1", "name": "a.bin", "dst": out},
                 {"cmd": "chunk", "id": "s1", "data": B64}, {"cmd": "chunk", "id": "s1", "data": B64},
                 {"cmd": "close", "id": "s1"},
                 {"cmd": "open", "id": "s2", "name": "a.bin", "dst": out}, {"cmd": "close", "id": "s2"},
                 {"cmd": "nope"}], L)
    text = read_log(L)
    check("1: the log file exists in JOWN_LOG_DIR", log_files(L), ["jownloader.log"])
    check("1: host start line: pid, python, prefix", bool(re.search(r"host: host start pid=\d+ ppid=\d+ python=\S+ \(3\.\d+\.\d+\) prefix=\S+", text)), True)
    check("1: venv / re-exec decision is stated", bool(re.search(r"host: (boot pid=\d+: outside native/\.venv .* re-exec into |host boot: (inside native/\.venv|native/\.venv does not exist))", text)), True)
    check("1: tool versions line", "host tools: yt-dlp=" in text or "host tools: " in text, True)
    check("1: message in (cmd, id, name, dst) and reply out (ok, path, bytes)",
          [bool(re.search(p, text)) for p in (r"msg in  cmd='open' id='s1' name='a\.bin' dst=", r"reply  id='s1' ok=True path=.*a\.bin", r"reply  id='s1' ok=True path=.* bytes=58")], [True] * 3)
    check("1: chunks are counted at close, never logged", ["close id=s1:" in text and "2 chunk(s), 58 bytes" in text, B64 not in text, "cmd='chunk'" not in text], [True, True, True])
    check("1: a name collision is logged (a.bin taken -> a (2).bin)", bool(re.search(r"name taken, reserved .*a \(2\)\.bin instead of .*a\.bin", text)), True)
    check("1: an unknown command is logged as a warning", bool(re.search(r"WARN.*host: unknown cmd 'nope'", text)), True)
    check("1: exists results are logged per path", "('/etc/hosts', True)" in text and "('/definitely/not/here', False)" in text, True)
    check("1: every line is a well-formed log line (no torn or stray lines)", [l for l in text.splitlines() if not LINE.match(l) and not l.startswith(("  ", "Traceback", " "))], [])
    check("1: host replies unchanged by logging", [bool(x.get("ok")) for x in r], [True, True, True, True, True, True, True, False])

    # ---- 2. JOWN_LOG=info quiets the debug lines (message in/out) but keeps the decisions
    L = fresh("quiet")
    session([{"cmd": "exists", "paths": ["/etc/hosts"]}, {"cmd": "open", "id": "q1", "name": "q.bin", "dst": out}, {"cmd": "close", "id": "q1"}], L, JOWN_LOG="info")
    text = read_log(L)
    check("2: JOWN_LOG=info drops DEBUG lines and keeps INFO ones", ["DEBUG" in text, "close id=q1:" in text, "host start" in text], [False, True, True])

    # ---- 3. extension entries land in the same file with their own time; cookie values never do; tail + open
    L = fresh("ext")
    stub_open = os.path.join(root, "fake-open.sh")
    with open(stub_open, "w") as f:
        f.write('#!/bin/sh\necho "$@" > "$0.args"\nexit 0\n')
    os.chmod(stub_open, 0o755)
    entries = [{"t": 1760000000123, "l": "info", "a": "save", "m": "job start file a.jpg", "d": {"key": "https://x.test/a.jpg"}},
               {"t": 1760000001000, "l": "error", "a": "job", "m": "FAILED file b.jpg: HTTP 403", "d": "Cookie: SID=EXTSECRETCOOKIE; HSID=EXTSECRET2"},
               {"t": 1760000002000, "l": "debug", "a": "popup.click", "m": "token=EXTSECRETTOKEN clicked"},
               {"t": 1760000003000, "l": "warn", "a": "host", "m": "headers sent", "d": {"authorization": "Bearer JSONSECRET1", "sessionid": "JSONSECRET2", "status": 403}}]
    r = session([{"cmd": "log", "entries": entries, "noreply": True}, {"cmd": "log", "entries": entries[:1]}, {"cmd": "taillog", "lines": 4}, {"cmd": "openlog"}], L, JOWN_OPEN=stub_open)
    text = read_log(L)
    check("3: noreply batch gets no answer, the plain one does, taillog + openlog answer", [x.get("n") for x in r[:1]] + [x.get("ok") for x in r[1:]], [1, True, True])
    check("3: extension entries keep their own timestamps and levels", bool(re.search(r"2025-10-09 \d\d:\d\d:\d\d\.123 INFO  \[\d+\] ext\.save: job start file a\.jpg  \{\"key\"", text)) and bool(re.search(r"ERROR \[\d+\] ext\.job: FAILED file b\.jpg", text)), True)
    check("3: cookie / token values from the extension are redacted (plain text and JSON data)", ["EXTSECRET" in text, "JSONSECRET" in text, "Cookie: ***" in text, "token=***" in text, '"status": 403' in text], [False, False, True, True, True])
    tail = r[1]
    check("3: taillog returns the last lines (4) of the file", [tail.get("lines"), len(tail["text"].splitlines()), tail["text"].splitlines()[-1] == text.splitlines()[-1] or "ext.save" in tail["text"]], [4, 4, True])
    check("3: openlog runs `open -a TextEdit <log>`", [r[-1].get("ok"), open(stub_open + ".args").read().strip() == f"-a TextEdit {os.path.join(L, 'jownloader.log')}"], [True, True])

    # ---- 4. a failing fake yt-dlp: argv, return code, stderr tail, strategy decisions; cookie-looking values redacted
    L = fresh("ytdlp")
    stubs = os.path.join(root, "stubbin")
    os.makedirs(stubs)
    with open(os.path.join(stubs, "yt-dlp"), "w") as f:
        f.write('#!/bin/sh\n'
                'echo "WARNING: [youtube] Cookie: SID=STUBSECRETCOOKIE1; HSID=STUBSECRETCOOKIE2" >&2\n'
                'echo "WARNING: sessionid=STUBSECRETSESSION and auth_token: STUBSECRETAUTH" >&2\n'
                'echo "ERROR: [youtube] abcdefghijk: Requested format is not available. Use --list-formats for a list of available formats" >&2\n'
                'exit 1\n')
    with open(os.path.join(stubs, "ffmpeg"), "w") as f:
        f.write('#!/bin/sh\nexit 0\n')
    for n in ("yt-dlp", "ffmpeg"):
        os.chmod(os.path.join(stubs, n), 0o755)
    driver = os.path.join(root, "driver.py")
    with open(driver, "w") as f:
        f.write(f'''import os, runpy, shutil, sys
sys.path.insert(0, {NATIVE!r})
import engine
os.environ["PATH"] = {stubs!r} + ":" + os.environ["PATH"]      # after engine put Homebrew first
engine.YT_STRATEGIES = [[], ["--extractor-args", "youtube:player_client=android"]]     # never a cookie store
if shutil.which("yt-dlp") != {os.path.join(stubs, "yt-dlp")!r}:
    sys.exit("the stub does not shadow yt-dlp: refusing to run")
sys.argv = [{HOST!r}]
runpy.run_path({HOST!r}, run_name="__main__")
''')
    url = "https://www.youtube.com/watch?v=abcdefghijk&token=STUBSECRETURLTOKEN"
    p = subprocess.Popen([RUN_PY, driver], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_for(L))
    p.stdin.write(frame({"cmd": "fetch", "id": "f1", "url": url, "mode": "video", "dst": os.path.join(root, "dl")})); p.stdin.flush()
    dl = time.time() + 60
    hdr = b""
    while len(hdr) < 4 and time.time() < dl:
        if select.select([p.stdout], [], [], 1)[0]:
            hdr += os.read(p.stdout.fileno(), 4 - len(hdr))
    body = b""
    n = struct.unpack("<I", hdr)[0]
    while len(body) < n and time.time() < dl:
        if select.select([p.stdout], [], [], 1)[0]:
            body += os.read(p.stdout.fileno(), n - len(body))
    reply = json.loads(body)
    p.stdin.close(); p.wait(timeout=20)
    text = read_log(L)
    check("4: the failure the user sees names the real cause", reply.get("output"), "YouTube refused the download (format not available).")
    check("4: the exact argv of every yt-dlp run is logged",
          [bool(re.search(r"exec  \S*yt-dlp --dump-json --no-playlist 'https://www\.youtube\.com/watch\?v=abcdefghijk&token=\*\*\*'  \[cwd=", text)),
           bool(re.search(r"exec  \S*yt-dlp --dump-json --no-playlist --extractor-args youtube:player_client=android ", text))], [True, True])
    check("4: return code, duration and the stderr tail of a failure", [bool(re.search(r"exec FAIL rc=1 \d+\.\d\ds yt-dlp", text)), "stderr| ERROR: [youtube] abcdefghijk: Requested format is not available" in text], [True, True])
    check("4: each strategy is tried and its error line recorded",
          [bool(re.search(r"engine: youtube info: strategy 1/2 \[default \(no extra args\)\] FAILED: ERROR: \[youtube\] abcdefghijk: Requested format", text)),
           bool(re.search(r"engine: youtube info: strategy 2/2 \[--extractor-args youtube:player_client=android\] FAILED", text)),
           bool(re.search(r"ERROR.*engine: youtube info: all 2 strategies failed", text))], [True, True, True])
    check("4: fetch start + outcome with the user-facing message", bool(re.search(r"engine: fetch: https://www\.youtube\.com/watch\?v=abcdefghijk&token=\*\*\* site=youtube mode=video", text)) and bool(re.search(r"ERROR.*fetch done in \d+\.\ds: ok=False .*YouTube refused the download", text)), True)
    check("4: cookie-looking values are redacted everywhere (argv, stderr tail, url token)", [s in text for s in ("STUBSECRETCOOKIE1", "STUBSECRETCOOKIE2", "STUBSECRETSESSION", "STUBSECRETAUTH", "STUBSECRETURLTOKEN")], [False] * 5)
    check("4: ...and the redaction marker is there", "Cookie: ***" in text and "sessionid=***" in text, True)

    # ---- 5. rotation: small files, nothing torn, newest in jownloader.log, oldest in .N
    L = fresh("rotate")
    N = 25
    session([{"cmd": "exists", "paths": [f"/rot/{i:03d}"]} for i in range(N)], L, JOWN_LOG_MAX_BYTES="2000")
    files = log_files(L)
    check("5: the log rotated (jownloader.log + numbered backups, at most 5 backups)", [files[0], len(files) > 1, len(files) <= 6], ["jownloader.log", True, True])
    sizes = [os.path.getsize(os.path.join(L, f)) for f in files]
    check("5: every file stays near the size limit (one line of slack)", max(sizes) < 2000 + 600, True)
    text = read_log(L)
    check("5: no line lost or torn across the rotation (every request appears once)", sorted(re.findall(r"exists: \[\('/rot/(\d{3})'", text)), [f"{i:03d}" for i in range(N)])

    # ---- 6. two hosts writing at once, with and without rotation underway
    for tag, mx in (("plain", None), ("rotating", "4000")):
        L = fresh("two-" + tag)
        extra = {"JOWN_LOG_MAX_BYTES": mx} if mx else {}
        K = 15 if mx else 40
        procs = []
        for who in "AB":
            pr = subprocess.Popen([BOOT_PY, HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env_for(L, **extra))
            procs.append((who, pr))
        for i in range(K):                                   # interleave the two senders message by message
            for who, pr in procs:
                pr.stdin.write(frame({"cmd": "exists", "paths": [f"/{who}/{i:03d}"]})); pr.stdin.flush()
        for who, pr in procs:
            pr.stdin.close()
        for who, pr in procs:
            pr.stdout.read(); pr.wait(timeout=60)
        text = read_log(L)
        for who in "AB":
            got = sorted(re.findall(rf"exists: \[\('/{who}/(\d{{3}})'", text))
            check(f"6 ({tag}): host {who}: all {K} requests are in the file", got, [f"{i:03d}" for i in range(K)])
        pids = set(re.findall(r"^\S+ \S+ \S+\s+\[(\d+)\] host: host start", text, re.M))
        check(f"6 ({tag}): two different processes wrote", len(pids), 2)
        check(f"6 ({tag}): no torn lines", [l for l in text.splitlines() if not LINE.match(l) and not l.startswith(" ")], [])

    # ---- 7. the engine in-process: Spotify decisions (why a hit lost), sync renames, finishing steps
    L = fresh("engine")
    os.environ["JOWN_LOG_DIR"] = L
    sys.path.insert(0, NATIVE)
    import engine
    track = {"title": "Floaty", "artists": "Some Artist", "duration_ms": 200000, "number": 6}
    H = lambda t, ch, d, u: {"title": t, "channel": ch, "duration": d, "url": u}
    hits = [H("Some Artist - Floaty (Official Audio)", "Some Artist - Topic", 201, "https://y/good"),
            H("Floaty (slowed + reverb)", "Random", 240, "https://y/slow"),
            H("Other Song", "Some Artist", 200, "https://y/other"),
            H("Floaty", "Some Artist", 400, "https://y/long")]
    urls, msg = engine.spotify_candidates(track, search=lambda q: (hits, []))
    text = read_log(L)
    check("7: the chosen candidate", urls, ["https://y/good"])
    check("7: queries are logged", bool(re.search(r"spotify: candidates for Some Artist - Floaty \(200\.0s\): 3 queries \[", text)), True)
    check("7: an accepted hit shows its score parts", bool(re.search(r"ACCEPT score=8 .*https://y/good: duration gap 1\.0s \+4, main artist in channel \+1, '- Topic' channel \+3", text)) or bool(re.search(r"ACCEPT score=9 .*https://y/good", text)), True)
    check("7: rejection reasons: duration, title, artist", [bool(re.search(r"REJECT .*https://y/long: duration off by 200s", text)), bool(re.search(r"REJECT .*https://y/other: title 'floaty' is not in the upload's title", text)),
                                                            bool(re.search(r"REJECT .*https://y/slow: none of the artists", text))], [True, True, True])
    check("7: the final choice is logged with its score", bool(re.search(r"candidates for Some Artist - Floaty: chosen \[\('https://y/good', \d+\)\]", text)), True)
    dst = os.path.join(root, "sync"); folder = os.path.join(dst, "List")
    os.makedirs(folder)
    open(os.path.join(folder, "05 - Some Artist - Floaty.m4a"), "w").write("x")
    res = engine.spotify_sync(dst, "List", [{"title": "Floaty", "artists": "Some Artist", "number": 6}], [], 12)
    text = read_log(L)
    check("7: sync renames are logged old -> new, with the match", [res["renamed"], "have #6 Some Artist - Floaty <- 05 - Some Artist - Floaty.m4a" in text, "rename 05 - Some Artist - Floaty.m4a -> 06 - Some Artist - Floaty.m4a" in text], [1, True, True])
    pics = os.path.join(root, "pics"); os.makedirs(pics)
    open(os.path.join(pics, "a.webp"), "wb").write(b"not really a webp")
    engine.finish([os.path.join(pics, "a.webp")], strip=False, webp="jpg")
    check("7: a finishing step that fails is logged with the tool's command line", bool(re.search(r"exec  /usr/bin/sips -s format jpeg -s formatOptions 100 .*a\.webp --out", read_log(L))), True)
    s2 = engine.spotify_list("https://example.com/not-spotify")
    check("7: list errors are logged", [s2["ok"], "list: not a Spotify track" in read_log(L)], [False, True])
finally:
    shutil.rmtree(root, ignore_errors=True)
print("ALL GREEN" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
