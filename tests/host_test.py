# Native-host rename test: drives native/jownloader_host.py over its stdio protocol with real files in a
# temp folder (no Finder dialog, no browser). Run: python3 tests/host_test.py
import json, os, struct, subprocess, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(os.path.dirname(HERE), "native", "jownloader_host.py")
fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
def call(msg):
    data = json.dumps(msg).encode()
    p = subprocess.run([sys.executable, HOST], input=struct.pack("<I", len(data)) + data, capture_output=True)
    n = struct.unpack("<I", p.stdout[:4])[0]
    return json.loads(p.stdout[4:4 + n])

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
finally:
    shutil.rmtree(d)
print("ALL GREEN" if not fails else f"{fails} FAILED")
