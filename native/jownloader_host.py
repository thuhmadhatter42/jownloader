#!/usr/bin/env python3
"""Jownloader native-messaging host.

The browser launches this with PATH=/usr/bin:/bin, so the shebang above only ever resolves to the
Xcode CLT python3 — that is the bootstrap, never where library deps (pypdf, the BPM/key analyzer)
live. Before anything else runs, this re-execs into native/.venv (built by deps.sh's ensure_venv) when
it exists, so the process that answers every command below is always the venv's interpreter.

The extension talks to it two ways:

Persistent port (one process per download batch) — streaming saves. The extension fetched the bytes
with the browser session; this side only writes them.
  {"cmd": "open",  "id", "name", "dst"}  -> {"id", "ok", "path"}      dst "" = ~/Downloads
  {"cmd": "chunk", "id", "data": base64}  -> {"id", "ok"}            acked: the extension sends the next only after this
  {"cmd": "close", "id"}                 -> {"id", "ok", "path", "bytes"}
  {"cmd": "abort", "id"}                 -> {"id", "ok": false}
  {"cmd": "join", "id", "ids": [...], "name", "dst"} -> {"id", "ok", "path", "bytes", "output"?}
     an HLS/DASH stream: "open" with "temp": true keeps each track as a temp file on close; "join" runs
     ffmpeg -c copy over the tracks (video, or video + audio) into name.mp4 and removes the temps.
     No ffmpeg on this Mac: one track is kept raw (name.ts / name.mp4), two are saved as
     name.video.* and name.audio.* with "output" saying so.
  {"cmd": "mergepdf", "id", "ids": [...], "name", "dst", "titles"?: [...], "split"?} -> {"id", "ok", "path", "bytes"?, "output"?}
     finishes closed temp-track PDFs (from "open" with "temp": true), in order. One page -> dst/<title>.pdf.
     Several pages -> merged with pypdf into one PDF named after the first page's title (else dst/<name>.pdf).
     No pypdf (or "split") -> one PDF per page named by its title in dst/<name>/ (never 001.pdf).
     Never overwrites: name (2).pdf.
  {"cmd": "fetch", "id", "url", "mode", "dst", "strip"?, "webp"?, "mp3"?, "cookies"?} -> {"id", "ok", "path", "files", "output"}
     a site the engine handles (YouTube, Instagram, Twitter/X — engine.py): runs in a thread so the
     port keeps streaming other files meanwhile. mode "video" | "audio". Audio is the upload's AAC saved
     as delivered (.m4a); "mp3": true converts it to MP3 V0 instead (single links and Spotify songs alike).
     A Spotify track also carries "meta" ({title, artists, album, number, total, duration_ms, cover, url,
     folder}) and "analyze": true|false (BPM/key into the name): the host matches it to an upload and saves
     <dst>/<folder>/NN - Artists - Title.m4a (AAC as the upload has it, never re-encoded), tagged.
     "cookies": the browser's own session for that site as cookies.txt text (the extension reads it with
     chrome.cookies). It goes to a private temp file (0700 dir, 0600 file) used as the first login source of
     that one job and deleted when the job ends (done, failed, stopped, host exit); never logged, never kept.
  {"cmd": "cancel", "id", "jobs": [ids]} or {"cmd": "cancel", "id", "all": true}
     -> {"id", "ok": true, "cancelled": [ids found], "killed": n}     Stop. Ids are the ids of earlier requests:
     a running "fetch" has its yt-dlp / gallery-dl / ffmpeg / analyzer processes killed with their children
     (SIGTERM to the process group, SIGKILL after 3 s) and its partial files removed (finished files stay);
     its own reply then comes once as {"id": <fetch id>, "ok": false, "stopped": true, "output": "stopped"}.
     A cancelled fetch starts nothing further. An open streaming file is aborted (.jownloading removed), a
     closed temp track removed; "all" does this for every running fetch, open file and temp track. Unknown ids
     are not listed (ok stays true).
  {"cmd": "sync", "id", "dst", "folder", "tracks": [{artists, title, number}], "total", "libs": [paths]}
     -> {"id", "ok", "have": [i...], "inlib": [i...], "renamed": n, "notes": [...]}
     before a Spotify list is queued: songs already in <dst>/<folder>/ are matched on "Artists - Title" (not the
     file's exact name) and renamed in place to the list's current numbering; songs found in
     <lib>/<folder>/ of an extra library folder count as downloaded. Nothing is deleted or overwritten.
  {"cmd": "list", "id", "url"} -> {"id", "ok", "name", "kind", "cover", "truncated", "tracks": [...]}
     a Spotify track / album / playlist / artist link read from its public embed page (own thread).
  {"cmd": "setup", "id"} -> {"id", "ok", "output", "missing": [...], "python": str}
     runs native/deps.sh's ensure_deps (brew installs can take minutes): its own thread, reply when
     done. "output" is the tail of what it printed; "missing" is whichever of the six deps below are
     still absent afterward (empty when everything installed clean); "python" is sys.executable —
     native/.venv's interpreter once it exists, so a persistent "missing" names which Python answered.
  {"cmd": "log", "entries": [{"t": ms, "l": level, "a": area, "m": text, "d": data}], "noreply"?: true}
     the extension's log entries, appended to the same file as the host's own (see jlog.py); never answered
     when "noreply" is set, so a log batch can ride the streaming port without disturbing it.
  {"cmd": "openlog"} -> {"ok", "path"}     opens the log in TextEdit (JOWN_OPEN overrides /usr/bin/open for tests)
  {"cmd": "taillog", "lines": 200} -> {"ok", "path", "text", "lines"}   the last lines of the log
  "open" / "join" / "fetch" take "strip": true (remove all metadata) and "webp": "jpg" | "png" (WebP -> that);
  both are applied to the finished file(s) before the reply (engine.finish).
Names are never overwritten: name (2).ext, name (3).ext …  A file is written as name.jownloading and
renamed on close, so a half file never looks finished.

One-shot (sendNativeMessage, own process each):
  {"cmd": "choose_dir"}                             -> {"ok", "path"}   Finder folder picker
  {"cmd": "exists", "paths": [...]}                 -> {"ok", "exists": [bool...]}   still on disk?
  {"cmd": "choose_files"}                           -> {"ok", "files": [{"path", "mtime"}...]}   Finder multi-file picker
  {"cmd": "rename", "mode": "move"|"copy", "ops": [{"src", "dst_dir", "name"}...]}
                                                    -> {"ok", "results": [{"ok", "path"} | {"ok": false, "output"}...]}
     batch rename: each src becomes dst_dir/name (dst_dir "" = the file's own folder); "copy" leaves
     the original in place. Never overwrites: name (2).ext …
  {"cmd": "finish", "paths": [...], "strip"?, "webp"?} -> {"ok", "results": [{"path", "ok", "output"}...]}
     the finishing steps on files already on disk (the Batch tab).
  {"cmd": "deps"} -> {"ok", "deps": {"yt-dlp"|"ffmpeg"|"gallery-dl"|"exiftool"|"pypdf"|"analyzer":
                                      {"present": bool, "version": str}}, "python": str}
     fast presence + version check of the six things native/deps.sh installs. Never installs anything.
     "python" is sys.executable and "prefix" is sys.prefix (the venv dir when re-exec worked), so the
     popup/tests can see which interpreter answered.
"""
import base64, importlib.metadata, importlib.util, json, os, re, shutil, struct, subprocess, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jlog     # stdlib only: importable under the bootstrap python3, before the venv re-exec
log = jlog.get("host")
jlog.install_hooks(log)

