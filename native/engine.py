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
      open.spotify.com         -> a track / album / playlist / artist link: spotify_list() reads the public embed page
                                  (no sign-in) into a track list; each track is matched to the best upload by length
                                  + title (spotify_match), its audio saved as-is (AAC .m4a, never re-encoded) as
                                  <list name>/NN - Artists - Title.m4a and tagged (title, artist, album, number, cover)
  finish(paths, strip, webp)  strip = remove all metadata (images: exiftool -all=; audio/video: ffmpeg -map_metadata -1,
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
SPOTIFY_RE = re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z]{2,5}(?:-[a-z]{2,4})?/)?(track|album|playlist|artist)/([A-Za-z0-9]+)", re.I)


def site_of(url):
    """'youtube' | 'instagram' | 'twitter' | None — which engine a page URL gets."""
    if YOUTUBE_RE.match(url or ""):
        return "youtube"
    if INSTAGRAM_RE.match(url or ""):
        return "instagram"
    if TWITTER_RE.match(url or ""):
        return "twitter"
    if SPOTIFY_RE.match(url or ""):
        return "spotify"
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


# ══════════════════════════════════════════════════════════════════════════════ Spotify links
# A Spotify link is never downloaded from Spotify. The public embed page lists the tracks (title, artists,
# length); each one is matched to the best upload by length + title, and that upload's audio is saved as-is.
SPOTIFY_EMBED_CAP = 100        # the embed page lists at most this many tracks of a playlist (verified)
SPOTIFY_GAP_S = 20             # the extension waits this long between two tracks of a list (see background.js)
EMBED_DIR_ENV = "JOWNLOADER_EMBED_DIR"   # tests: read <dir>/<kind>.html instead of the network
SPOTIFY_MAX_GAP_S = 15         # an upload this far from the track's length is never the track
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_yt_working = None             # the cookie strategy that last worked, tried first for the next track


def spotify_parse(url):
    """(kind, id) of a Spotify link, or (None, None)."""
    m = SPOTIFY_RE.match((url or "").strip())
    return (m.group(1).lower(), m.group(2)) if m else (None, None)


def _spotify_embed(kind, sid):
    """The entity dict inside the embed page's __NEXT_DATA__ (props.pageProps.state.data.entity)."""
    fixtures = os.environ.get(EMBED_DIR_ENV)
    if fixtures:
        html = (Path(fixtures) / f"{kind}.html").read_text(encoding="utf-8")
    else:
        req = urllib.request.Request(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    m = _NEXT_DATA.search(html)
    if not m:
        raise ValueError("the page had no track data")
    return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]


def _best_image(entity):
    """URL of the largest picture in an embed entity (track / album art, playlist cover)."""
    imgs = list(((entity.get("visualIdentity") or {}).get("image")) or [])
    imgs += list(((entity.get("coverArt") or {}).get("sources")) or [])
    imgs = [i for i in imgs if i.get("url")]
    imgs.sort(key=lambda i: i.get("maxWidth") or i.get("width") or 0, reverse=True)
    return imgs[0]["url"] if imgs else ""


def spotify_list(url):
    """Spotify link -> {ok, name, kind, cover, truncated, tracks:[{title, artists, album, number, duration_ms, cover, url}]}.
    truncated = the embed page may have cut the list short (a playlist at the cap) — the extension then
    collects the rest from the open page. Never raises."""
    kind, sid = spotify_parse(url)
    if not kind:
        return {"ok": False, "output": "That isn't a Spotify track, album, playlist or artist link."}
    try:
        e = _spotify_embed(kind, sid)
    except Exception as exc:
        return {"ok": False, "output": f"Couldn't read that Spotify link ({exc}). Is it public?"}
    name = e.get("name") or e.get("title") or kind
    cover = _best_image(e)
    if kind == "track":
        rows = [{"uri": e.get("uri") or f"spotify:track:{sid}", "title": e.get("name") or e.get("title"),
                 "subtitle": ", ".join(a.get("name", "") for a in e.get("artists") or []), "duration": e.get("duration")}]
    else:
        rows = e.get("trackList") or []
    tracks = []
    for r in rows:
        tid = (r.get("uri") or "").rsplit(":", 1)[-1]
        if not tid or not r.get("title"):
            continue
        tracks.append({"title": r["title"], "artists": r.get("subtitle") or "", "album": name if kind == "album" else "",
                       "number": len(tracks) + 1, "duration_ms": r.get("duration") or 0,
                       "cover": cover, "url": f"https://open.spotify.com/track/{tid}"})
    if not tracks:
        return {"ok": False, "output": "No songs found in that Spotify link."}
    return {"ok": True, "name": name, "kind": kind, "id": sid, "cover": cover, "tracks": tracks,
            "truncated": kind == "playlist" and len(tracks) >= SPOTIFY_EMBED_CAP,
            "note": "an artist link lists the artist's top tracks only" if kind == "artist" else ""}


