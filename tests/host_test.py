# Native-host rename test: drives native/jownloader_host.py over its stdio protocol with real files in a
# temp folder (no Finder dialog, no browser). Run: python3 tests/host_test.py
import json, os, select, struct, subprocess, sys, tempfile, time, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "native", "jownloader_host.py")
BROWSER_ENV = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}   # how Brave launches the host: login PATH, HOME, nothing else
# the interpreter the browser's PATH resolves (Xcode CLT python3) — proves the host re-execs into the venv itself
BOOT_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
fails = 0
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
def call_port(msg, timeout=180):
    # setup/fetch reply asynchronously from a background thread on the persistent port — closing
    # stdin right after the request (like call() does) races the daemon thread's send() against
    # process exit, so keep stdin open and read exactly one framed reply instead.
    data = json.dumps(msg).encode()
    p = subprocess.Popen([BOOT_PY, HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=BROWSER_ENV)
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
finally:
    shutil.rmtree(d)
print("ALL GREEN" if not fails else f"{fails} FAILED")