# Re-exec into native/.venv (deps.sh's ensure_venv) before any third-party import or stdin read: the
# shebang above is only the bootstrap (Brave launches us with PATH=/usr/bin:/bin -> Xcode CLT python3),
# and library deps (pypdf, the BPM/key analyzer) live only in the venv. Compare prefixes, never
# executables: the venv's python3 is a symlink to whatever built it, and when that is the very python3
# the browser launched us with the executables' realpaths match even though we are outside the venv.
# Inside a venv sys.prefix is the venv dir, whatever interpreter it links to.
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python3")


def _in_venv():
    return os.path.realpath(sys.prefix) == os.path.realpath(_VENV_DIR)


# The exec happens at most once: a marker in the environment (inherited by the exec'd process) stops a venv
# that does not report itself as one (pyvenv.cfg gone, python3 a shim for the bootstrap python) from being
# re-exec'd forever, and an exec that fails (not executable, wrong format) falls through. Either way the host
# keeps running in this interpreter, and _VENV_UNUSABLE makes the deps reply say what to do.
_REEXEC_MARK = "JOWNLOADER_HOST_REEXEC"
_VENV_UNUSABLE = os.path.lexists(_VENV_PY) and not os.path.exists(_VENV_PY)   # dangling python3 symlink
_BOOT_NOTE = "no native/.venv: running on " + sys.executable
if os.path.exists(_VENV_PY) and not _in_venv():
    if os.environ.get(_REEXEC_MARK):
        _VENV_UNUSABLE = True
        _BOOT_NOTE = "venv present but this process (after one re-exec) is still outside it: venv unusable"
        log.warning("boot pid=%d: %s (prefix=%s venv=%s)", os.getpid(), _BOOT_NOTE, sys.prefix, _VENV_DIR)
    else:
        os.environ[_REEXEC_MARK] = "1"
        log.info("boot pid=%d: outside native/.venv (python=%s prefix=%s), re-exec into %s", os.getpid(), sys.executable, sys.prefix, _VENV_PY)
        try:
            os.execv(_VENV_PY, [_VENV_PY, *sys.argv])
        except OSError as e:
            _VENV_UNUSABLE = True
            _BOOT_NOTE = f"re-exec into the venv failed: {e}"
            log.error("boot pid=%d: %s", os.getpid(), _BOOT_NOTE)
elif os.path.lexists(_VENV_PY):
    _BOOT_NOTE = "inside native/.venv" if _in_venv() else "venv python3 is a dangling symlink: venv unusable"
else:
    _BOOT_NOTE = "native/.venv does not exist: running on " + sys.executable

import engine

SEND_LOCK = threading.Lock()   # fetch replies come from worker threads


def read_msg():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    msg = json.loads(sys.stdin.buffer.read(n).decode())
    if msg.get("cmd") not in ("chunk", "log"):          # chunks are counted at close; log batches are the log itself
        log.debug("msg in  %s", jlog.summarize(msg))
    return msg


def send(obj, quiet=False):
    if not quiet:
        log.debug("reply  %s", jlog.summarize(obj))
    data = json.dumps(obj).encode()
    with SEND_LOCK:
        sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
        sys.stdout.buffer.flush()


