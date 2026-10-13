# Browser-session cookies test (0.1.46): the extension hands a site's cookies to the host as cookies.txt text for ONE
# fetch. Drives native/jownloader_host.py over its stdio port with a stand-in yt-dlp (no network, no browser, and
# never a real cookie store: HOME is an empty temp folder, only FAKE cookies are used). Checks that the text lands in
# a private temp file (dir 0700, file 0600), that `--cookies <file>` is the FIRST strategy of the job, that the file is
# gone after success, failure, Stop and host exit, and that no cookie value ever reaches the log.
# Run: python3 tests/cookies_test.py
import glob, json, os, queue, shutil, struct, subprocess, sys, tempfile, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "native", "jownloader_host.py")
ROOT = tempfile.mkdtemp(prefix="jown-cookies-test-")
LOGDIR = os.path.join(ROOT, "log"); os.makedirs(LOGDIR)
TOOLS = os.path.join(ROOT, "tools"); os.makedirs(TOOLS)
STATE = os.path.join(ROOT, "state"); os.makedirs(STATE)
FAKEHOME = os.path.join(ROOT, "home"); os.makedirs(FAKEHOME)      # no browser data folder in here: no cookie-store strategy is usable
TMP = os.path.join(ROOT, "tmp"); os.makedirs(TMP)
OUT = os.path.join(ROOT, "out"); os.makedirs(OUT)
BOOT_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
ENV = {"PATH": "/usr/bin:/bin", "HOME": FAKEHOME, "TMPDIR": TMP, "JOWN_LOG_DIR": LOGDIR, "JOWN_TOOL_DIR": TOOLS, "FAKE_STATE": STATE}
fails = 0
SECRETS = ["FAKESECRETVALUE111", "FAKESECRETVALUE222", "FAKESECRETVALUE333"]
COOKIES = "# Netscape HTTP Cookie File\n" + "\n".join([
    f".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\t{SECRETS[0]}",
    f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\t{SECRETS[1]}",
    f".google.com\tTRUE\t/\tTRUE\t2000000000\tNID\t{SECRETS[2]}",
]) + "\n"


def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))


def tool(name, body):
    p = os.path.join(TOOLS, name)
    with open(p, "w") as f: f.write(body)
    os.chmod(p, 0o755)


# stand-in yt-dlp. Every call appends its argv (one line) to $FAKE_STATE/argv; when it gets --cookies <file> it also
# records that file's mode, its folder's mode and a copy of its content. FAKE_MODE (a file) = ok | fail | hang.
tool("yt-dlp", r'''#!/bin/bash
T=""; INFO=0; PREV=""; C=""
for a in "$@"; do
  [ "$a" = "--version" ] && { echo "2099.01.01"; exit 0; }
  [ "$a" = "--dump-json" ] && INFO=1
  [ "$PREV" = "-o" ] && T="$a"
  [ "$PREV" = "--cookies" ] && C="$a"
  PREV="$a"
done
echo "$*" >> "$FAKE_STATE/argv"
if [ -n "$C" ]; then
  stat -f %Lp "$C" >> "$FAKE_STATE/filemode"; stat -f %Lp "$(dirname "$C")" >> "$FAKE_STATE/dirmode"
  echo "$C" >> "$FAKE_STATE/cookiepath"; cp "$C" "$FAKE_STATE/seen-cookies"
fi
MODE=$(cat "$FAKE_STATE/mode")
if [ $INFO = 1 ]; then echo '{"title":"Clip"}'; exit 0; fi
if [ "$MODE" = fail ]; then echo "ERROR: unable to download video data: HTTP Error 403: Forbidden" >&2; exit 1; fi
B="${T%.%(ext)s}"
if [ "$MODE" = hang ]; then : > "$B.mp4.part"; touch "$FAKE_STATE/hanging"; sleep 120 & wait; exit 0; fi
echo data > "$B.mp4"; echo "$B.mp4"
''')
tool("ffmpeg", "#!/bin/sh\nexit 0\n")


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

    def wait(self, mid, timeout=30):
        end = time.time() + timeout
        while mid not in self.seen:
            try: m = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty: return None
            self.seen[m.get("id")] = m
        return self.seen.pop(mid)

    def close(self):
        try: self.p.stdin.close()
        except Exception: pass
        try: self.p.wait(timeout=10)
        except Exception: self.p.kill()


def until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


def state(name):
    try: return open(os.path.join(STATE, name)).read().split()
    except OSError: return []


def reset(mode):
    for f in os.listdir(STATE): os.remove(os.path.join(STATE, f))
    open(os.path.join(STATE, "mode"), "w").write(mode)


def temp_left(): return glob.glob(os.path.join(TMP, "jownloader-cookies-*"))


