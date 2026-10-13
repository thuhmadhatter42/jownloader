# Stop/cancel test: drives native/jownloader_host.py over its stdio port with stand-in yt-dlp / gallery-dl / ffmpeg
# executables (a temp folder put first on the engine's PATH via the test-only env var JOWN_TOOL_DIR), so no network,
# no browser and no real downloader runs. Run: python3 tests/host_cancel_test.py
import json, os, queue, shutil, signal, struct, subprocess, sys, tempfile, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "native", "jownloader_host.py")
ROOT = tempfile.mkdtemp(prefix="jown-cancel-")
LOGDIR = os.path.join(ROOT, "log"); os.makedirs(LOGDIR)
TOOLS = os.path.join(ROOT, "tools"); os.makedirs(TOOLS)
STATE = os.path.join(ROOT, "state"); os.makedirs(STATE)
BOOT_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
ENV = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "JOWN_LOG_DIR": LOGDIR, "JOWN_TOOL_DIR": TOOLS, "FAKE_STATE": STATE}
fails = 0


def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))


def tool(name, body):
    p = os.path.join(TOOLS, name)
    with open(p, "w") as f: f.write(body)
    os.chmod(p, 0o755)


# stand-in yt-dlp: --dump-json answers at once; a download writes partial files, records its pid and its child's
# pid, then hangs. Every call is appended to $FAKE_STATE/calls ("info" or "download").
tool("yt-dlp", r'''#!/bin/bash
T=""; INFO=0; PREV=""
for a in "$@"; do
  [ "$a" = "--version" ] && { echo "2099.01.01"; exit 0; }      # the host asks every tool for its version at start
  [ "$a" = "--dump-json" ] && INFO=1
  [ "$PREV" = "-o" ] && T="$a"
  PREV="$a"
done
if [ $INFO = 1 ]; then echo "info" >> "$FAKE_STATE/calls"; echo '{"title":"Clip"}'; exit 0; fi
echo "download" >> "$FAKE_STATE/calls"
[ -z "$T" ] && { echo "fake yt-dlp: no -o template: $*" >> "$FAKE_STATE/bad"; exit 3; }
B="${T%.%(ext)s}"
: > "$B.mp4.part"; : > "$B.f137.mp4.part"; : > "$B.mp4.ytdl"
echo $$ > "$FAKE_STATE/ytdlp.pid"
sleep 120 &
echo $! > "$FAKE_STATE/ytdlp.child"
wait
''')
tool("ffmpeg", "#!/bin/sh\nexit 0\n")
# gallery-dl: the engine reads its interpreter from the shebang and runs `<interp> -c <probe>` (cookie-store probe,
# "is yt_dlp importable"). The stand-in is a python script: the probe fails to import gallery_dl (so no browser cookie
# store is ever touched) and a real call runs the body below with the real arguments.
tool("gallery-dl", """#!/usr/bin/python3 -I
import os, subprocess, sys
if "--version" in sys.argv:
    print("2099.01.01"); sys.exit(0)
base = [a.split("=", 1)[1] for a in sys.argv if a.startswith("extractor.base-directory=")][0]
d = os.path.join(base, "instagram", "someone"); os.makedirs(d, exist_ok=True)
open(os.path.join(d, "done.jpg"), "w").write("x")
print(os.path.join(d, "done.jpg"), flush=True)
open(os.path.join(d, "next.jpg.part"), "w").close()
st = os.environ["FAKE_STATE"]
open(os.path.join(st, "gdl.pid"), "w").write(str(os.getpid()))
c = subprocess.Popen(["sleep", "120"])
open(os.path.join(st, "gdl.child"), "w").write(str(c.pid))
c.wait()
""")