def finished(reply, msg):
    """Apply the strip / webp switches carried by the request to the reply's file(s); the reply's
    path follows a conversion (x.webp -> x.jpg) and a failed step lands in "output"."""
    if not reply.get("ok") or not (msg.get("strip") or msg.get("webp")):
        return reply
    paths = reply.get("files") or ([reply["path"]] if reply.get("path") and os.path.isfile(reply["path"]) else [])
    if not paths:
        return reply
    log.info("finishing %d file(s): strip=%s webp=%s", len(paths), bool(msg.get("strip")), msg.get("webp") or False)
    res = engine.finish(paths, strip=bool(msg.get("strip")), webp=msg.get("webp") or False)["results"]
    reply["files"] = [r["path"] for r in res]
    if reply.get("path") in paths:
        reply["path"] = res[paths.index(reply["path"])]["path"]
    bad = [r for r in res if not r["ok"]]
    if bad:
        reply["output"] = (reply.get("output") + "; " if reply.get("output") else "") + bad[0]["output"]
    return reply


def fetch_async(msg):
    """The engine (yt-dlp / gallery-dl) can run for minutes: its own thread, reply when done."""
    jid = msg.get("id")
    if jid is not None:
        jlog.register_job(jid)      # in the reader thread: a cancel that follows at once finds the job

    def run():
        log.info("fetch thread start id=%s", jid)
        with jlog.job(jid) as j:
            try:
                r = engine.fetch(msg.get("url") or "", msg.get("mode") or "video", msg.get("dst") or "",
                                 msg.get("meta") or None, bool(msg.get("analyze")), bool(msg.get("mp3")),
                                 cookies=msg.get("cookies") or None)
            except Exception as e:
                log.exception("fetch id=%s raised", jid)
                r = {"ok": False, "output": str(e)}
            if j.cancelled.is_set():        # Stop: its processes are gone; remove its partial files, then say so (once)
                removed = jlog.run_cleanups(j)
                log.info("cancel id=%s job=%s: fetch stopped, removed %s", jid, jid, removed or "nothing")
                if r.get("ok") and r.get("path") and os.path.exists(r["path"]):
                    # it finished before the cancel landed: the file is real, so it is reported (and remembered as
                    # saved by the extension); only the finishing step is skipped
                    r["id"] = jid
                    log.info("cancel id=%s job=%s: the fetch had already finished (%s), reported as saved", jid, jid, r["path"])
                    send(r)
                    return
                send({"id": jid, "ok": False, "stopped": True, "output": "stopped"})
                return
            r["id"] = jid
            send(r if msg.get("meta") else finished(r, msg))   # a Spotify song keeps its tags: "strip metadata" would erase them
    threading.Thread(target=run, daemon=True).start()


def cancel(msg):
    """Stop: kill the engine processes of the named fetches (their threads then remove the partial files and
    reply "stopped"), abort named open streaming files, remove named closed temp tracks. all:true = everything."""
    rid, ids = msg.get("id"), list(msg.get("jobs") or [])
    if msg.get("all"):
        ids = list(dict.fromkeys(ids + jlog.running_jobs() + list(OPEN) + list(TEMP)))
    cancelled, killed = [], 0
    for jid in ids:
        found = False
        was, pids = jlog.cancel_job(jid, log)
        if was:
            found = True
            killed += len(pids)
            log.info("cancel id=%s job=%s: fetch marked cancelled, %d process(es) signalled", rid, jid, len(pids))
        st = OPEN.pop(jid, None)
        if st:
            found = True
            try: st["f"].close()
            except OSError: pass
            try: os.remove(st["path"] + ".jownloading")
            except OSError: pass
            log.info("cancel id=%s job=%s: open file %s.jownloading closed and removed (%d chunk(s), %d bytes)", rid, jid, st["path"], st["chunks"], st["bytes"])
        t = TEMP.pop(jid, None)
        if t:
            found = True
            try: os.remove(t)
            except OSError: pass
            log.info("cancel id=%s job=%s: temp track %s removed", rid, jid, t)
        if found:
            cancelled.append(jid)
    return {"id": rid, "ok": True, "cancelled": cancelled, "killed": killed}


def list_async(msg):
    """A Spotify link's track list: one page fetch, in its own thread so the port keeps streaming."""
    def run():
        try:
            r = engine.spotify_list(msg.get("url") or "")
        except Exception as e:
            log.exception("list id=%s raised", msg.get("id"))
            r = {"ok": False, "output": str(e)}
        r["id"] = msg.get("id")
        send(r)
    threading.Thread(target=run, daemon=True).start()


def _cli_version(cmd, args):
    """Present + a short version string, without running anything that installs or upgrades."""
    path = shutil.which(cmd)
    if not path:
        return False, ""
    rc, out, err = jlog.run([path, *args], log, timeout=5, ok_tail=0)
    if rc != 0 and not (out or err):
        return True, ""   # on PATH but wouldn't run — still "present" for the ✓/✗
    line = ((out or err or "").strip().splitlines() or [""])[0]
    return True, line[:80]


def _pkg_version(name):
    """find_spec avoids importing the package (some are slow); version comes from its own metadata."""
    if importlib.util.find_spec(name) is None:
        return False, ""
    try:
        return True, importlib.metadata.version(name)
    except Exception:
        return True, ""


def _analyzer_version():
    """bpm.py uses whichever of these is installed, essentia first (see native/deps.sh). The importable
    module is "essentia" but the pip distribution (and its version metadata) is "essentia-tensorflow"."""
    if importlib.util.find_spec("essentia") is not None:
        try:
            return True, "essentia " + importlib.metadata.version("essentia-tensorflow")
        except Exception:
            return True, "essentia"
    ok, ver = _pkg_version("librosa")
    if ok:
        return True, "librosa " + ver
    return False, ""