def first_cookies_argv():
    lines = open(os.path.join(STATE, "argv")).read().splitlines() if os.path.exists(os.path.join(STATE, "argv")) else []
    return lines


try:
    # ---- success: the file is private, `--cookies <file>` leads, the file is gone afterwards
    reset("ok")
    h = Host()
    h.send({"cmd": "fetch", "id": "f1", "url": "https://www.youtube.com/watch?v=abc", "mode": "video", "dst": OUT, "cookies": COOKIES})
    r = h.wait("f1")
    check("success: the fetch is ok", bool(r and r.get("ok")), True)
    argv = first_cookies_argv()
    check("success: every yt-dlp call of the job carries --cookies <file> as its first strategy", [("--cookies " in a) for a in argv], [True] * len(argv))
    check("success: the very first call (the info lookup) used it", bool(argv) and "--dump-json" in argv[0] and "--cookies " in argv[0], True)
    check("success: the cookie file was 0600 and its folder 0700 while yt-dlp ran", [set(state("filemode")), set(state("dirmode"))], [{"600"}, {"700"}])
    seen = open(os.path.join(STATE, "seen-cookies")).read() if os.path.exists(os.path.join(STATE, "seen-cookies")) else ""
    check("success: yt-dlp saw the cookies.txt exactly as sent", seen, COOKIES)
    check("success: the temp file and its folder are deleted when the job ends", [os.path.exists(p) for p in state("cookiepath")[:1]] + [temp_left()], [False, []])
    h.send({"cmd": "fetch", "id": "f2", "url": "https://www.youtube.com/watch?v=abc", "mode": "video", "dst": OUT})
    r = h.wait("f2")
    check("no cookies sent: no --cookies argument anywhere", ["--cookies " in a for a in first_cookies_argv()[len(argv):]] and any("--cookies " in a for a in first_cookies_argv()[len(argv):]), False)
    h.close()

    # ---- failure: the file is gone, the YouTube refusal is what the user reads
    reset("fail")
    h = Host()
    h.send({"cmd": "fetch", "id": "f3", "url": "https://www.youtube.com/watch?v=abc", "mode": "video", "dst": OUT, "cookies": COOKIES})
    r = h.wait("f3")
    check("failure: the fetch fails and says what YouTube said (403)", [bool(r) and r.get("ok"), "403" in (r or {}).get("output", "")], [False, True])
    check("failure: the temp file and folder are deleted", [os.path.exists(p) for p in state("cookiepath")[:1]] + [temp_left()], [False, []])
    h.close()

    # ---- Stop: the file is gone once the job is cancelled
    reset("hang")
    h = Host()
    h.send({"cmd": "fetch", "id": "f4", "url": "https://www.youtube.com/watch?v=abc", "mode": "video", "dst": OUT, "cookies": COOKIES})
    check("stop: the download is running (hanging)", until(lambda: os.path.exists(os.path.join(STATE, "hanging"))), True)
    check("stop: while it runs the private file exists", [os.path.exists(p) for p in state("cookiepath")[:1]], [True])
    h.send({"cmd": "cancel", "id": "c4", "jobs": ["f4"]})
    r = h.wait("f4")
    check("stop: the fetch answers stopped", [bool(r) and r.get("ok"), bool(r) and r.get("stopped")], [False, True])
    check("stop: the temp file and folder are deleted", [os.path.exists(p) for p in state("cookiepath")[:1]] + [until(lambda: not temp_left())], [False, True])
    h.close()

    # ---- host exit mid-job: the file does not outlive the host
    reset("hang")
    h = Host()
    h.send({"cmd": "fetch", "id": "f5", "url": "https://www.youtube.com/watch?v=abc", "mode": "video", "dst": OUT, "cookies": COOKIES})
    until(lambda: os.path.exists(os.path.join(STATE, "hanging")))
    check("host exit: the file exists while the job runs", len(temp_left()), 1)
    h.close()
    check("host exit: the host process ended and nothing is left in the temp folder", [h.p.poll() is not None, until(lambda: not temp_left())], [True, True])

    # ---- the log: a count, never a value
    logs = glob.glob(os.path.join(LOGDIR, "jownloader.log*"))
    text = "".join(open(p, errors="replace").read() for p in logs)
    check("log: exists and says what was handed over", "cookies.txt from the browser: 3 cookies for 2 domains" in text, True)
    check("log: no cookie value anywhere", [s in text for s in SECRETS], [False] * 3)
    check("log: the incoming message is logged without the cookie lines", "values never logged" in text, True)
finally:
    subprocess.run(["pkill", "-f", ROOT], capture_output=True)
    shutil.rmtree(ROOT, ignore_errors=True)
print("ALL GREEN" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
