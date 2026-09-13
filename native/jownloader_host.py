#!/usr/bin/env python3
"""Jownloader native-messaging host. The extension talks to it two ways:

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
  {"cmd": "fetch", "id", "url", "mode", "dst", "strip"?, "webp"?} -> {"id", "ok", "path", "files", "output"}
     a site the engine handles (YouTube, Instagram, Twitter/X — engine.py): runs in a thread so the
     port keeps streaming other files meanwhile. mode "video" | "audio".
  "open" / "join" / "fetch" take "strip": true (remove all metadata) and "webp": true (WebP -> JPEG);
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
"""
import base64, json, os, shutil, struct, subprocess, sys, threading
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
    res = engine.finish(paths, strip=bool(msg.get("strip")), webp=bool(msg.get("webp")))["results"]
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
            r = engine.fetch(msg.get("url") or "", msg.get("mode") or "video", msg.get("dst") or "")
        except Exception as e:
            r = {"ok": False, "output": str(e)}
        r["id"] = msg.get("id")
        send(finished(r, msg))
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


def unique_path(dst_dir, name):
    base, ext = os.path.splitext(name)
    out, n = os.path.join(dst_dir, name), 2
    while os.path.exists(out):
        out = os.path.join(dst_dir, f"{base} ({n}){ext}"); n += 1
    return out


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
    name = os.path.basename(msg.get("name") or "video") or "video"
    tracks = [TEMP.pop(i) for i in ids if i in TEMP]
    if len(tracks) != len(ids) or not tracks:
        for t in tracks:
            try: os.remove(t)
            except OSError: pass
        return {"id": sid, "ok": False, "output": "track missing"}
    raw_ext = lambda t: os.path.splitext(t[:-len(".jownloading")])[1] or ".mp4"
    ff = ffmpeg_path()
    if ff:
        out = unique_path(dst, name + ".mp4")
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
            os.replace(out + ".jownloading", out)
            return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out)}
        try: os.remove(out + ".jownloading")
        except OSError: pass
        note = "ffmpeg could not join the tracks (" + err + ")"
    else:
        note = "ffmpeg not found (brew install ffmpeg)"
    # no ffmpeg / it failed: keep what was fetched, say so
    if len(tracks) == 1:
        out = unique_path(dst, name + raw_ext(tracks[0]))
        os.replace(tracks[0], out)
        return {"id": sid, "ok": True, "path": out, "bytes": os.path.getsize(out), "output": note + " — saved raw as " + os.path.basename(out)}
    outs = []
    for t in tracks:
        base = os.path.basename(t[:-len(".jownloading")])          # name.video.mp4 / name.audio.mp4 (+ (2) suffix)
        out = unique_path(dst, base)
        os.replace(t, out); outs.append(out)
    return {"id": sid, "ok": True, "path": outs[0], "bytes": sum(os.path.getsize(o) for o in outs),
            "output": note + " — video and audio saved as separate files: " + ", ".join(os.path.basename(o) for o in outs)}


def stream(msg):
    """Streaming save; the extension fetched the bytes itself so every site's login just works."""
    cmd, sid = msg.get("cmd"), msg.get("id")
    if cmd == "open":
        dst = msg.get("dst") or os.path.expanduser("~/Downloads")
        name = os.path.basename(msg.get("name") or "download") or "download"
        try:
            os.makedirs(dst, exist_ok=True)
            path = unique_path(dst, name)
            OPEN[sid] = {"f": open(path + ".jownloading", "wb"), "path": path, "bytes": 0, "temp": bool(msg.get("temp")),
                         "strip": bool(msg.get("strip")), "webp": bool(msg.get("webp"))}
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
        os.replace(st["path"] + ".jownloading", st["path"])
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
        elif msg.get("cmd") == "fetch":
            fetch_async(msg)
        elif msg.get("cmd") == "finish":
            send({"ok": True, **engine.finish(msg.get("paths") or [], strip=bool(msg.get("strip")), webp=bool(msg.get("webp")))})
        elif msg.get("cmd") == "choose_dir":
            send(choose_dir())
        elif msg.get("cmd") == "choose_files":
            send(choose_files())
        elif msg.get("cmd") == "rename":
            send(rename(msg))
        else:
            send({"ok": False, "output": "unknown cmd"})


if __name__ == "__main__":
    main()