def check_deps():
    """Fast presence + version check of exactly what native/deps.sh installs. Never installs anything."""
    out = {}
    for name, cmd, args in (
        ("yt-dlp", "yt-dlp", ["--version"]),
        ("ffmpeg", "ffmpeg", ["-version"]),
        ("gallery-dl", "gallery-dl", ["--version"]),
        ("exiftool", "exiftool", ["-ver"]),
    ):
        present, ver = _cli_version(cmd, args)
        if present and cmd == "ffmpeg":
            m = re.search(r"version (\S+)", ver)
            if m:
                ver = m.group(1)
        out[name] = {"present": present, "version": ver}
    out["pypdf"] = dict(zip(("present", "version"), _pkg_version("pypdf")))
    out["mutagen"] = dict(zip(("present", "version"), _pkg_version("mutagen")))
    out["analyzer"] = dict(zip(("present", "version"), _analyzer_version()))
    return out


APPLE_PY_HINT = "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"


VENV_UNUSABLE_HINT = ("native/.venv is unusable (its Python is gone or pyvenv.cfg is missing): the host runs on the system "
                      "Python without the libraries. Delete the native/.venv folder, then click Install missing dependencies")
# for the popup and the host's own errors: how to get each tool when the installer cannot (same advice as deps.sh)
TOOL_HOWTO = engine.TOOL_HOWTO


def _is_apple_path(p):
    p = p or ""
    return p == "/usr/bin" or p.startswith(("/Library/Developer/CommandLineTools/", "/Applications/Xcode",
                                             "/usr/bin/", "/System/Library/Frameworks/"))


def python_is_apple():
    """Is the interpreter this one is built on Apple's? Inside a venv sys._base_executable is the venv's own
    python3, so read pyvenv.cfg (`home`, `base-executable`) and sys.base_prefix, which name the real base."""
    cfg = {}
    try:
        with open(os.path.join(sys.prefix, "pyvenv.cfg")) as f:
            for line in f:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return any(_is_apple_path(p) for p in (cfg.get("home"), cfg.get("base-executable"), sys.base_prefix,
                                           getattr(sys, "_base_executable", "")))


def deps_hints(deps):
    """Why a dependency is missing, when we know, and how to get it by hand: the venv runs on Apple's Python
    3.9 (Xcode Command Line Tools) and has no BPM/key analyzer (the text deps.sh prints), the venv itself is
    unusable, or an engine tool the installer could not or would not provide (no prebuilt package here)."""
    hints = {}
    if not deps.get("analyzer", {}).get("present") and python_is_apple():
        hints["analyzer"] = APPLE_PY_HINT
    if _VENV_UNUSABLE:
        hints["venv"] = VENV_UNUSABLE_HINT
    for tool in ("yt-dlp", "ffmpeg", "gallery-dl", "exiftool"):
        if tool in deps and not deps[tool].get("present"):
            hints[tool] = f"not installed: click Install missing dependencies, or by hand: {TOOL_HOWTO[tool]}"
    return hints


def setup_async(msg):
    """native/deps.sh's ensure_deps, sourced the way install.sh sources it. brew installs can take
    minutes: its own thread, reply when done (never blocks the port)."""
    def run():
        deps_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deps.sh")
        log.info("setup: running ensure_deps from %s", deps_sh)
        # the path is an argument, never spliced into the script: `"`, `$`, backticks in it are harmless
        rc, so, se = jlog.run(["/bin/bash", "-c", 'source "$1" && ensure_deps', "_", deps_sh], log, timeout=1800)
        lines = [l for l in (so + se).splitlines() if l.strip()]
        output, ok = "\n".join(lines[-20:]), rc == 0
        # On a first-run install this process was launched (and re-exec-checked) BEFORE ensure_deps
        # ever created native/.venv, so it's still running under the old (system) Python and an
        # in-process check_deps() would see none of what ensure_deps just installed into the venv.
        # Re-check in a fresh venv-interpreter subprocess whenever the venv exists and this process
        # isn't already it; fall back to the in-process check on any error (e.g. venv missing).
        missing, python, hints = None, sys.executable, {}
        if os.path.exists(_VENV_PY) and not _in_venv():
            try:
                _, cpout, _ = jlog.run([_VENV_PY, os.path.abspath(__file__), "--deps"], log, timeout=60)
                info = json.loads(cpout)
                missing = [name for name, v in info["deps"].items() if not v["present"]]
                python = info["python"]
                hints = info.get("hints", {})
            except Exception:
                log.exception("setup: re-check in the venv failed, falling back to the in-process check")
                missing = None
        if missing is None:
            cd = check_deps()
            missing, hints = [name for name, info in cd.items() if not info["present"]], deps_hints(cd)
        log.info("setup done: ok=%s missing=%s python=%s", ok, missing, python)
        send({"id": msg.get("id"), "ok": ok, "output": output, "missing": missing, "python": python, "hints": hints})
    threading.Thread(target=run, daemon=True).start()


def choose_dir():
    """Finder folder picker, in front of Brave. Cancel -> ok:false."""
    script = ('tell application "System Events" to activate\n'
              'tell application "System Events" to return POSIX path of '
              '(choose folder with prompt "Jownloader: save downloads into…")')
    rc, out, err = jlog.run(["/usr/bin/osascript", "-e", script], log)
    path = out.strip()
    if rc != 0 or not path:
        return {"ok": False, "output": "cancelled" if "-128" in err else err.strip()}
    return {"ok": True, "path": path.rstrip("/")}