class Host:
    def __init__(self):
        self.p = subprocess.Popen([BOOT_PY, HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV)
        self.q = queue.Queue(); self.seen = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                h = self.p.stdout.read(4)
                if len(h) < 4: return
                self.q.put(json.loads(self.p.stdout.read(struct.unpack("<I", h)[0])))
        except Exception:
            pass

    def send(self, msg):
        d = json.dumps(msg).encode()
        self.p.stdin.write(struct.pack("<I", len(d)) + d); self.p.stdin.flush()

    def wait(self, mid, timeout=10):
        """The reply with this id (replies of other ids that arrive first are kept)."""
        end = time.time() + timeout
        while mid not in self.seen:
            try: m = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty: return None
            self.seen[m.get("id")] = m
        return self.seen.pop(mid)

    def close(self):
        try: self.p.stdin.close()
        except Exception: pass
        try: self.p.wait(timeout=5)
        except Exception: self.p.kill()


def until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


def readpid(name):
    try: return int(open(os.path.join(STATE, name)).read().strip())
    except (OSError, ValueError): return 0


def alive(pid):
    if not pid: return False
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def reset_state():
    for n in os.listdir(STATE): os.remove(os.path.join(STATE, n))


def calls(kind):
    try: return open(os.path.join(STATE, "calls")).read().split().count(kind)
    except OSError: return 0


def strays():
    # only processes whose command line carries this test's temp folder
    out = subprocess.run(["/usr/bin/pgrep", "-f", ROOT], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if int(x) != os.getpid()]


def started(files, pidfile):
    return until(lambda: all(os.path.exists(f) for f in files) and readpid(pidfile) and readpid(pidfile.replace("pid", "child")))


STOPPED = lambda i: {"id": i, "ok": False, "stopped": True, "output": "stopped"}
try:
    # ---------------------------------------------------------------- 1. a running yt-dlp fetch is stopped
    out1 = os.path.join(ROOT, "out1"); os.makedirs(out1)
    open(os.path.join(out1, "Other.mp4"), "w").write("finished earlier")
    h = Host()
    h.send({"cmd": "fetch", "id": "s1", "url": "https://www.youtube.com/watch?v=abc123", "mode": "video", "dst": out1})
    ok = started([os.path.join(out1, "Clip.mp4.part"), os.path.join(out1, "Clip.f137.mp4.part")], "ytdlp.pid")
    check("1 fake yt-dlp is downloading (parts written, pids recorded)", ok, True)
    pid, child = readpid("ytdlp.pid"), readpid("ytdlp.child")
    check("1 both processes are alive before the cancel", (alive(pid), alive(child)), (True, True))
    t0 = time.time()
    h.send({"cmd": "cancel", "id": "c1", "jobs": ["s1", "nope"]})
    rc = h.wait("c1", 5); rf = h.wait("s1", 8)
    check("1 cancel reply: ok, only the found job listed, killed counted",
          ((rc or {}).get("ok"), (rc or {}).get("cancelled"), (rc or {}).get("killed", 0) >= 1), (True, ["s1"], True))
    check("1 fetch reply", rf, STOPPED("s1"))
    check("1 fetch reply within 8 s", time.time() - t0 < 8, True)
    time.sleep(1.0)
    check("1 yt-dlp and its sleep child are dead", (alive(pid), alive(child)), (False, False))
    check("1 partial files removed, finished file untouched", sorted(os.listdir(out1)), ["Other.mp4"])
    check("1 finished file content intact", open(os.path.join(out1, "Other.mp4")).read(), "finished earlier")
    check("1 download started exactly once (no further strategy after the cancel)", calls("download"), 1)
    check("1 no second reply for the fetch", h.q.empty(), True)
    h.close()

    # ---------------------------------------------------------------- 2. an open streaming file
    out2 = os.path.join(ROOT, "out2"); os.makedirs(out2)
    h = Host()
    h.send({"cmd": "open", "id": "o1", "name": "a.bin", "dst": out2})
    check("2 open ok", (h.wait("o1") or {}).get("ok"), True)
    h.send({"cmd": "chunk", "id": "o1", "data": "aGVsbG8="})
    check("2 chunk acked", (h.wait("o1") or {}).get("ok"), True)
    check("2 .jownloading exists", os.listdir(out2), ["a.bin.jownloading"])
    h.send({"cmd": "cancel", "id": "c2", "jobs": ["o1"]})
    r = h.wait("c2") or {}
    check("2 cancel reply lists it", (r.get("ok"), r.get("cancelled"), r.get("killed")), (True, ["o1"], 0))
    check("2 .jownloading removed", os.listdir(out2), [])
    h.send({"cmd": "chunk", "id": "o1", "data": "aGVsbG8="})
    check("2 a chunk for the cancelled id is refused", (h.wait("o1") or {}).get("ok"), False)
    h.close()

    # ---------------------------------------------------------------- 3. all:true
    reset_state()
    out3 = os.path.join(ROOT, "out3"); os.makedirs(out3)
    h = Host()
    h.send({"cmd": "fetch", "id": "s3", "url": "https://www.youtube.com/watch?v=zzz", "mode": "video", "dst": out3})
    h.send({"cmd": "open", "id": "o3", "name": "b.bin", "dst": out3})
    h.send({"cmd": "open", "id": "t3", "name": "c.ts", "dst": out3, "temp": True})
    h.wait("o3"); h.wait("t3")
    h.send({"cmd": "chunk", "id": "t3", "data": "aGVsbG8="}); h.wait("t3")
    h.send({"cmd": "close", "id": "t3"}); h.wait("t3")
    ok = started([os.path.join(out3, "Clip.mp4.part")], "ytdlp.pid")
    check("3 fetch running, one open file, one closed temp track",
          (ok, sorted(os.listdir(out3))), (True, sorted(["Clip.f137.mp4.part", "Clip.mp4.part", "Clip.mp4.ytdl", "b.bin.jownloading", "c.ts.jownloading"])))
    pid, child = readpid("ytdlp.pid"), readpid("ytdlp.child")
    h.send({"cmd": "cancel", "id": "c3", "all": True})
    r = h.wait("c3") or {}; rf = h.wait("s3", 8)
    check("3 cancel all: every job listed", sorted(r.get("cancelled", [])), ["o3", "s3", "t3"])
    check("3 fetch stopped", rf, STOPPED("s3"))
    time.sleep(1.0)
    check("3 processes dead, folder empty", (alive(pid), alive(child), os.listdir(out3)), (False, False, []))
    h.close()

    # ---------------------------------------------------------------- 4. gallery-dl
    reset_state()
    out4 = os.path.join(ROOT, "out4"); os.makedirs(out4)
    h = Host()
    h.send({"cmd": "fetch", "id": "s4", "url": "https://www.instagram.com/p/ABC123/", "mode": "video", "dst": out4})
    part = os.path.join(out4, "instagram", "someone", "next.jpg.part")
    done = os.path.join(out4, "instagram", "someone", "done.jpg")
    ok = started([part, done], "gdl.pid")
    check("4 fake gallery-dl running (finished file + .part, pids recorded)", ok, True)
    pid, child = readpid("gdl.pid"), readpid("gdl.child")
    h.send({"cmd": "cancel", "id": "c4", "jobs": ["s4"]})
    r = h.wait("c4") or {}; rf = h.wait("s4", 8)
    check("4 cancel reply", r.get("cancelled"), ["s4"])
    check("4 fetch reply", rf, STOPPED("s4"))
    time.sleep(1.0)
    check("4 gallery-dl and child dead", (alive(pid), alive(child)), (False, False))
    check("4 finished file kept, .part removed", (os.path.exists(done), os.path.exists(part)), (True, False))
    h.close()
finally:
    for p in strays():
        try: os.kill(p, signal.SIGKILL)
        except OSError: pass
    if os.environ.get("KEEP"): print("kept", ROOT)     # debugging: keep the temp folder (and its log)
    else: shutil.rmtree(ROOT, ignore_errors=True)

print(f"{fails} failure(s)")
sys.exit(1 if fails else 0)