# ---- matching a track to an upload
_BAD_WORDS = ("live", "remix", "cover", "sped up", "slowed", "karaoke", "instrumental", "nightcore", "reverb",
              "8d", "bass boosted", "mashup", "reaction", "tutorial", "acoustic")


def _norm(text):
    import unicodedata
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "").lower()).strip()


def _core_title(title):
    """Track title without '(feat. x)', '- Remastered 2011', '- Single Version' and the like."""
    t = re.sub(r"\s*[(\[](?:feat|ft|with)\b[^)\]]*[)\]]", "", title or "", flags=re.I)
    t = re.sub(r"\s+-\s+.*$", "", t)
    return _norm(t)


def spotify_score(track, cand, strict=False):
    """Score one search hit for a track (higher is better); None = never acceptable.
    cand = {title, channel, duration (s)}. Length is the main signal (+4 within 3 s, +2 within 8 s, hard
    reject beyond SPOTIFY_MAX_GAP_S); the upload's title must contain the track's title (symbol-only
    titles excepted); 'Artist - Topic' channels and 'official audio' score up; live / remix / cover /
    sped up / slowed / karaoke / instrumental score down unless the track's own title says so.
    A hit with no length is never accepted (live streams, 77-minute sets). A symbol-only title has no words to
    match, so it needs length within 3 s AND the artist's name in the upload's title or channel. strict (the
    shortened last-resort query) also needs length within 3 s: never a blind first hit."""
    ctitle, want = _norm(cand.get("title")), _core_title(track.get("title"))
    channel = _norm(cand.get("channel"))
    symbols_only = not re.search(r"\w", want)
    if not symbols_only and want not in ctitle:
        return None
    score = 0
    want_s = (track.get("duration_ms") or 0) / 1000
    got_s = cand.get("duration")
    main_artist = _norm((track.get("artists") or "").split(",")[0])
    if want_s:
        if not got_s:
            return None
        gap = abs(got_s - want_s)
        if gap > SPOTIFY_MAX_GAP_S or ((strict or symbols_only) and gap > 3):
            return None
        score += 4 if gap <= 3 else 2 if gap <= 8 else 0
    if symbols_only and not (main_artist and (main_artist in channel or main_artist in ctitle)):
        return None
    if main_artist and main_artist in channel:
        score += 1
    if channel.endswith("- topic"):
        score += 3
    if "official audio" in ctitle:
        score += 1
    if "lyric" in ctitle:
        score -= 1
    own = _norm(track.get("title"))
    for w in _BAD_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", ctitle) and not re.search(rf"\b{re.escape(w)}\b", own):
            score -= 5
    return score


def _yt_order():
    return ([_yt_working] if _yt_working is not None else []) + [s for s in YT_STRATEGIES if s != _yt_working]


def _yt_search(query, n=5):
    """Top n hits as [{title, channel, duration, url}] via yt-dlp (cookie strategies, last good one first)."""
    global _yt_working
    err = []
    for extra in _yt_order():
        code, out, e = _yt("--dump-json", "--flat-playlist", "--no-warnings", *extra, f"ytsearch{n}:{query}")
        hits = []
        for line in out.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("id"):
                hits.append({"title": d.get("title") or "", "channel": d.get("channel") or d.get("uploader") or "",
                             "duration": d.get("duration"), "url": d.get("url") or f"https://www.youtube.com/watch?v={d['id']}"})
        if code == 0 and hits:
            _yt_working = extra
            return hits, []
        err = (out + e).splitlines()
    return [], err


def spotify_queries(track):
    """Search phrasings, best first. The last is short, for titles made of symbols that find nothing otherwise."""
    artists, title = track.get("artists") or "", _core_title(track.get("title")) or track.get("title") or ""
    first = artists.split(",")[0].strip()
    qs = [f"{artists} - {title}", f"{first} {title} audio", f"{first} {(title.split() or [''])[0]}"]
    return [q.strip() for i, q in enumerate(qs) if q.strip() and q not in qs[:i]]