def choose_files():
    """Finder multi-file picker, in front of Brave. Cancel -> ok:false. One path per line."""
    script = ('tell application "System Events" to activate\n'
              'tell application "System Events"\n'
              'set fs to choose file with prompt "Jownloader: files to rename…" with multiple selections allowed\n'
              'set out to ""\n'
              'repeat with f in fs\n'
              'set out to out & POSIX path of f & linefeed\n'
              'end repeat\n'
              'return out\n'
              'end tell')
    rc, out, err = jlog.run(["/usr/bin/osascript", "-e", script], log)
    paths = [l for l in out.split("\n") if l.strip()]
    if rc != 0 or not paths:
        return {"ok": False, "output": "cancelled" if "-128" in err else err.strip()}
    files = []
    for path in paths:
        try:
            files.append({"path": path, "mtime": int(os.stat(path).st_mtime * 1000)})
        except OSError as e:
            return {"ok": False, "output": str(e)}
    return {"ok": True, "files": files}


def rename(msg):
    """Batch rename / move / copy. Each op fails on its own; the batch keeps going."""
    copy = msg.get("mode") == "copy"
    results = []
    for op in msg.get("ops") or []:
        src = op.get("src") or ""
        name = os.path.basename(op.get("name") or "") or os.path.basename(src)
        dst_dir = op.get("dst_dir") or os.path.dirname(src)
        try:
            if not os.path.isfile(src):
                raise FileNotFoundError(f"not a file: {src}")
            os.makedirs(dst_dir, exist_ok=True)
            if not copy and dst_dir == os.path.dirname(src) and name == os.path.basename(src):
                results.append({"ok": True, "path": src}); continue      # already named that
            dst = unique_path(dst_dir, name)
            log.debug("rename %s %s -> %s", "copy" if copy else "move", src, dst)
            if copy:
                shutil.copy2(src, dst)
            else:
                shutil.move(src, dst)                                   # rename, or cross-volume move
            results.append({"ok": True, "path": dst})
        except Exception as e:
            log.warning("rename %s -> %s/%s failed: %s", src, dst_dir, name, e)
            results.append({"ok": False, "output": str(e)})
    log.info("rename mode=%s: %d op(s), %d ok", "copy" if copy else "move", len(results), sum(1 for r in results if r["ok"]))
    return {"ok": True, "results": results}


NAME_LIMIT = 200      # bytes of UTF-8 in a name we build or accept: leaves room for " (99)" and ".jownloading" under 255
NAME_LOCK = threading.Lock()


def fit_bytes(s, limit):
    """s cut to at most `limit` bytes of UTF-8 at a character boundary; a lone surrogate is dropped."""
    s = s.encode("utf-8", "ignore").decode("utf-8")
    if len(s.encode("utf-8")) <= limit:
        return s
    return s.encode("utf-8")[:limit].decode("utf-8", "ignore")


def clean_name(name, fallback="download"):
    """A page-supplied file name: last path part, no leading dots, <= NAME_LIMIT bytes (extension kept)."""
    name = os.path.basename((name or "").replace("\\", "/")).strip().lstrip(".").strip()
    name = name.encode("utf-8", "ignore").decode("utf-8")
    if len(name.encode("utf-8")) > NAME_LIMIT:
        base, ext = os.path.splitext(name)
        if len(ext.encode("utf-8")) > 16:
            base, ext = name, ""
        name = fit_bytes(base, NAME_LIMIT - len(ext.encode("utf-8"))).rstrip() + ext
    return name or fallback


def _taken(out):
    return os.path.lexists(out) or os.path.lexists(out + ".jownloading")


def unique_path(dst_dir, name, reserve=False):
    """A free name in dst_dir: neither the file nor its .jownloading exists. reserve=True also creates the
    empty name.jownloading atomically (O_EXCL) so a concurrent caller can never pick the same name;
    the caller then owns that temp (write into it, rename or remove it)."""
    base, ext = os.path.splitext(name)
    with NAME_LOCK:
        first = out = os.path.join(dst_dir, name); n = 2
        while True:
            if not _taken(out):
                if not reserve:
                    if out != first:
                        log.info("name taken, using %s instead of %s", out, first)
                    return out
                try:
                    os.close(os.open(out + ".jownloading", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644))
                    if out != first:
                        log.info("name taken, reserved %s instead of %s", out, first)
                    return out
                except FileExistsError:
                    log.info("reserve race on %s: someone else took it just now, trying the next name", out)
            out = os.path.join(dst_dir, f"{base} ({n}){ext}"); n += 1


def final_for(temp, dst, name):
    """The final name for a temp track that already holds its own reservation: the temp's own name minus
    .jownloading when that is `name` and still free, else the next free name."""
    own = os.path.join(dst, name)
    if own + ".jownloading" == temp and not os.path.lexists(own):
        return own
    return unique_path(dst, name)


def commit(temp, path):
    """Move a finished temp onto its reserved final name, never replacing a file that appeared there
    meanwhile (then the next free name is used). Returns the path actually written."""
    while True:
        try:
            os.link(temp, path)
        except FileExistsError:
            new = unique_path(os.path.dirname(path), os.path.basename(path))
            log.warning("commit: %s appeared meanwhile, saving as %s", path, new)
            path = new
            continue
        except OSError as e:                             # filesystem without hard links
            if os.path.lexists(path):
                new = unique_path(os.path.dirname(path), os.path.basename(path))
                log.warning("commit: no hard links (%s) and %s exists, saving as %s", e, path, new)
                path = new; continue
            log.info("commit: no hard links here (%s), moving %s into place with os.replace", e, temp)
            os.replace(temp, path); return path
        os.remove(temp)
        return path


