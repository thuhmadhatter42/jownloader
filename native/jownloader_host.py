#!/usr/bin/env python3
"""Jownloader native-messaging host. The extension talks to it two ways:

Persistent port (one process per download batch) — streaming saves. The extension fetched the bytes
with the browser session; this side only writes them.
  {"cmd": "open",  "id", "name", "dst"}  -> {"id", "ok", "path"}      dst "" = ~/Downloads
  {"cmd": "chunk", "id", "data": base64}  -> {"id", "ok"}            acked: the extension sends the next only after this
  {"cmd": "close", "id"}                 -> {"id", "ok", "path", "bytes"}
  {"cmd": "abort", "id"}                 -> {"id", "ok": false}
Names are never overwritten: name (2).ext, name (3).ext …  A file is written as name.jownloading and
renamed on close, so a half file never looks finished.

One-shot (sendNativeMessage, own process each):
  {"cmd": "choose_dir"}                             -> {"ok", "path"}   Finder folder picker
  {"cmd": "exists", "paths": [...]}                 -> {"ok", "exists": [bool...]}   still on disk?
"""
import base64, json, os, struct, subprocess, sys


def read_msg():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(n).decode())


def send(obj):
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


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


def unique_path(dst_dir, name):
    base, ext = os.path.splitext(name)
    out, n = os.path.join(dst_dir, name), 2
    while os.path.exists(out):
        out = os.path.join(dst_dir, f"{base} ({n}){ext}"); n += 1
    return out


OPEN = {}   # id -> {"f": file, "path": str, "bytes": int}


def stream(msg):
    """Streaming save; the extension fetched the bytes itself so every site's login just works."""
    cmd, sid = msg.get("cmd"), msg.get("id")
    if cmd == "open":
        dst = msg.get("dst") or os.path.expanduser("~/Downloads")
        name = os.path.basename(msg.get("name") or "download") or "download"
        try:
            os.makedirs(dst, exist_ok=True)
            path = unique_path(dst, name)
            OPEN[sid] = {"f": open(path + ".jownloading", "wb"), "path": path, "bytes": 0}
            return {"id": sid, "ok": True, "path": path}
        except Exception as e:
            return {"id": sid, "ok": False, "output": str(e)}
    st = OPEN.get(sid)
    if not st:
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
    try:
        os.replace(st["path"] + ".jownloading", st["path"])
        return {"id": sid, "ok": True, "path": st["path"], "bytes": st["bytes"]}
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
        elif msg.get("cmd") == "choose_dir":
            send(choose_dir())
        else:
            send({"ok": False, "output": "unknown cmd"})


if __name__ == "__main__":
    main()
