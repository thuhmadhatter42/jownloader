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
  {"cmd": "fetch", "id", "url", "mode", "dst", "strip"?, "webp"?, "mp3"?} -> {"id", "ok", "path", "files", "output"}
     a site the engine handles (YouTube, Instagram, Twitter/X — engine.py): runs in a thread so the
     port keeps streaming other files meanwhile. mode "video" | "audio". Audio is the upload's AAC saved
     as delivered (.m4a); "mp3": true converts it to MP3 V0 instead (single links and Spotify songs alike).
     A Spotify track also carries "meta" ({title, artists, album, number, total, duration_ms, cover, url,
     folder}) and "analyze": true|false (BPM/key into the name): the host matches it to an upload and saves
     <dst>/<folder>/NN - Artists - Title.m4a (AAC as the upload has it, never re-encoded), tagged.
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
import base64, importlib.metadata, importlib.util, json, os, re, shutil, struct, subprocess, sys, threading

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


if os.path.exists(_VENV_PY) and not _in_venv():
    os.execv(_VENV_PY, [_VENV_PY, *sys.argv])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

SEND_LOCK = threading.Lock()   # fetch replies come from worker threads


def read_msg():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(n).decode())


def send(obj):
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
    def run():
        try:
            r = engine.fetch(msg.get("url") or "", msg.get("mode") or "video", msg.get("dst") or "",
                             msg.get("meta") or None, bool(msg.get("analyze")), bool(msg.get("mp3")))
        except Exception as e:
            r = {"ok": False, "output": str(e)}
        r["id"] = msg.get("id")
        send(r if msg.get("meta") else finished(r, msg))   # a Spotify song keeps its tags: "strip metadata" would erase them
    threading.Thread(target=run, daemon=True).start()


def list_async(msg):
    """A Spotify link's track list: one page fetch, in its own thread so the port keeps streaming."""
    def run():
        try:
            r = engine.spotify_list(msg.get("url") or "")
        except Exception as e:
            r = {"ok": False, "output": str(e)}
        r["id"] = msg.get("id")
        send(r)
    threading.Thread(target=run, daemon=True).start()


def _cli_version(cmd, args):
    """Present + a short version string, without running anything that installs or upgrades."""
    path = shutil.which(cmd)
    if not path:
        return False, ""
    try:
        p = subprocess.run([path, *args], capture_output=True, text=True, timeout=5)
        line = ((p.stdout or p.stderr or "").strip().splitlines() or [""])[0]
        return True, line[:80]
    except Exception:
        return True, ""   # on PATH but wouldn't run — still "present" for the ✓/✗


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


def deps_hints(deps):
    """Why a dependency is missing, when we know. The one known case: the venv runs on Apple's Python 3.9
    (Xcode Command Line Tools) and has no BPM/key analyzer; the same text deps.sh prints."""
    hints = {}
    base = getattr(sys, "_base_executable", sys.executable) or ""
    apple = base.startswith(("/Library/Developer/CommandLineTools/", "/usr/bin/")) or base.startswith("/Applications/Xcode")
    if not deps.get("analyzer", {}).get("present") and apple:
        hints["analyzer"] = APPLE_PY_HINT
    return hints


def setup_async(msg):
    """native/deps.sh's ensure_deps, sourced the way install.sh sources it. brew installs can take
    minutes: its own thread, reply when done (never blocks the port)."""
    def run():
        deps_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deps.sh")
        try:
            p = subprocess.run(["/bin/bash", "-c", f'source "{deps_sh}" && ensure_deps'],
                                capture_output=True, text=True, timeout=1800)
            lines = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()]
            output, ok = "\n".join(lines[-20:]), p.returncode == 0
        except Exception as e:
            output, ok = str(e), False
        # On a first-run install this process was launched (and re-exec-checked) BEFORE ensure_deps
        # ever created native/.venv, so it's still running under the old (system) Python and an
        # in-process check_deps() would see none of what ensure_deps just installed into the venv.
        # Re-check in a fresh venv-interpreter subprocess whenever the venv exists and this process
        # isn't already it; fall back to the in-process check on any error (e.g. venv missing).
        missing, python, hints = None, sys.executable, {}
        if os.path.exists(_VENV_PY) and not _in_venv():
            try:
                cp = subprocess.run([_VENV_PY, os.path.abspath(__file__), "--deps"],
                                     capture_output=True, text=True, timeout=60)
                info = json.loads(cp.stdout)
                missing = [name for name, v in info["deps"].items() if not v["present"]]
                python = info["python"]
                hints = info.get("hints", {})
            except Exception:
                missing = None
        if missing is None:
            cd = check_deps()
            missing, hints = [name for name, info in cd.items() if not info["present"]], deps_hints(cd)
        send({"id": msg.get("id"), "ok": ok, "output": output, "missing": missing, "python": python, "hints": hints})
    threading.Thread(target=run, daemon=True).start()