OPEN = {}   # id -> {"f": file, "path": str, "bytes": int, "temp": bool}
TEMP = {}   # id -> path of a closed temp track (a .jownloading file waiting for "join")


def ffmpeg_path():
    """ffmpeg, wherever Homebrew or a manual install put it (the browser's PATH lacks /opt/homebrew/bin)."""
    for c in [shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", os.path.expanduser("~/bin/ffmpeg")]:
        if c and os.access(c, os.X_OK):
            return c
    return None


def join(msg):
    """Join the temp tracks of one stream into dst/name.mp4 (ffmpeg -c copy); temps are removed."""
    sid, ids = msg.get("id"), msg.get("ids") or []
    dst = msg.get("dst") or os.path.expanduser("~/Downloads")
    name = clean_name(msg.get("name"), "video")
    tracks = [TEMP.pop(i) for i in ids if i in TEMP]
    log.info("join id=%s: %d/%d track(s) known %s -> %s/%s", sid, len(tracks), len(ids), [os.path.basename(t) for t in tracks], dst, name)
    if len(tracks) != len(ids) or not tracks:
        log.error("join id=%s: track missing (asked %s, have temp ids %s)", sid, ids, sorted(TEMP))
        for t in tracks:
            try: os.remove(t)
            except OSError: pass
        return {"id": sid, "ok": False, "output": "track missing"}
    raw_ext = lambda t: os.path.splitext(t[:-len(".jownloading")])[1] or ".mp4"
    ff = ffmpeg_path()
    if ff:
        out = unique_path(dst, name + ".mp4", reserve=True)
        cmd = [ff, "-nostdin", "-y", "-loglevel", "error"]
        for t in tracks:
            cmd += ["-i", t]
        for i in range(len(tracks)):
            cmd += ["-map", str(i)]
        cmd += ["-c", "copy", "-movflags", "+faststart", "-f", "mp4", out + ".jownloading"]
        rc, _so, se = jlog.run(cmd, log, timeout=1800)
        err = "" if rc == 0 else (se.strip().splitlines() or ["ffmpeg failed"])[-1]
        if not err:
            for t in tracks:
                try: os.remove(t)
                except OSError: pass
            out = commit(out + ".jownloading", out)
            return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out)}
        try: os.remove(out + ".jownloading")
        except OSError: pass
        note = "ffmpeg could not join the tracks (" + err + ")"
    else:
        note = "ffmpeg not found (" + TOOL_HOWTO["ffmpeg"] + ")"
    log.warning("join id=%s: falling back to the raw tracks: %s", sid, note)
    # no ffmpeg / it failed: keep what was fetched, say so
    if len(tracks) == 1:
        out = commit(tracks[0], final_for(tracks[0], dst, name + raw_ext(tracks[0])))
        return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out), "output": note + " — saved raw as " + os.path.basename(out)}
    outs = []
    for t in tracks:
        base = os.path.basename(t[:-len(".jownloading")])          # name.video.mp4 / name.audio.mp4 (+ (2) suffix)
        out = commit(t, final_for(t, dst, base)); outs.append(out)
    return {"id": sid, "ok": True, "path": outs[0], "bytes": sum(os.path.getsize(o) for o in outs),
            "output": note + " — video and audio saved as separate files: " + ", ".join(os.path.basename(o) for o in outs)}


def safe_name(title, fallback):
    """A page title as a filename: path separators and control characters become '-', no leading dot,
    capped. Colons stay (Portal:Current events)."""
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", title or "")
    t = re.sub(r"[/\\]+", "-", t)
    t = re.sub(r"\s+", " ", t).strip().lstrip(".").strip()
    t = fit_bytes(t, 150).strip()                # bytes, not characters: 150 emoji are 600 bytes and APFS stops at 255
    return t or fallback


def mergepdf(msg):
    """Finish a whole-site PDF crawl from its closed temp-track PDFs, in page order.
    One page -> dst/<title>.pdf. Several pages -> merged into one PDF named after the first page's title
    (else <name>.pdf); with no pypdf (or "split") one PDF per page, named by title, in dst/<name>/
    (never 001.pdf). Nothing is overwritten."""
    sid, ids = msg.get("id"), msg.get("ids") or []
    dst = msg.get("dst") or os.path.expanduser("~/Downloads")
    name = clean_name(msg.get("name"), "site")
    titles = msg.get("titles") or []
    tracks = [TEMP.pop(i) for i in ids if i in TEMP]
    if len(tracks) != len(ids) or not tracks:
        for t in tracks:
            try: os.remove(t)
            except OSError: pass
        return {"id": sid, "ok": False, "output": "track missing"}
    title_of = lambda i: safe_name(titles[i] if i < len(titles) else "", f"{name} page {i + 1}")
    log.info("mergepdf id=%s: %d page(s) split=%s dst=%s", sid, len(tracks), bool(msg.get("split")), dst)
    try:
        os.makedirs(dst, exist_ok=True)
        if len(tracks) == 1:
            out = commit(tracks[0], unique_path(dst, title_of(0) + ".pdf"))
            return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out)}
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            PdfReader = None
        log.info("mergepdf id=%s: pypdf %s", sid, "available" if PdfReader is not None else "NOT installed")
        if msg.get("split") or PdfReader is None:
            folder = os.path.join(dst, name); k = 2
            while os.path.exists(folder):
                folder = os.path.join(dst, f"{name} ({k})"); k += 1
            os.makedirs(folder)
            pages = []
            for i, t in enumerate(tracks):
                pages.append(commit(t, unique_path(folder, title_of(i) + ".pdf")))
            r = {"id": sid, "ok": True, "path": folder, "files": pages}      # "files": strip/webp reach every page
            if PdfReader is None and not msg.get("split"):
                r["output"] = "pypdf not installed — pages kept separately; run native/install.sh"
            return r
        first = safe_name(titles[0] if titles else "", "")
        out = unique_path(dst, (first or name) + ".pdf", reserve=True)
        try:
            writer = PdfWriter()
            for t in tracks:
                for page in PdfReader(t).pages:
                    writer.add_page(page)
            with open(out + ".jownloading", "wb") as f:
                writer.write(f)
            for t in tracks:
                try: os.remove(t)
                except OSError: pass
            out = commit(out + ".jownloading", out)
            return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out)}
        except Exception:
            try: os.remove(out + ".jownloading")
            except OSError: pass
            raise
    except Exception as e:
        log.exception("mergepdf id=%s failed", sid)
        for t in tracks:                                # a failed merge / rename leaves no temp page in dst
            try: os.remove(t)
            except OSError: pass
        return {"id": sid, "ok": False, "output": str(e)}


