#!/usr/bin/env python3
"""Jownloader engine — the site-aware downloads and the finishing steps the host runs.

The extension fetches ordinary files itself. For sites that hide their media behind a player API
(YouTube, Instagram, Twitter/X) it hands the page URL to this module instead, and the host runs it:

  fetch(url, mode, outdir)   mode "video" (default) or "audio" (MP3 + BPM/key in the name)
      youtube.com / youtu.be   -> yt-dlp; video = H.264 + AAC in .mp4 (QuickTime-native — a naive
                                  bestvideo+bestaudio yields VP9/Opus that QuickTime cannot decode);
                                  audio = best audio -> .mp3, analysed (analyze.py) and renamed
                                  "Title (BPM key1 key2 key3).mp3", link logged to "(y-m-d) Youtube DL LINKS.txt"
      instagram.com            -> gallery-dl (gallery-dl.conf): a post/reel = every photo and video in it,
                                  a profile = posts, reels, highlights, avatar, into <username>/ as
                                  <username>-<mon-dd-yy>-<NN>.<ext>; 1080p DASH video via yt-dlp, VP9 -> H.264
      twitter.com / x.com      -> gallery-dl: a tweet = its media, a profile = its media timeline, into <name>/
  finish(paths, strip, webp)   strip = remove all metadata (images: exiftool -all=; audio/video: ffmpeg -map_metadata -1,
                               streams copied); webp = "jpg" | "png": WebP -> JPEG (quality 100) or PNG (sips)

Sign-in: the browser session is read from the cookie stores on this Mac (Brave first, then Chrome,
Firefox, Safari …) — nothing is asked for and nothing leaves the machine.
Every function returns plain dicts and never raises; messages are plain English for the popup.
Usage from a shell (for tests): python3 engine.py <url> [out-dir] [video|audio]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "gallery-dl.conf"
COOKIES_TXT = HERE / "cookies.txt"
LAST_SOURCE = HERE / ".cookie-source"
DOWNLOADS = Path.home() / "Downloads"
# the browser launches the host with a bare PATH; Homebrew and pipx live here
os.environ["PATH"] = ":".join([
    "/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), os.environ.get("PATH", "")])

YOUTUBE_RE = re.compile(r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com/|youtu\.be/)", re.I)
INSTAGRAM_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/", re.I)
TWITTER_RE = re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/", re.I)


def site_of(url):
    """'youtube' | 'instagram' | 'twitter' | None — which engine a page URL gets."""
    if YOUTUBE_RE.match(url or ""):
        return "youtube"
    if INSTAGRAM_RE.match(url or ""):
        return "instagram"
    if TWITTER_RE.match(url or ""):
        return "twitter"
    return None


def _run(cmd, timeout=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _missing(*tools):
    return [t for t in tools if not shutil.which(t)]


def _need(*tools):
    m = _missing(*tools)
    return f"{' and '.join(m)} not installed — run native/install.sh (brew install {' '.join(m)})" if m else None


def safe_name(text):
    for ch in r'\/:*?"<>|[]':
        text = text.replace(ch, "_")
    return text.strip(". ") or "download"


# ══════════════════════════════════════════════════════════════════════════════ YouTube
# Strategies tried silently in order until one works (YouTube's sign-in / bot gate).
YT_STRATEGIES = [
    ["--cookies-from-browser", "brave"],
    ["--cookies-from-browser", "firefox"],
    ["--cookies-from-browser", "safari"],
    ["--cookies-from-browser", "chrome"],
    ["--extractor-args", "youtube:player_client=android"],
    ["--extractor-args", "youtube:player_client=web"],
    [],
]
# QuickTime-compatible: H.264 video + AAC audio in MP4; falls back to any mp4, then anything.
YT_VIDEO_FORMAT = "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/b"


def _yt(*args):
    return _run(["yt-dlp"] + list(args), timeout=3600)


def _yt_info(url, extra):
    code, out, err = _yt("--dump-json", "--no-playlist", *extra, url)
    if code != 0 or not out.strip():
        return None, (out + err).splitlines()
    try:
        return json.loads(out.strip().splitlines()[-1]), []
    except (json.JSONDecodeError, ValueError):
        return None, (out + err).splitlines()


def _yt_download(url, mode, outtmpl, extra):
    """Returns (success, path_or_none, error_lines). Success = yt-dlp exited 0."""
    if mode == "audio":
        args = ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        args = ["-f", YT_VIDEO_FORMAT, "--merge-output-format", "mp4"]
    code, out, err = _yt(*args, "-o", outtmpl, "--no-playlist", "--quiet", "--no-progress",
                         "--print", "after_move:filepath", *extra, url)
    if code != 0:
        return False, None, (out + err).splitlines()
    lines = (out or "").strip().splitlines()
    path = Path(lines[0].strip()) if lines and lines[0].strip() else None
    if not (path and path.exists()):
        # --print sometimes doesn't fire (already-cached, some extractors): newest file with the title stem
        base, stem = Path(outtmpl).parent, Path(outtmpl).name.replace(".%(ext)s", "")
        matches = sorted(base.glob(f"{stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = matches[0] if matches else None
    return True, path, []


def _yt_explain(lines, url=""):
    text = " ".join(lines).lower()
    if "private video" in text:
        return "That video is private and cannot be downloaded."
    if "age" in text and ("restrict" in text or "confirm" in text):
        return "That video is age-restricted. Sign in to YouTube in this browser, then retry."
    if "not available" in text or "unavailable" in text:
        return "That video is unavailable in your region or has been removed."
    if "urlopen error" in text or "network" in text or "connection" in text:
        return "Network error — check your connection and try again."
    if "no video formats" in text or "requested format" in text:
        return "No downloadable format was found for that video."
    if "sign in" in text or "login" in text or "bot" in text:
        return "YouTube is blocking the download. Sign in to YouTube in this browser, then retry."
    if "copyright" in text:
        return "This video is blocked due to a copyright claim."
    if url and not url.startswith("http"):
        return "That doesn't look like a valid URL."
    for line in reversed(lines):
        line = line.strip()
        if line and "github.com" not in line and "yt-dlp -U" not in line:
            return f"Download failed: {line}"
    return "Download failed for an unknown reason."


def _analyze(path):
    """BPM + top-3 keys from analyze.py (4 lines), or None when no analyzer is installed."""
    code, out, _ = _run([sys.executable, str(HERE / "analyze.py"), str(path)], timeout=600)
    lines = [l.strip() for l in (out or "").splitlines()]
    if code != 0 or len(lines) < 4 or not lines[0]:
        return None
    keys = [k.split(" (")[0] for k in lines[1:4]]
    return lines[0], keys


def youtube(url, mode, outdir):
    err = _need("yt-dlp", "ffmpeg")
    if err:
        return {"ok": False, "output": err}
    info, meta_err, working = None, [], None
    for extra in YT_STRATEGIES:
        info, meta_err = _yt_info(url, extra)
        if info:
            working = extra
            break
    if not info:
        return {"ok": False, "output": _yt_explain(meta_err, url)}
    title = safe_name(info.get("title") or ("audio" if mode == "audio" else "video"))
    outtmpl = str(Path(outdir) / f"{title}.%(ext)s")
    last_err, ok, path = [], False, None
    for extra in [working] + [s for s in YT_STRATEGIES if s != working]:
        ok, path, last_err = _yt_download(url, mode, outtmpl, extra)
        if ok:
            break
    if not ok:
        return {"ok": False, "output": _yt_explain(last_err, url)}
    if not path:
        return {"ok": True, "path": str(outdir), "output": f"downloaded into {outdir} (file name not reported)"}
    note = ""
    if mode == "audio":
        res = _analyze(path)
        if res:
            bpm, keys = res
            new = path.with_name(f"{path.stem} ({bpm} BPM {' '.join(keys)}){path.suffix}")
            if not new.exists():
                path.rename(new)
                path = new
        else:
            note = "no BPM/key analyzer installed — run native/install.sh"
        log = Path(outdir) / time.strftime("(%-y-%-m-%-d) Youtube DL LINKS.txt")
        with open(log, "a") as f:
            f.write(f"Input URL: {url}\nDownloaded: {path.name}\n\n")
    return {"ok": True, "path": str(path), "files": [str(path)], "output": note}


# ══════════════════════════════════════════════════════════════════════════════ Instagram / Twitter
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
AUTH_RE = re.compile(
    r"login required|authorization required|authentication|--cookies|"
    r"redirect to login|/accounts/login|\b401\b|\b403\b|challenge_required|"
    r"checkpoint|not logged in|no (?:valid )?cookies", re.I)
RATE_RE = re.compile(r"rate.?limit|too many requests|\b429\b|please wait", re.I)
# Instagram URL path segments that are routes, not usernames.
IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "s", "share", "explore", "accounts",
               "direct", "about", "developer", "legal", "web", "challenge", "emails"}
TW_RESERVED = {"home", "explore", "search", "settings", "i", "messages", "notifications", "compose", "intent", "hashtag", "login"}
# Probed in this order; the first one holding a session wins.
BROWSERS = ["brave", "chrome", "firefox", "safari", "edge", "vivaldi", "opera", "chromium", "zen", "librewolf", "orion"]
SESSION_COOKIE = {"instagram": (".instagram.com", "sessionid"), "twitter": (".x.com", "auth_token")}


def gallery_dl_python():
    """The interpreter gallery-dl runs on — borrowed for its cookie-store reader (the only reliable
    way to see which browser is signed in) and to check whether yt-dlp is importable there."""
    gdl = shutil.which("gallery-dl")
    if not gdl:
        return None
    try:
        shebang = Path(gdl).read_text(errors="ignore").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    if not shebang.startswith("#!"):
        return None
    tokens = shebang[2:].strip().split()      # "#!/path/python -E" and "#!/usr/bin/env python3" both exist
    if not tokens:
        return None
    interp = tokens[0]
    if Path(interp).name == "env" and len(tokens) > 1:
        interp = shutil.which(tokens[1]) or tokens[1]
    return interp if Path(interp).exists() else None


_PROBE = r'''
import sys
from gallery_dl import cookies as C
domain, cookie = sys.argv[1], sys.argv[2]
for name in sys.argv[3:]:
    try:
        if name in C.SUPPORTED_BROWSERS_FIREFOX:
            jar = C.load_cookies_firefox(name, domain=domain)
        elif name in C.SUPPORTED_BROWSERS_WEBKIT:
            jar = C.load_cookies_webkit(name, domain=domain)
        else:
            jar = C.load_cookies_chromium(name, domain=domain)
    except Exception:
        continue
    if any(c.name == cookie for c in jar):
        print(name)
'''
_sessions = {}


def session_browsers(site):
    """Browsers on this Mac holding a session for the site, best first. Local cookie stores only —
    never a request at the site: a wrong guess there looks like scraping."""
    if site in _sessions:
        return _sessions[site]
    found = []
    interp = gallery_dl_python()
    if interp:
        order = list(BROWSERS)
        try:
            last = LAST_SOURCE.read_text().strip()
        except OSError:
            last = ""
        if last in order:
            order.remove(last)
            order.insert(0, last)
        domain, cookie = SESSION_COOKIE[site]
        code, out, _ = _run([interp, "-c", _PROBE, domain, cookie] + order, timeout=90)
        found = out.strip().splitlines() if code == 0 else []
    _sessions[site] = found
    return found


def cookie_strategies(site):
    out = []
    if COOKIES_TXT.exists():
        out.append(("cookies.txt", ["--cookies", str(COOKIES_TXT)]))
    found = session_browsers(site)
    if found:
        out.append((found[0], ["--cookies-from-browser", found[0]]))
    out.append(("no login", []))
    return out


_ytdl = None


def ytdl_available():
    """Instagram's 1080p DASH streams go to yt-dlp as an import inside gallery-dl's interpreter."""
    global _ytdl
    if _ytdl is None:
        interp = gallery_dl_python()
        _ytdl = bool(interp) and _run([interp, "-c", "import yt_dlp"], timeout=30)[0] == 0
    return _ytdl


def resolve_share_link(url):
    """instagram.com/share/... is a redirect stub — follow it to the real post."""
    if "/share/" not in url:
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.geturl() or url
    except Exception:
        return url


def normalize(site, raw):
    """(url, kind, label); kind 'post' | 'profile' | 'other'."""
    url = resolve_share_link(raw.strip()).split("?")[0].split("#")[0]
    m = re.match(r"https?://[^/]+/(.*)$", url)
    parts = [p for p in (m.group(1) if m else "").split("/") if p]
    if not parts:
        return url, "other", url
    head = parts[0].lower()
    if site == "instagram":
        if head in ("p", "reel", "reels", "tv") and len(parts) > 1:
            return url, "post", f"post {parts[1]}"
        if head == "stories" and len(parts) > 1:
            return url, "other", f"stories of @{parts[1]}"
        if head in IG_RESERVED:
            return url, "other", url
        if len(parts) == 1:
            return f"https://www.instagram.com/{parts[0]}/", "profile", f"@{parts[0]}"
        return url, "other", f"@{parts[0]} / {parts[1]}"
    if len(parts) >= 3 and parts[1] == "status":
        return f"https://x.com/{parts[0]}/status/{parts[2]}", "post", f"tweet {parts[2]}"
    if head in TW_RESERVED:
        return url, "other", url
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "media"):
        return f"https://x.com/{parts[0]}/media", "profile", f"@{parts[0]}"
    return url, "other", url


def build_cmd(site, url, cookie_args, outdir):
    cmd = ["gallery-dl", "-c", str(CONFIG), "-o", f"extractor.base-directory={outdir}"]
    if site == "instagram" and not ytdl_available():
        cmd += ["-o", "extractor.instagram.videos=merged"]   # the pre-merged file: H.264 but ~720p
    return cmd + list(cookie_args) + [url]


def attempt(site, url, cookie_args, outdir):
    """Run gallery-dl once. Returns (downloaded, skipped, messages, returncode)."""
    downloaded, skipped, messages = [], [], []
    try:
        proc = subprocess.Popen(build_cmd(site, url, cookie_args, outdir), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as exc:
        return [], [], [str(exc)], 1
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("# "):
            skipped.append(line[2:].strip())
        elif line.startswith("/"):
            downloaded.append(line.strip())
        else:
            messages.append(line)
    proc.wait()
    return downloaded, skipped, messages, proc.returncode


def video_codecs(path):
    code, out, _ = _run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                         "-of", "default=noprint_wrappers=1:nokey=0", str(path)], timeout=60)
    v = a = name = None
    for line in out.splitlines():
        key, _, val = line.partition("=")
        if key == "codec_name":
            name = val
        elif key == "codec_type":
            if val == "video" and v is None:
                v = name
            elif val == "audio" and a is None:
                a = name
    return v, a


def transcode_to_h264(path):
    """Instagram's 1080p renditions are VP9, which QuickTime, Finder preview and every NLE refuse.
    Re-encode the video to H.264 and keep AAC audio as-is, so only one stream takes a generation."""
    vcodec, acodec = video_codecs(path)
    if vcodec is None or vcodec == "h264":
        return False
    tmp = path.with_name(path.stem + ".h264-tmp.mp4")
    audio = ["-c:a", "copy"] if acodec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    code, _, _ = _run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:v", "libx264", "-crf", "20",
                       "-preset", "medium", "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", str(tmp)], timeout=1800)
    if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(path)
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def _social_explain(site, messages, code, kind):
    text = " ".join(messages).lower()
    name = "Instagram" if site == "instagram" else "Twitter"
    if AUTH_RE.search(text) or code == 16:
        if session_browsers(site):
            return (f"{name} bounced this to the login page even though {session_browsers(site)[0]} is signed in "
                    f"— usually throttling. Wait 15–20 minutes and try again; what's already saved is skipped.")
        return f"No signed-in {name} session on this Mac. Sign in to {name} in this browser, then try again."
    if RATE_RE.search(text):
        return f"{name} is rate-limiting you. Wait 20–30 minutes and pick up where you left off."
    if "not found" in text or "404" in text or code == 8:
        return "That post or profile doesn't exist (or was deleted)."
    if "private" in text:
        return "That account is private. You can only download it from an account it has accepted."
    if "no extractor" in text:
        return f"That isn't a {name} link this can download."
    if "urlopen" in text or "connection" in text or "timed out" in text:
        return "Network error — check your connection and try again."
    for line in reversed(messages):
        if "[error]" in line:
            return f"Failed: {line.split('] ', 1)[-1]}"
    return "Nothing came back for that profile." if kind == "profile" else "Download failed for an unknown reason."


def social(site, url, outdir):
    err = _need("gallery-dl", "ffmpeg")
    if err:
        return {"ok": False, "output": err}
    url, kind, label = normalize(site, url)
    messages, code = [], 1
    for source, cookie_args in cookie_strategies(site):
        downloaded, skipped, messages, code = attempt(site, url, cookie_args, outdir)
        if downloaded or skipped:
            try:
                LAST_SOURCE.write_text(source)
            except OSError:
                pass
            converted = sum(transcode_to_h264(Path(p)) for p in downloaded if Path(p).suffix.lower() in (".mp4", ".mkv"))
            dest = Path((downloaded or skipped)[0]).parent
            n, k = len(downloaded), len(skipped)
            warn = [m.split("] ", 1)[-1] for m in messages if "[warning]" in m]
            out = (f"{n} file{'s' if n != 1 else ''} → {dest.name}/" + (f" ({k} already there)" if k else "")
                   + (f", {converted} converted to H.264" if converted else "") + ("; " + warn[0] if warn else ""))
            return {"ok": True, "path": downloaded[0] if n == 1 else str(dest), "files": downloaded, "skipped": k, "output": out}
        if not (AUTH_RE.search(" ".join(messages)) or code == 16):
            break   # not a login problem — another cookie source won't help
    return {"ok": False, "output": _social_explain(site, messages, code, kind)}


# ══════════════════════════════════════════════════════════════════════════════ finishing steps
IMAGE_META = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff", ".gif", ".avif"}
VIDEO_META = {".mp4", ".m4v", ".mov", ".m4a", ".mp3", ".webm"}


def strip_metadata(path):
    """Remove all metadata in place: images with exiftool (EXIF, XMP, IPTC, GPS …), audio/video with
    ffmpeg (tags, chapters, encoder/handler strings; streams copied, not re-encoded). Returns (ok, message)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMAGE_META:
        if not shutil.which("exiftool"):
            return False, "exiftool not installed — run native/install.sh (brew install exiftool)"
        code, out, err = _run(["exiftool", "-all=", "-overwrite_original", "-q", str(p)], timeout=300)
        return (True, "") if code == 0 else (False, (err or out).strip().splitlines()[-1] if (err or out).strip() else "exiftool failed")
    if ext in VIDEO_META:
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg not installed — run native/install.sh (brew install ffmpeg)"
        tmp = p.with_name(p.stem + ".strip-tmp" + p.suffix)
        code, _, err = _run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-map", "0", "-map_metadata", "-1", "-map_chapters", "-1",
                             "-c", "copy", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", *(["-movflags", "+faststart"] if ext in (".mp4", ".m4v", ".mov", ".m4a") else []), str(tmp)], timeout=1800)
        if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(p)
            return True, ""
        if tmp.exists():
            tmp.unlink()
        return False, (err.strip().splitlines() or ["ffmpeg failed"])[-1]
    return True, ""


