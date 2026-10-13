# Host file-safety test: concurrent same-named opens, in-flight reservations, temp cleanup on a failed
# merge/rename, byte-capped titles, split-mode strip, leading dots. Drives native/jownloader_host.py over
# its stdio protocol in a temp folder (no browser). Run: python3 tests/host_files_test.py
import base64, json, os, struct, subprocess, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("JOWN_HOST_UNDER_TEST") or os.path.join(os.path.dirname(HERE), "native", "jownloader_host.py")   # override: show the checks FAIL on the old host
VENV_PY = os.path.join(os.path.dirname(HERE), "native", ".venv", "bin", "python3")
LOGDIR = tempfile.mkdtemp(prefix="jown-fileslog-"); os.environ["JOWN_LOG_DIR"] = LOGDIR   # the developer log goes to a temp folder
ENV = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "JOWN_LOG_DIR": LOGDIR}
BOOT_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
fails = 0
def check(label, got, want):
    global fails
    ok = got == want; fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got}" + ("" if ok else f"   want {want}"))
def session(msgs):
    blob = b"".join(struct.pack("<I", len(json.dumps(m).encode())) + json.dumps(m).encode() for m in msgs)
    p = subprocess.run([BOOT_PY, HOST], input=blob, capture_output=True, env=ENV)
    out, i = [], 0
    while i < len(p.stdout):
        n = struct.unpack("<I", p.stdout[i:i + 4])[0]; out.append(json.loads(p.stdout[i + 4:i + 4 + n])); i += 4 + n
    return out