GOOD_ENOUGH = 6      # stop searching once a hit scores this well (length within 3 s + a channel / audio bonus)
MIN_SCORE = 3        # below this nothing is saved: a wrong upload is worse than a listed failure


def spotify_match(track, search=None):
    """(url or None, message). search(query) -> (hits, err); default = yt-dlp. Up to three searches."""
    search = search or _yt_search
    best, last_err = None, []
    for i, q in enumerate(spotify_queries(track)):
        hits, last_err = search(q)
        for h in hits:
            s = spotify_score(track, h, strict=(i == 2))
            if s is not None and (best is None or s > best[0]):
                best = (s, h["url"])
        if best and best[0] >= GOOD_ENOUGH:
            break
    if best and best[0] >= MIN_SCORE:
        return best[1], ""
    if not best and last_err:
        return None, _yt_explain(last_err)
    return None, "no matching upload found"


# ---- saving + tagging
def _fetch_bytes(url, limit=15_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(limit)


def _yt_download_native(url, outtmpl, extra):
    """Audio exactly as the upload carries it: AAC in .m4a when there is one, else the best audio stream
    in its own container (Opus -> .opus, a stream copy). Never an --audio-format conversion."""
    code, out, err = _yt("-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "best", "-o", outtmpl,
                         "--no-playlist", "--quiet", "--no-progress", "--print", "after_move:filepath", *extra, url)
    if code != 0:
        return False, None, (out + err).splitlines()
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    path = Path(lines[-1]) if lines else None
    if not (path and path.exists()):
        import glob
        base, stem = Path(outtmpl).parent, Path(outtmpl).name.replace(".%(ext)s", "")
        found = sorted(base.glob(f"{glob.escape(stem)}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = found[0] if found else None
    return True, path, []


def spotify_tag(path, meta, cover=None, year=""):
    """Write title / artist / album / track number / cover into the saved file. m4a -> MP4 atoms, opus ->
    Vorbis comments + picture. Other containers are left as they are. Returns '' or a caveat."""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return "mutagen not installed — run native/install.sh (saved untagged)"
    p = Path(path)
    ext = p.suffix.lower()
    n, total = meta.get("number") or 0, meta.get("total") or 0
    is_png = bool(cover) and cover[:4] == b"\x89PNG"
    try:
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(str(p))
            f["\xa9nam"], f["\xa9ART"] = [meta.get("title") or p.stem], [meta.get("artists") or ""]
            if meta.get("album"):
                f["\xa9alb"] = [meta["album"]]
            if n:
                f["trkn"] = [(n, total or 0)]
            if year:
                f["\xa9day"] = [year]
            if cover:
                f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG)]
            f.save()
        elif ext == ".opus":
            import base64
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            f = OggOpus(str(p))
            f["title"], f["artist"] = [meta.get("title") or p.stem], [meta.get("artists") or ""]
            if meta.get("album"):
                f["album"] = [meta["album"]]
            if n:
                f["tracknumber"] = [f"{n}/{total}" if total else str(n)]
            if year:
                f["date"] = [year]
            if cover:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, "image/png" if is_png else "image/jpeg", cover
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            f.save()
        else:
            return f"{ext or 'this'} files can't be tagged — saved untagged"
    except Exception as exc:
        return f"saved, but tagging failed: {exc}"
    return ""


def _cover_bytes(url):
    """The picture at url, or None: a dead or 404 cover link never fails a song."""
    if not url:
        return None
    try:
        return _fetch_bytes(url)
    except Exception:
        return None


# ---- what is already downloaded: matched on "Artists - Title", not on the file's exact name
_NUM_PREFIX = re.compile(r"^\d{1,4}\s*(?:-\s+|_)\s*")
_BPM_SUFFIX = re.compile(r"\s\(\d+(?:\.\d+)? BPM [^)]*\)$")
AUDIO_EXT = {".m4a", ".mp4", ".mp3", ".opus", ".ogg", ".oga", ".webm", ".flac", ".wav", ".aif", ".aiff", ".aac", ".alac", ".wma"}


def _alnum(text):
    return re.sub(r"[\W_]+", "", _norm(text))


def spotify_keys(track):
    """Every spelling of 'Artists - Title' a saved file may carry: all artists or the first, full or trimmed title."""
    artists, title = track.get("artists") or "", track.get("title") or ""
    first = artists.split(",")[0]
    return {_alnum(a + t) for a in (artists, first) for t in (title, _core_title(title))} - {""}


def _file_key(name):
    """A saved file's name without extension, leading 'NN - ' / 'NN_' and BPM/key suffix, reduced to letters + digits."""
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_EXT else name
    return _alnum(_BPM_SUFFIX.sub("", _NUM_PREFIX.sub("", stem)))


def _scan(folder):
    """[(path, key)] for the audio files in a folder (not recursive); a missing folder = empty."""
    out = []
    try:
        for e in os.scandir(folder):
            if e.is_file() and Path(e.name).suffix.lower() in AUDIO_EXT and not e.name.startswith("."):
                out.append((e.path, _file_key(e.name)))
    except OSError:
        pass
    return sorted(out)


def find_existing(folder, track):
    """Path of a file in folder that is this track (any number, any extension), else None."""
    keys = spotify_keys(track)
    return next((p for p, k in _scan(folder) if k in keys), None)


def library_folder_name(name):
    """A list name as a library tool writes it to disk."""
    return re.sub(r'[/\\?%*:|"<>]', "-", name or "").strip()


def spotify_stem(meta, total=None):
    """NN - Artists - Title (no number for a lone song)."""
    width = max(2, len(str(total or meta.get("total") or 0)))
    base = " - ".join(x for x in (safe_name(meta.get("artists") or ""), safe_name(meta.get("title") or "")) if x and x != "download")
    stem = (f"{int(meta['number']):0{width}d} - " if meta.get("number") and meta.get("folder") else "") + base
    return stem[:180].strip(". ")


def spotify_sync(dst, folder, tracks, libs=(), total=None):
    """Before a list is queued, look at what is already there.
      - files in <dst>/<folder>/ that are songs of this list are renamed in place to the list's current
        numbering (two-phase, so a reorder that swaps numbers never collides; a clash with an unrelated
        file gets ' (2)'; nothing is overwritten; a file whose song left the list is never touched)
      - songs found in <lib>/<folder as a library tool writes it>/ count as downloaded too (never copied)
    Returns {ok, have: [indexes already in the folder], inlib: [indexes found in a library], renamed, notes}."""
    dest = Path(dst or DOWNLOADS).expanduser() / safe_name(folder)
    files, used, have, plan = _scan(dest), set(), [], []
    for i, t in enumerate(tracks):
        keys = spotify_keys(t)
        m = next(((p, k) for p, k in files if p not in used and k in keys), None)
        if not m:
            continue
        used.add(m[0])
        have.append(i)
        cur = Path(m[0])
        bpm = _BPM_SUFFIX.search(cur.stem)
        new = spotify_stem({**t, "folder": folder}, total or len(tracks)) + (bpm.group(0) if bpm else "") + cur.suffix
        if new != cur.name:
            plan.append((cur, new))
    moved = []
    for n, (src, new) in enumerate(plan):       # phase 1: out of the way; phase 2: into place
        tmp = src.with_name(f".jown-sync-{n}{src.suffix}")
        try:
            src.rename(tmp)
            moved.append((tmp, src, new))
        except OSError:
            pass
    renamed = 0
    for tmp, src, new in moved:
        target, k = src.with_name(new), 2
        while target.exists():
            target = src.with_name(f"{Path(new).stem} ({k}){Path(new).suffix}")
            k += 1
        try:
            tmp.rename(target)
            renamed += 1
        except OSError:
            tmp.rename(src)
    inlib, notes = [], []
    for lib in libs or []:
        lp = Path(lib).expanduser()
        if not lp.is_dir():
            notes.append(f"library folder not found (drive not connected?): {lp.name or lib}")
            continue
        ld = lp / library_folder_name(folder)
        if not ld.is_dir():
            continue
        lkeys = {k for _, k in _scan(ld)}
        for i, t in enumerate(tracks):
            if i not in have and i not in inlib and lkeys & spotify_keys(t):
                inlib.append(i)
    return {"ok": True, "have": have, "inlib": sorted(inlib), "renamed": renamed, "notes": notes}


def spotify_track(meta, outdir, analyze=False):
    """Download one Spotify track described by meta ({title, artists, album, number, total, duration_ms,
    cover, url, folder}) into outdir/<folder>/NN - Artists - Title.<ext>, tagged. Never overwrites."""
    err = _need("yt-dlp", "ffmpeg")
    if err:
        return {"ok": False, "output": err}
    folder = Path(outdir) / safe_name(meta["folder"]) if meta.get("folder") else Path(outdir)
    stem = spotify_stem(meta)
    os.makedirs(folder, exist_ok=True)
    there = find_existing(folder, meta)
    if there:
        return {"ok": True, "path": there, "files": [], "skipped": 1, "output": ""}
    yt, msg = spotify_match(meta)
    if not yt:
        return {"ok": False, "output": f"{meta.get('title')}: {msg}"}
    tid = (meta.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    year, cover_url = "", meta.get("cover") or ""
    try:        # this track's own embed: the album art of THIS song (a playlist's cover is not it) + release year
        e = _spotify_embed("track", tid)
        cover_url = _best_image(e) or cover_url
        year = ((e.get("releaseDate") or {}).get("isoString") or "")[:10]
    except Exception:
        pass
    global _yt_working
    ok, path, last_err = False, None, []
    outtmpl = str(folder / f"{stem}.%(ext)s")
    for extra in _yt_order():
        ok, path, last_err = _yt_download_native(yt, outtmpl, extra)
        if ok:
            _yt_working = extra
            break
    if not ok or not path:
        return {"ok": False, "output": f"{meta.get('title')}: {_yt_explain(last_err, yt)}"}
    cover = _cover_bytes(cover_url) or (_cover_bytes(meta.get("cover")) if meta.get("cover") != cover_url else None)
    notes = []
    caveat = spotify_tag(path, meta, cover, year)
    if caveat:
        notes.append(caveat)
    if analyze:        # read-only analysis of the saved file; only its name changes
        res = _analyze(path)
        if res:
            bpm, keys = res
            new = path.with_name(f"{path.stem} ({bpm} BPM {' '.join(keys)}){path.suffix}")
            if not new.exists():
                path.rename(new)
                path = new
        else:
            notes.append("no BPM/key analyzer installed — run native/install.sh")
    return {"ok": True, "path": str(path), "files": [str(path)], "output": "; ".join(notes), "format": path.suffix.lower().lstrip(".")}


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
def fetch(url, mode="video", outdir="", meta=None, analyze=False):
    outdir = str(Path(outdir or DOWNLOADS).expanduser())
    try:
        os.makedirs(outdir, exist_ok=True)
        site = site_of(url)
        if site == "youtube":
            return youtube(url, "audio" if mode == "audio" else "video", outdir)
        if site in ("instagram", "twitter"):
            return social(site, url, outdir)
        if site == "spotify":
            if not meta:
                return {"ok": False, "output": "Spotify links go through the list first (spotify_list)"}
            if os.environ.get("JOWNLOADER_DRYRUN"):      # tests: show what reached the engine, touch nothing
                return {"ok": True, "output": "dry-run " + json.dumps({"analyze": bool(analyze), "folder": meta.get("folder"), "number": meta.get("number")})}
            return spotify_track(meta, outdir, bool(analyze))
        return {"ok": False, "output": "no engine for this site"}
    except Exception as e:      # never let a site module take the host down
        return {"ok": False, "output": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if "--list" in sys.argv:        # engine.py --list <spotify url>  -> the track list as JSON
        print(json.dumps(spotify_list(sys.argv[sys.argv.index("--list") + 1]), indent=1))
        sys.exit(0)
    if "--spotify" in sys.argv:     # engine.py --spotify <url> <out-dir> [--bpm]  -> every track of the link
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        lst = spotify_list(args[0])
        if not lst["ok"]:
            print(lst["output"])
            sys.exit(1)
        res = []
        for t in lst["tracks"][: int(os.environ.get("JOWNLOADER_LIMIT", "9999"))]:
            meta = {**t, "total": len(lst["tracks"]), "folder": "" if lst["kind"] == "track" else lst["name"],
                    "album": t["album"] or lst["name"]}
            res.append(fetch(t["url"], "audio", args[1] if len(args) > 1 else "", meta, "--bpm" in sys.argv))
            if len(lst["tracks"]) > 1:
                time.sleep(SPOTIFY_GAP_S)
        print(json.dumps(res, indent=1))
        sys.exit(0 if all(r.get("ok") for r in res) else 1)
    r = fetch(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else "video", sys.argv[2] if len(sys.argv) > 2 else "")
    print(json.dumps(r, indent=1))
    sys.exit(0 if r.get("ok") else 1)