def open_log():
    """Open the log in TextEdit. JOWN_OPEN replaces /usr/bin/open (tests)."""
    path = jlog.log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").close()
    except OSError as e:
        return {"ok": False, "path": path, "output": f"cannot create the log file: {e}"}
    rc, _, err = jlog.run([os.environ.get("JOWN_OPEN") or "/usr/bin/open", "-a", "TextEdit", path], log, timeout=20)
    return {"ok": rc == 0, "path": path, **({} if rc == 0 else {"output": (err.strip() or "open failed")})}


def stream(msg):
    """Streaming save; the extension fetched the bytes itself so every site's login just works."""
    cmd, sid = msg.get("cmd"), msg.get("id")
    if cmd == "open":
        dst = msg.get("dst") or os.path.expanduser("~/Downloads")
        name = clean_name(msg.get("name"))
        try:
            os.makedirs(dst, exist_ok=True)
            path = unique_path(dst, name, reserve=True)           # the name is ours from here: no other open can pick it
            OPEN[sid] = {"f": open(path + ".jownloading", "wb"), "path": path, "bytes": 0, "chunks": 0, "temp": bool(msg.get("temp")),
                         "strip": bool(msg.get("strip")), "webp": msg.get("webp") or False}
            return {"id": sid, "ok": True, "path": path}
        except Exception as e:
            log.exception("open id=%s name=%r dst=%s failed", sid, name, dst)
            return {"id": sid, "ok": False, "output": str(e)}
    st = OPEN.get(sid)
    if not st:
        if cmd == "abort" and sid in TEMP:              # a closed track of a stream that failed later
            log.info("abort id=%s: removing its closed temp track", sid)
            try: os.remove(TEMP.pop(sid))
            except OSError: pass
            return {"id": sid, "ok": False, "output": "aborted"}
        log.warning("%s id=%s: not open (open ids: %s)", cmd, sid, sorted(OPEN))
        return {"id": sid, "ok": False, "output": "not open"}
    if cmd == "chunk":
        data = base64.b64decode(msg.get("data", ""))
        st["f"].write(data); st["bytes"] += len(data); st["chunks"] += 1
        return {"id": sid, "ok": True}
    st["f"].close(); OPEN.pop(sid, None)
    if cmd == "abort":
        log.info("abort id=%s: %s discarded after %d chunk(s), %d bytes", sid, st["path"], st["chunks"], st["bytes"])
        try: os.remove(st["path"] + ".jownloading")
        except OSError: pass
        return {"id": sid, "ok": False, "output": "aborted"}
    log.info("close id=%s: %s %d chunk(s), %d bytes%s", sid, st["path"], st["chunks"], st["bytes"], " (temp track, waits for join)" if st["temp"] else "")
    if st["temp"]:                                      # a stream track: stays hidden until "join"
        TEMP[sid] = st["path"] + ".jownloading"
        return {"id": sid, "ok": True, "path": st["path"], "bytes": st["bytes"]}
    try:
        st["path"] = commit(st["path"] + ".jownloading", st["path"])
        return finished({"id": sid, "ok": True, "path": st["path"], "bytes": st["bytes"]}, st)
    except Exception as e:
        log.exception("close id=%s: commit of %s failed", sid, st["path"])
        return {"id": sid, "ok": False, "output": str(e)}


def _version_cache_path():
    return os.path.join(jlog.log_dir(), ".versions.json")


def log_startup():
    """One block per host process: who launched it, which python, venv decision, tool versions. Tool versions are
    cached on disk by (path, mtime, size) so a one-shot process does not spawn three subprocesses every time."""
    log.info("host start pid=%d ppid=%d python=%s (%s) prefix=%s base_prefix=%s", os.getpid(), os.getppid(), sys.executable, sys.version.split()[0], sys.prefix, sys.base_prefix)
    log.info("host boot: %s | re-exec mark=%s venv_dir=%s in_venv=%s unusable=%s | HOME=%s PATH=%s", _BOOT_NOTE, os.environ.get(_REEXEC_MARK), _VENV_DIR, _in_venv(), _VENV_UNUSABLE, os.path.expanduser("~"), os.environ.get("PATH"))
    try:
        cache = json.load(open(_version_cache_path()))
    except (OSError, ValueError):
        cache = {}
    changed, seen = False, []
    for name, args in (("yt-dlp", ["--version"]), ("ffmpeg", ["-version"]), ("gallery-dl", ["--version"])):
        path = shutil.which(name) or (ffmpeg_path() if name == "ffmpeg" else None)
        if not path:
            seen.append(f"{name}=MISSING"); continue
        try:
            st = os.stat(os.path.realpath(path)); key = f"{path}|{int(st.st_mtime)}|{st.st_size}"
        except OSError:
            key = path
        if key not in cache:
            rc, out, err = jlog.run([path, *args], log, timeout=10, ok_tail=0)
            cache = {k: v for k, v in cache.items() if not k.startswith(path + "|")}
            cache[key] = ((out or err).strip().splitlines() or ["?"])[0][:80]; changed = True
        seen.append(f"{name}={cache[key]} ({path})")
    if changed:
        try:
            tmp = _version_cache_path() + f".{os.getpid()}"
            json.dump(cache, open(tmp, "w")); os.replace(tmp, _version_cache_path())
        except OSError:
            pass
    log.info("host tools: %s", "; ".join(seen))


