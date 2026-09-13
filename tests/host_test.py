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

    # finishing steps: WebP -> JPEG (sips) and strip metadata (exiftool), on files already on disk
    import importlib.util
    fin = os.path.join(d, "fin"); os.makedirs(fin)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=1", "-frames:v", "1", fin + "/pic.jpg"], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Comment=secret", "-Artist=me", fin + "/pic.jpg"], check=True)
    if importlib.util.find_spec("PIL"):
        from PIL import Image
        Image.new("RGB", (8, 8), (200, 30, 30)).save(fin + "/pic.webp", "WEBP")
        # pic.jpg already exists, so the converted webp must land as pic (2).jpg — never overwrite
        r = call({"cmd": "finish", "paths": [fin + "/pic.webp", fin + "/pic.jpg"], "webp": True, "strip": True})
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