def webp_to_jpeg(path, fmt="jpg"):
    """WebP -> JPEG (quality 100) or PNG next to it with macOS's sips, original removed.
    Returns (ok, new path or message)."""
    p = Path(path)
    if p.suffix.lower() != ".webp":
        return True, str(p)
    fmt = "png" if str(fmt).lower() == "png" else "jpg"
    out = p.with_suffix("." + fmt)
    n = 2
    while out.exists():
        out = p.with_name(f"{p.stem} ({n}).{fmt}")
        n += 1
    opts = ["-s", "format", "png"] if fmt == "png" else ["-s", "format", "jpeg", "-s", "formatOptions", "100"]
    code, _, err = _run(["/usr/bin/sips", *opts, str(p), "--out", str(out)], timeout=300)
    if code != 0 or not out.exists():
        return False, (err.strip() or "sips failed")
    p.unlink()
    return True, str(out)


def finish(paths, strip=False, webp=False):
    """Apply the finishing switches to saved files; webp = "jpg" | "png" | falsy.
    Returns {results: [{path, ok, output}]}."""
    results = []
    for path in paths or []:
        ok, msg, cur = True, "", str(path)
        if webp:
            ok, r = webp_to_jpeg(cur, webp)
            if ok:
                cur = r
            else:
                msg = r
        if strip and ok:
            ok, m = strip_metadata(cur)
            msg = m if not ok else msg
        results.append({"path": cur, "ok": ok, "output": msg})
    return {"results": results}


# ══════════════════════════════════════════════════════════════════════════════ entry
def fetch(url, mode="video", outdir=""):
    outdir = str(Path(outdir or DOWNLOADS).expanduser())
    try:
        os.makedirs(outdir, exist_ok=True)
        site = site_of(url)
        if site == "youtube":
            return youtube(url, "audio" if mode == "audio" else "video", outdir)
        if site in ("instagram", "twitter"):
            return social(site, url, outdir)
        return {"ok": False, "output": "no engine for this site"}
    except Exception as e:      # never let a site module take the host down
        return {"ok": False, "output": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    r = fetch(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else "video", sys.argv[2] if len(sys.argv) > 2 else "")
    print(json.dumps(r, indent=1))
    sys.exit(0 if r.get("ok") else 1)