def main():
    log_startup()
    try:
        serve()
    except BaseException:
        log.exception("host stopped by an exception")
        raise
    finally:                                   # port closed mid-stream: no half files left behind
        log.info("host exit pid=%d: %d open file(s), %d temp track(s) discarded", os.getpid(), len(OPEN), len(TEMP))
        for st in OPEN.values():
            try:
                st["f"].close(); os.remove(st["path"] + ".jownloading")
            except OSError:
                pass
        for t in TEMP.values():
            try: os.remove(t)
            except OSError: pass
        for jid in jlog.running_jobs():         # the port closed: no engine process outlives the host
            jlog.cancel_job(jid, log)


def serve():
    while True:
        msg = read_msg()
        if msg is None:
            break
        if msg.get("cmd") == "log":
            n = jlog.write_ext(msg.get("entries"))
            if not msg.get("noreply"):
                send({"ok": True, "n": n}, quiet=True)
        elif msg.get("cmd") == "openlog":
            send(open_log())
        elif msg.get("cmd") == "taillog":
            text = jlog.tail_file(msg.get("lines") or 200)
            send({"ok": True, "path": jlog.log_path(), "text": text, "lines": len(text.splitlines())}, quiet=True)
            log.debug("taillog: sent %d line(s) to the popup", len(text.splitlines()))
        elif msg.get("cmd") == "exists":
            res = [bool(p) and os.path.exists(p) for p in msg.get("paths") or []]
            log.debug("exists: %s", [(p, r) for p, r in zip(msg.get("paths") or [], res)][:20])
            send({"ok": True, "exists": res})
        elif msg.get("cmd") in ("open", "chunk", "close", "abort"):
            try:
                r = stream(msg)
            except Exception as e:          # disk full, bad base64 …: fail THIS file, keep serving the others
                log.exception("%s id=%s failed", msg.get("cmd"), msg.get("id"))
                st = OPEN.pop(msg.get("id"), None)
                if st:
                    try:
                        st["f"].close(); os.remove(st["path"] + ".jownloading")
                    except OSError:
                        pass
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            if r is not None:
                send(r, quiet=(msg.get("cmd") == "chunk" and bool(r.get("ok"))))     # chunk acks are counted at close, not logged one by one
        elif msg.get("cmd") == "join":
            try:
                r = finished(join(msg), msg)
            except Exception as e:
                log.exception("join id=%s raised", msg.get("id"))
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            send(r)
        elif msg.get("cmd") == "mergepdf":
            try:
                r = finished(mergepdf(msg), msg)
            except Exception as e:
                log.exception("mergepdf id=%s raised", msg.get("id"))
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            send(r)
        elif msg.get("cmd") == "fetch":
            fetch_async(msg)
        elif msg.get("cmd") == "cancel":
            try:
                r = cancel(msg)
            except Exception as e:
                log.exception("cancel id=%s raised", msg.get("id"))
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            send(r)
        elif msg.get("cmd") == "sync":
            try:
                r = engine.spotify_sync(msg.get("dst") or "", msg.get("folder") or "", msg.get("tracks") or [], msg.get("libs") or [], msg.get("total"))
            except Exception as e:
                log.exception("sync raised")
                r = {"ok": False, "output": str(e)}
            r["id"] = msg.get("id")
            send(r)
        elif msg.get("cmd") == "list":
            list_async(msg)
        elif msg.get("cmd") == "setup":
            setup_async(msg)
        elif msg.get("cmd") == "finish":
            send({"ok": True, **engine.finish(msg.get("paths") or [], strip=bool(msg.get("strip")), webp=msg.get("webp") or False)})
        elif msg.get("cmd") == "choose_dir":
            send(choose_dir())
        elif msg.get("cmd") == "choose_files":
            send(choose_files())
        elif msg.get("cmd") == "rename":
            send(rename(msg))
        elif msg.get("cmd") == "deps":
            cd = check_deps()
            log.info("deps: %s", "; ".join(f"{k}={'v' + v['version'] if v['present'] else 'MISSING'}" for k, v in cd.items()))
            send({"ok": True, "deps": cd, "hints": deps_hints(cd), "python": sys.executable, "prefix": sys.prefix})
        else:
            log.warning("unknown cmd %r", msg.get("cmd"))
            send({"ok": False, "output": "unknown cmd"})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--deps":
        # One-shot CLI mode, used by setup_async to re-check deps in the venv process: the re-exec
        # block above already ran by this point, so this always prints from native/.venv when it
        # exists — even when *this* process itself was launched too early to have re-execed (the
        # port process that received "setup" started before ensure_deps had created the venv).
        cd = check_deps()
        print(json.dumps({"deps": cd, "hints": deps_hints(cd), "python": sys.executable, "prefix": sys.prefix}))
        sys.exit(0)
    main()