def choose_dir():
    """Finder folder picker, in front of Brave. Cancel -> ok:false."""
    script = ('tell application "System Events" to activate\n'
              'tell application "System Events" to return POSIX path of '
              '(choose folder with prompt "Jownloader: save downloads into…")')
    p = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    path = p.stdout.strip()
    if p.returncode != 0 or not path:
        return {"ok": False, "output": "cancelled" if "-128" in p.stderr else p.stderr.strip()}
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
    p = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    paths = [l for l in p.stdout.split("\n") if l.strip()]
    if p.returncode != 0 or not paths:
        return {"ok": False, "output": "cancelled" if "-128" in p.stderr else p.stderr.strip()}
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
            if copy:
                shutil.copy2(src, dst)
            else:
                shutil.move(src, dst)                                   # rename, or cross-volume move
            results.append({"ok": True, "path": dst})
        except Exception as e:
            results.append({"ok": False, "output": str(e)})
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
        out, n = os.path.join(dst_dir, name), 2
        while True:
            if not _taken(out):
                if not reserve:
                    return out
                try:
                    os.close(os.open(out + ".jownloading", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644))
                    return out
                except FileExistsError:
                    pass
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
            path = unique_path(os.path.dirname(path), os.path.basename(path))
            continue
        except OSError:                                  # filesystem without hard links
            if os.path.lexists(path):
                path = unique_path(os.path.dirname(path), os.path.basename(path)); continue
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
    if len(tracks) != len(ids) or not tracks:
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
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            err = "" if p.returncode == 0 else (p.stderr.strip().splitlines() or ["ffmpeg failed"])[-1]
        except Exception as e:
            err = str(e)
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
        note = "ffmpeg not found (brew install ffmpeg)"
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
    try:
        os.makedirs(dst, exist_ok=True)
        if len(tracks) == 1:
            out = commit(tracks[0], unique_path(dst, title_of(0) + ".pdf"))
            return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out)}
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            PdfReader = None
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
        for t in tracks:                                # a failed merge / rename leaves no temp page in dst
            try: os.remove(t)
            except OSError: pass
        return {"id": sid, "ok": False, "output": str(e)}


def stream(msg):
    """Streaming save; the extension fetched the bytes itself so every site's login just works."""
    cmd, sid = msg.get("cmd"), msg.get("id")
    if cmd == "open":
        dst = msg.get("dst") or os.path.expanduser("~/Downloads")
        name = clean_name(msg.get("name"))
        try:
            os.makedirs(dst, exist_ok=True)
            path = unique_path(dst, name, reserve=True)           # the name is ours from here: no other open can pick it
            OPEN[sid] = {"f": open(path + ".jownloading", "wb"), "path": path, "bytes": 0, "temp": bool(msg.get("temp")),
                         "strip": bool(msg.get("strip")), "webp": msg.get("webp") or False}
            return {"id": sid, "ok": True, "path": path}
        except Exception as e:
            return {"id": sid, "ok": False, "output": str(e)}
    st = OPEN.get(sid)
    if not st:
        if cmd == "abort" and sid in TEMP:              # a closed track of a stream that failed later
            try: os.remove(TEMP.pop(sid))
            except OSError: pass
            return {"id": sid, "ok": False, "output": "aborted"}
        return {"id": sid, "ok": False, "output": "not open"}
    if cmd == "chunk":
        data = base64.b64decode(msg.get("data", ""))
        st["f"].write(data); st["bytes"] += len(data)
        return {"id": sid, "ok": True}
    st["f"].close(); OPEN.pop(sid, None)
    if cmd == "abort":
        try: os.remove(st["path"] + ".jownloading")
        except OSError: pass
        return {"id": sid, "ok": False, "output": "aborted"}
    if st["temp"]:                                      # a stream track: stays hidden until "join"
        TEMP[sid] = st["path"] + ".jownloading"
        return {"id": sid, "ok": True, "path": st["path"], "bytes": st["bytes"]}
    try:
        st["path"] = commit(st["path"] + ".jownloading", st["path"])
        return finished({"id": sid, "ok": True, "path": st["path"], "bytes": st["bytes"]}, st)
    except Exception as e:
        return {"id": sid, "ok": False, "output": str(e)}


def main():
    try:
        serve()
    finally:                                   # port closed mid-stream: no half files left behind
        for st in OPEN.values():
            try:
                st["f"].close(); os.remove(st["path"] + ".jownloading")
            except OSError:
                pass
        for t in TEMP.values():
            try: os.remove(t)
            except OSError: pass


def serve():
    while True:
        msg = read_msg()
        if msg is None:
            break
        if msg.get("cmd") == "exists":
            send({"ok": True, "exists": [bool(p) and os.path.exists(p) for p in msg.get("paths") or []]})
        elif msg.get("cmd") in ("open", "chunk", "close", "abort"):
            try:
                r = stream(msg)
            except Exception as e:          # disk full, bad base64 …: fail THIS file, keep serving the others
                st = OPEN.pop(msg.get("id"), None)
                if st:
                    try:
                        st["f"].close(); os.remove(st["path"] + ".jownloading")
                    except OSError:
                        pass
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            if r is not None:
                send(r)
        elif msg.get("cmd") == "join":
            try:
                r = finished(join(msg), msg)
            except Exception as e:
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            send(r)
        elif msg.get("cmd") == "mergepdf":
            try:
                r = finished(mergepdf(msg), msg)
            except Exception as e:
                r = {"id": msg.get("id"), "ok": False, "output": str(e)}
            send(r)
        elif msg.get("cmd") == "fetch":
            fetch_async(msg)
        elif msg.get("cmd") == "sync":
            try:
                r = engine.spotify_sync(msg.get("dst") or "", msg.get("folder") or "", msg.get("tracks") or [], msg.get("libs") or [], msg.get("total"))
            except Exception as e:
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
            send({"ok": True, "deps": cd, "hints": deps_hints(cd), "python": sys.executable, "prefix": sys.prefix})
        else:
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