b64 = lambda b: base64.b64encode(b).decode()
OP = lambda i, name, dst, **kw: {"cmd": "open", "id": i, "name": name, "dst": dst, **kw}
CH = lambda i, data: {"cmd": "chunk", "id": i, "data": b64(data)}
CL = lambda i: {"cmd": "close", "id": i}
d = tempfile.mkdtemp(prefix="jown-files-")
try:
    # ---- 3/4/5: two same-named opens in flight at once
    x = os.path.join(d, "x"); os.makedirs(x)
    r = session([OP("a", "image.jpg", x), OP("b", "image.jpg", x), CH("a", b"A" * 100), CH("b", b"B" * 100), CL("a"), CL("b")])
    pa, pb = r[0].get("path"), r[1].get("path")
    check("concurrent same-named opens get distinct paths", pa != pb, True)
    check("both closes ok", [r[4]["ok"], r[5]["ok"]], [True, True])
    check("each file keeps its own bytes", [open(r[4]["path"], "rb").read(), open(r[5]["path"], "rb").read()], [b"A" * 100, b"B" * 100])
    check("names are image.jpg and image (2).jpg", sorted(os.listdir(x)), ["image (2).jpg", "image.jpg"])

    # a batch of three same-named files, interleaved chunks
    y = os.path.join(d, "y"); os.makedirs(y)
    m = [OP(k, "pic.jpg", y) for k in "123"] + [CH(k, k.encode() * 50) for k in "321"] + [CL(k) for k in "123"]
    r = session(m)
    check("three same-named files all saved", sorted(os.listdir(y)), ["pic (2).jpg", "pic (3).jpg", "pic.jpg"])
    check("...each with its own content", sorted(open(e["path"], "rb").read()[:1] for e in r[-3:]), [b"1", b"2", b"3"])

    # an open plus an old in-progress temp elsewhere counts as taken; an open vs a temp-track of the same name
    z = os.path.join(d, "z"); os.makedirs(z)
    open(os.path.join(z, "doc.pdf.jownloading"), "w").write("someone else's temp")
    r = session([OP("a", "doc.pdf", z), CH("a", b"%PDF"), CL("a")])
    check("an existing .jownloading reserves its name", [os.path.basename(r[0]["path"]), open(os.path.join(z, "doc.pdf.jownloading")).read()], ["doc (2).pdf", "someone else's temp"])

    # a file that shows up at the final path while the download runs is never replaced
    w = os.path.join(d, "w"); os.makedirs(w)
    blob = [OP("a", "late.bin", w), CH("a", b"NEW")]
    blob_bytes = b"".join(struct.pack("<I", len(json.dumps(m).encode())) + json.dumps(m).encode() for m in blob)
    p = subprocess.Popen([BOOT_PY, HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=ENV)
    p.stdin.write(blob_bytes); p.stdin.flush()
    for _ in range(2):
        n = struct.unpack("<I", p.stdout.read(4))[0]; p.stdout.read(n)
    open(os.path.join(w, "late.bin"), "wb").write(b"PRECIOUS")
    cl = json.dumps(CL("a")).encode(); p.stdin.write(struct.pack("<I", len(cl)) + cl); p.stdin.flush()
    n = struct.unpack("<I", p.stdout.read(4))[0]; rep = json.loads(p.stdout.read(n)); p.stdin.close(); p.wait()
    check("a file that appeared at the final name is kept", [open(os.path.join(w, "late.bin"), "rb").read(), rep["ok"], open(rep["path"], "rb").read()], [b"PRECIOUS", True, b"NEW"])

    # ---- 49: a failed merge leaves no .jownloading behind
    if os.path.exists(VENV_PY):
        mp = os.path.join(d, "mp"); os.makedirs(mp)
        def tr(k, data): return [OP(f"t{k}", f"host.{k}.pdf", mp, temp=True), CH(f"t{k}", data), CL(f"t{k}")]
        good = subprocess.run([VENV_PY, "-c", "import sys,io;from pypdf import PdfWriter;w=PdfWriter();w.add_blank_page(100,100);b=io.BytesIO();w.write(b);sys.stdout.buffer.write(b.getvalue())"], capture_output=True).stdout
        r = session(tr(0, good) + tr(1, b"not a pdf at all") + tr(2, good) + [{"cmd": "mergepdf", "id": "m", "ids": ["t0", "t1", "t2"], "name": "host", "dst": mp, "titles": ["A", "B", "C"]}])
        check("corrupt page: merge fails loudly", r[-1]["ok"], False)
        check("...and leaves no temp tracks or partial output", os.listdir(mp), [])
    else:
        print("skip merge cleanup (no native/.venv)")
    # a single-page rename that raises removes its temp (in-process, os.replace forced to fail)
    sp = os.path.join(d, "sp"); os.makedirs(sp)
    code = f"""
import sys, os, json
sys.argv = ['x']; sys.path.insert(0, {os.path.dirname(HOST)!r})
import jownloader_host as h
msgs = json.loads({json.dumps([OP('t0', 'x.0.pdf', sp, temp=True), CH('t0', b'%PDF-1.4'), CL('t0')])!r})
for m in msgs: h.stream(m)
real = os.replace
def boom(a, b): raise OSError(63, 'File name too long')
os.replace = boom; os.link = boom
r = h.mergepdf({{'id': 'm', 'ids': ['t0'], 'name': 'site', 'dst': {sp!r}, 'titles': ['T']}})
os.replace = real
print(json.dumps([r['ok'], sorted(os.listdir({sp!r}))]))
"""
    pr = subprocess.run([VENV_PY if os.path.exists(VENV_PY) else BOOT_PY, "-c", code], capture_output=True, text=True, env=ENV)
    check("single page: a failed rename removes the temp", json.loads(pr.stdout.strip().splitlines()[-1]) if pr.stdout.strip() else pr.stderr[-300:], [False, []])

    # ---- 50/61: long / astral titles are cut by bytes and save; a lone surrogate does not break the save
    for label, title in (("130 emoji", "\U0001F600" * 130), ("150 emoji", "\U0001F600" * 150), ("300 ASCII", "a" * 300), ("150 CJK", "中" * 150)):
        t = os.path.join(d, "t-" + label.replace(" ", "")); os.makedirs(t)
        r = session([OP("t0", "x.0.pdf", t, temp=True), CH("t0", b"%PDF-1.4"), CL("t0"),
                     {"cmd": "mergepdf", "id": "m", "ids": ["t0"], "name": "site", "dst": t, "titles": [title]}])
        names = os.listdir(t)
        check(f"title {label}: saved, one file, name <= 255 bytes", [r[-1]["ok"], len(names), all(len(n.encode()) <= 255 for n in names)], [True, 1, True])
    t = os.path.join(d, "t-surr"); os.makedirs(t)
    blob = b"".join(struct.pack("<I", len(s)) + s for s in [json.dumps(m).encode() for m in [OP("t0", "x.0.pdf", t, temp=True), CH("t0", b"%PDF-1.4"), CL("t0")]])
    mm = ('{"cmd":"mergepdf","id":"m","ids":["t0"],"name":"site","dst":' + json.dumps(t) + ',"titles":["' + "a" * 149 + '\\ud83dtail"]}').encode()
    p = subprocess.run([BOOT_PY, HOST], input=blob + struct.pack("<I", len(mm)) + mm, capture_output=True, env=ENV)
    outs, i = [], 0
    while i < len(p.stdout):
        n = struct.unpack("<I", p.stdout[i:i + 4])[0]; outs.append(json.loads(p.stdout[i + 4:i + 4 + n])); i += 4 + n
    check("lone surrogate in a title: saved", [outs[-1]["ok"], len(os.listdir(t))], [True, 1])
    # a streamed file name is capped too (markdown single page, any long name)
    t = os.path.join(d, "t-open"); os.makedirs(t)
    r = session([OP("a", "\U0001F600" * 150 + ".md", t), CH("a", b"hi"), CL("a")])
    check("open: a 150-emoji name saves, keeps .md, <= 255 bytes", [r[-1]["ok"], [n.endswith(".md") and len(n.encode()) <= 255 for n in os.listdir(t)]], [True, [True]])

    # ---- 53: split-mode PDFs are handed to the strip step
    sd = os.path.join(d, "sd"); os.makedirs(sd)
    def tk(k): return [OP(f"t{k}", f"s.{k}.pdf", sd, temp=True), CH(f"t{k}", b"%PDF-1.4 p"), CL(f"t{k}")]
    r = session(tk(0) + tk(1) + [{"cmd": "mergepdf", "id": "m", "ids": ["t0", "t1"], "name": "s", "dst": sd, "titles": ["One", "Two"], "split": True, "strip": True}])
    check("split + strip: the reply lists every page file that went through strip", sorted(os.path.basename(f) for f in r[-1].get("files") or []), ["One.pdf", "Two.pdf"])

    # ---- 58: leading dots are stripped from names the page supplied
    t = os.path.join(d, "dots"); os.makedirs(t)
    r = session([OP("a", ".zshrc", t), CH("a", b"x"), CL("a"), OP("b", "...hidden.txt", t), CH("b", b"x"), CL("b"), OP("c", "..", t), CH("c", b"x"), CL("c")])
    check("leading dots stripped on open", sorted(os.listdir(t)), ["download", "hidden.txt", "zshrc"])
finally:
    shutil.rmtree(d)
    shutil.rmtree(LOGDIR, ignore_errors=True)
print("ALL GREEN" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
