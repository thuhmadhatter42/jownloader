#!/usr/bin/env python3
"""Jownloader engine — the site-aware downloads and the finishing steps the host runs.

The extension fetches ordinary files itself. For sites that hide their media behind a player API
(YouTube, Instagram, Twitter/X) it hands the page URL to this module instead, and the host runs it:

  fetch(url, mode, outdir)   mode "video" (default) or "audio" (AAC as delivered, MP3 V0 with mp3=True; BPM/key in the name)
      youtube.com / youtu.be   -> yt-dlp; video = H.264 + AAC in .mp4 (QuickTime-native — a naive
                                  bestvideo+bestaudio yields VP9/Opus that QuickTime cannot decode);
                                  audio = the upload's own AAC saved as-is (.m4a, never re-encoded; Opus stays
                                  Opus), or MP3 V0 when mp3=True; analysed (analyze.py) and renamed
                                  "Title (BPM key1 key2 key3).m4a", link logged to "(y-m-d) Youtube DL LINKS.txt"
      instagram.com            -> gallery-dl (gallery-dl.conf): a post/reel = every photo and video in it,
                                  a profile = posts, reels, highlights, avatar, into <username>/ as
                                  <username>-<mon-dd-yy>-<NN>.<ext>; 1080p DASH video via yt-dlp, VP9 -> H.264
      twitter.com / x.com      -> gallery-dl: a tweet = its media, a profile = its media timeline, into <name>/
      open.spotify.com         -> a track / album / playlist / artist link: spotify_list() reads the public embed page
                                  (no sign-in) into a track list; each track is matched to the best upload by length
                                  + title (spotify_match), its audio saved as-is (AAC .m4a, never re-encoded; .mp3 when mp3=True) as
                                  <list name>/NN - Artists - Title.m4a and tagged (title, artist, album, number, cover)
  finish(paths, strip, webp)  strip = remove all metadata (images: exiftool -all=; audio/video: ffmpeg -map_metadata -1,
                               streams copied); webp = "jpg" | "png": WebP -> JPEG (quality 100) or PNG (sips)

Sign-in: the browser session is read from the cookie stores on this Mac (Brave first, then Chrome,
Firefox, Safari …) — nothing is asked for and nothing leaves the machine.
Every function returns plain dicts and never raises; messages are plain English for the popup.
Everything it decides or runs is written to the developer log (jlog.py: ~/Library/Logs/Jownloader/jownloader.log).
Usage from a shell (for tests): python3 engine.py <url> [out-dir] [video|audio] [--mp3]
"""

import contextlib
import fcntl
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jlog
log = jlog.get("engine")       # downloads, strategies, subprocesses, finishing steps
slog = jlog.get("spotify")     # Spotify lists, matching, per-track decisions
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


def _run(cmd, timeout=None, cwd=None):
    """(returncode, stdout, stderr); never raises. Every call is logged by jlog.run: argv, cwd, rc, time, output tail."""
    return jlog.run(cmd, log, timeout=timeout, cwd=cwd)


def _sname(extra):
    """A yt-dlp strategy as it is written in the log."""
    return " ".join(extra) if extra else "default (no extra args)"


def _errline(lines):
    """The line of yt-dlp output that says what went wrong: the last 'ERROR:' line, else the last non-blank one."""
    ls = [l.strip() for l in (lines or []) if l and l.strip()]
    for l in reversed(ls):
        if l.upper().startswith("ERROR"):
            return l[:400]
    return (ls[-1][:400] if ls else "(no output)")


def _missing(*tools):
    return [t for t in tools if not shutil.which(t)]


# How a person gets each tool when the installer cannot (same advice native/deps.sh prints). Homebrew is only
# one way in, and on a macOS without prebuilt packages the installer deliberately refuses to build from source.
TOOL_HOWTO = {
    "yt-dlp": "pipx install yt-dlp (no pipx: python3 -m pip install --user pipx && python3 -m pipx install yt-dlp)",
    "gallery-dl": "pipx install gallery-dl (no pipx: python3 -m pip install --user pipx && python3 -m pipx install gallery-dl)",
    "ffmpeg": "download a static build from https://ffmpeg.org/download.html, put ffmpeg and ffprobe in /usr/local/bin",
    "exiftool": "install the macOS package from https://exiftool.org",
}


def tool_howto(tool):
    return TOOL_HOWTO.get(tool, "see the tool's own website")


def _need(*tools):
    m = _missing(*tools)
    if not m:
        return None
    return (f"{' and '.join(m)} not installed — click Install missing dependencies in the popup or run native/install.sh; by hand: "
            + "; ".join(f"{t}: {tool_howto(t)}" for t in m))


def safe_name(text):
    for ch in r'\/:*?"<>|[]':
        text = text.replace(ch, "_")
    return text.strip(". ") or "download"


def tmpl_escape(text):
    """Text that goes into a yt-dlp output template as itself. yt-dlp reads '%(field)s' and expands '$VAR' / '${VAR}'
    in -o: '%' is doubled, and a '$' gets an empty field behind it ('$%(x|)s' -> '$') so no variable can follow it."""
    return str(text).replace("%", "%%").replace("$", "$%(zz|)s")


def tmpl_unescape(text):
    return str(text).replace("$%(zz|)s", "$").replace("%%", "%")


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


# Audio, two ways. Default: the upload's own audio exactly as delivered (AAC in .m4a when there is one, else
# the best stream in its own container, e.g. Opus) - extracted/remuxed only, never re-encoded. mp3 switch on:
# converted to MP3 V0.
AUDIO_ARGS = ["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "best"]
AUDIO_ARGS_MP3 = ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]


def audio_args(mp3=False):
    return list(AUDIO_ARGS_MP3 if mp3 else AUDIO_ARGS)


def _yt_download(url, mode, outtmpl, extra, mp3=False):
    """Returns (success, path_or_none, error_lines). Success = yt-dlp exited 0."""
    if mode == "audio":
        args = audio_args(mp3)
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
        plain = tmpl_unescape(outtmpl)
        base, stem = Path(plain).parent, Path(plain).name.replace(".%(ext)s", "")
        matches = sorted(base.glob(f"{glob.escape(stem)}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = matches[0] if matches else None
    return True, path, []


def _yt_explain(lines, url=""):
    text = " ".join(lines).lower()
    if "private video" in text:
        return "That video is private and cannot be downloaded."
    if "requested format" in text or "no video formats" in text:     # before "not available": "Requested format is not available" is YouTube refusing, not a removed video
        return "YouTube refused the download (format not available)."
    if "age" in text and ("restrict" in text or "confirm" in text):
        return "That video is age-restricted. Sign in to YouTube in this browser, then retry."
    if "not available" in text or "unavailable" in text:
        return "That video is unavailable in your region or has been removed."
    if "urlopen error" in text or "network" in text or "connection" in text:
        return "Network error — check your connection and try again."
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
        log.warning("analyze: no BPM/key for %s (rc=%s, %d output line(s)): no analyzer installed or it failed", path, code, len(lines))
        return None
    keys = [k.split(" (")[0] for k in lines[1:4]]
    log.info("analyze: %s -> %s BPM, keys %s", Path(path).name, lines[0], keys)
    return lines[0], keys


def _bpm_named(folder, title, mp3=False):
    """An audio file 'title (NNN BPM keys).ext' already in folder (an .mp3 only when the mp3 switch is on), else None."""
    try:
        for e in sorted(os.scandir(folder), key=lambda e: e.name):
            p = Path(e.name)
            if e.is_file() and p.suffix.lower() in AUDIO_EXT and (p.suffix.lower() == ".mp3") == bool(mp3) \
                    and p.stem.startswith(title + " (") and _BPM_SUFFIX.search(p.stem) and _BPM_SUFFIX.sub("", p.stem) == title:
                return Path(e.path)
    except OSError:
        pass
    return None


def youtube(url, mode, outdir, mp3=False):
    log.info("youtube: %s mode=%s mp3=%s outdir=%s", url, mode, mp3, outdir)
    err = _need("yt-dlp", "ffmpeg")
    if err:
        log.error("youtube: %s", err)
        return {"ok": False, "output": err}
    info, meta_err, working = None, [], None
    for i, extra in enumerate(YT_STRATEGIES, 1):
        info, meta_err = _yt_info(url, extra)
        if info:
            working = extra
            log.info("youtube info: strategy %d/%d [%s] OK, title=%r", i, len(YT_STRATEGIES), _sname(extra), info.get("title"))
            break
        log.warning("youtube info: strategy %d/%d [%s] FAILED: %s", i, len(YT_STRATEGIES), _sname(extra), _errline(meta_err))
    if not info:
        log.error("youtube info: all %d strategies failed for %s; last error: %s", len(YT_STRATEGIES), url, _errline(meta_err))
        return {"ok": False, "output": _yt_explain(meta_err, url)}
    title = safe_name(info.get("title") or ("audio" if mode == "audio" else "video"))
    if mode == "audio":      # a re-download of a song whose BPM-named file is already there: that file is the song
        there = _bpm_named(Path(outdir), title, mp3)
        if there:
            log.info("youtube: %s is already there as %s, nothing to download", title, there)
            return {"ok": True, "path": str(there), "files": [], "skipped": 1, "output": ""}
    outtmpl = tmpl_escape(Path(outdir) / title) + ".%(ext)s"
    last_err, ok, path = [], False, None
    order = [working] + [s for s in YT_STRATEGIES if s != working]
    for i, extra in enumerate(order, 1):
        ok, path, last_err = _yt_download(url, mode, outtmpl, extra, mp3)
        if ok:
            log.info("youtube download: strategy %d/%d [%s] OK -> %s", i, len(order), _sname(extra), path)
            break
        log.warning("youtube download: strategy %d/%d [%s] FAILED: %s", i, len(order), _sname(extra), _errline(last_err))
    if not ok:
        log.error("youtube download: all %d strategies failed for %s; last error: %s", len(order), url, _errline(last_err))
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
                log.info("youtube: rename %s -> %s", path.name, new.name)
                path.rename(new)
                path = new
            else:
                log.warning("youtube: %s already exists, file keeps its name without BPM/keys", new.name)
        else:
            note = "no BPM/key analyzer installed — run native/install.sh"
        linkfile = Path(outdir) / time.strftime("(%-y-%-m-%-d) Youtube DL LINKS.txt")
        with open(linkfile, "a") as f:
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
        slog.info("embed %s/%s: reading the fixture %s", kind, sid, Path(fixtures) / f"{kind}.html")
        html = (Path(fixtures) / f"{kind}.html").read_text(encoding="utf-8")
    else:
        slog.info("embed %s/%s: GET https://open.spotify.com/embed/%s/%s", kind, sid, kind, sid)
        req = urllib.request.Request(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": UA})
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
            slog.info("embed %s/%s: HTTP %s, %d chars, %.2fs", kind, sid, getattr(resp, "status", "?"), len(html), time.monotonic() - t0)
    m = _NEXT_DATA.search(html)
    if not m:
        slog.error("embed %s/%s: the page has no __NEXT_DATA__ block (%d chars): %r", kind, sid, len(html), html[:200])
        raise ValueError("the page had no track data")
    return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]


def _best_image(entity):
    """URL of the largest picture in an embed entity (track / album art, playlist cover)."""
    imgs = list(((entity.get("visualIdentity") or {}).get("image")) or [])
    imgs += list(((entity.get("coverArt") or {}).get("sources")) or [])
    imgs = [i for i in imgs if i.get("url")]
    imgs.sort(key=lambda i: i.get("maxWidth") or i.get("width") or 0, reverse=True)
    return imgs[0]["url"] if imgs else ""


def _embed_album(e):
    """The album name an embed entity / row names, else '' (the embed of a plain track usually names none)."""
    a = (e or {}).get("album") or (e or {}).get("albumName")
    if isinstance(a, dict):
        a = a.get("name") or a.get("title")
    return a.strip() if isinstance(a, str) else ""


def spotify_list(url):
    """Spotify link -> {ok, name, kind, cover, truncated, skipped, note, tracks:[{title, artists, album, number, duration_ms, cover, url}]}.
    truncated = the embed page may have cut the list short (a playlist at the cap) — the extension then
    collects the rest from the open page. Rows that are not songs (podcast episodes, local files) are left out
    and named in `skipped`. Never raises."""
    kind, sid = spotify_parse(url)
    slog.info("list: %s -> kind=%s id=%s", url, kind, sid)
    if not kind:
        slog.error("list: not a Spotify track / album / playlist / artist link")
        return {"ok": False, "output": "That isn't a Spotify track, album, playlist or artist link."}
    try:
        e = _spotify_embed(kind, sid)
    except Exception as exc:
        slog.exception("list: could not read the embed page for %s/%s", kind, sid)
        return {"ok": False, "output": f"Couldn't read that Spotify link ({exc}). Is it public?"}
    name = e.get("name") or e.get("title") or kind
    cover = _best_image(e)
    if kind == "track":
        rows = [{"uri": e.get("uri") or f"spotify:track:{sid}", "title": e.get("name") or e.get("title"),
                 "subtitle": ", ".join(a.get("name", "") for a in e.get("artists") or []), "duration": e.get("duration"),
                 "album": _embed_album(e)}]
    else:
        rows = e.get("trackList") or []
    tracks, skipped = [], []
    for r in rows:
        uri = r.get("uri") or ""
        title = r.get("title")
        if not uri.startswith("spotify:track:") or r.get("entityType") not in (None, "track"):
            if title or uri:      # a podcast episode or a local file is not a song YouTube can have
                what = "podcast episode" if ":episode:" in uri or r.get("entityType") == "episode" else "local file" if ":local:" in uri else "not a song"
                skipped.append(f"{title or uri} ({what})")
            continue
        tid = uri.rsplit(":", 1)[-1]
        if not tid or not title:
            continue
        tracks.append({"title": title, "artists": r.get("subtitle") or "",
                       "album": name if kind == "album" else _embed_album(r),
                       "number": len(tracks) + 1, "duration_ms": r.get("duration") or 0,
                       "cover": cover, "url": f"https://open.spotify.com/track/{tid}"})
    for sk in skipped:
        slog.info("list: skipped row: %s", sk)
    if not tracks:
        slog.error("list: no songs in %s/%s (%d row(s), %d skipped)", kind, sid, len(rows), len(skipped))
        return {"ok": False, "output": "No songs found in that Spotify link." + (f" Skipped (not songs): {'; '.join(skipped)}." if skipped else "")}
    slog.info("list: %r (%s): %d song(s) from %d row(s), %d skipped, truncated=%s", name, kind, len(tracks), len(rows), len(skipped), kind == "playlist" and len(rows) >= SPOTIFY_EMBED_CAP)
    notes = []
    if kind == "artist":
        notes.append("an artist link lists the artist's top tracks only")
    if skipped:
        notes.append(f"skipped {len(skipped)} that can't be saved as songs: " + "; ".join(skipped[:5]) + (f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""))
    return {"ok": True, "name": name, "kind": kind, "id": sid, "cover": cover, "tracks": tracks, "skipped": skipped,
            "truncated": kind == "playlist" and len(rows) >= SPOTIFY_EMBED_CAP,
            "note": " · ".join(notes)}


# ---- matching a track to an upload
_BAD_WORDS = ("live", "remix", "cover", "sped up", "slowed", "karaoke", "instrumental", "nightcore", "reverb",
              "8d", "bass boosted", "mashup", "reaction", "tutorial", "acoustic")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "ʼ": "'", "´": "'", "`": "'",
                         "“": '"', "”": '"', "„": '"', "″": '"', "–": "-", "—": "-", "−": "-"})
_LETTERS = {"ø": "o", "ł": "l", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe", "ð": "d", "þ": "th"}


def _fold_accents(text):
    """e-acute -> e, but only on Latin letters: kana / hangul / accents on other scripts keep their marks."""
    out = []
    for ch in text:
        d = unicodedata.normalize("NFD", ch)
        if len(d) > 1 and d[0].isascii() and d[0].isalpha():
            out.append(d[0])
        else:
            out.append(_LETTERS.get(ch, ch))
    return "".join(out)


def _norm(text):
    """Lower case, curly quotes / long dashes straight, accents folded, '&' = 'and', 'Pt.' = 'Part', spaces single."""
    t = _fold_accents(unicodedata.normalize("NFKC", text or "").translate(_QUOTES).lower())
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"\bpt\b\.?", "part", t)
    return re.sub(r"\s+", " ", t).strip()


def _loose(text):
    """_norm without apostrophes: "don't" = "dont" (uploads drop them as often as Spotify keeps them)."""
    return _norm(text).replace("'", "")


def _strip_feat(title):
    return re.sub(r"\s*[(\[](?:feat|ft|with)\b[^)\]]*[)\]]", "", title or "", flags=re.I)


def _core_title(title):
    """Track title without '(feat. x)', '- Remastered 2011', '- Single Version' and the like."""
    return _norm(re.sub(r"\s+-\s+.*$", "", _strip_feat(title)))


def _has_word(needle, hay):
    return bool(needle) and bool(re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay))


def _words(text):
    """Only the words: punctuation and brackets become single spaces ('song (part 1)' = 'song part 1')."""
    return re.sub(r"[^\w']+", " ", text).strip()


def _title_segments(ctitle):
    """The pieces of an upload's title: 'Artist - Title (Official Audio)' -> ['artist', 'title']."""
    segs = re.split(r"\s+[-|:•·]\s+|\s*[|•·]\s*", ctitle)
    return [s for s in (re.sub(r"[(\[][^)\]]*[)\]]", "", s).strip(" '\"") for s in segs) if s]


def _title_ok(want, ctitle):
    """The upload's title carries the track's title: as whole words, and for a title of three letters or fewer
    ('i', 'Her', 'Up') as a whole piece of the upload's title - never inside another song's name."""
    w = _words(want)
    if len(_alnum(want)) <= 3:
        return any(_words(s) == w for s in _title_segments(ctitle))
    return _has_word(w, _words(ctitle))


def _artist_agrees(track, ctitle, channel):
    """Some listed artist is named in the upload's title or channel ('X - Topic' channels included)."""
    names = [_loose(a) for a in (track.get("artists") or "").split(",") if a.strip()]
    if not names:
        return True
    return any(_has_word(n, channel) or _has_word(n, ctitle) for n in names)


def spotify_score_why(track, cand, strict=False, artist_required=True):
    """(score or None, why). The score as spotify_score documents it; why = the reason an upload was rejected, or the
    bonuses and penalties that made up its score (the developer log shows both for every hit)."""
    ctitle, want = _loose(cand.get("title")), _loose(_core_title(track.get("title")))
    channel = _loose(cand.get("channel"))
    symbols_only = not re.search(r"\w", want)
    if symbols_only:
        if re.sub(r"\s+", "", want) not in re.sub(r"\s+", "", ctitle):
            return None, "symbol-only title: the symbols are not in the upload's title"
    elif not _title_ok(want, ctitle):
        return None, (f"title {want!r} is not in the upload's title as whole words" +
                      (" (a title of 3 letters or fewer must be a whole piece of it)" if len(_alnum(want)) <= 3 else ""))
    if artist_required and not _artist_agrees(track, ctitle, channel):
        return None, f"none of the artists ({track.get('artists')!r}) is named in the upload's title or channel {cand.get('channel')!r}"
    score, parts = 0, []
    want_s = (track.get("duration_ms") or 0) / 1000
    got_s = cand.get("duration")
    main_artist = _loose((track.get("artists") or "").split(",")[0])
    if want_s:
        if not got_s:
            return None, "the upload has no length (live stream?)"
        gap = abs(got_s - want_s)
        if gap > SPOTIFY_MAX_GAP_S:
            return None, f"duration off by {gap:.0f}s (track {want_s:.0f}s, upload {got_s:.0f}s; the limit is {SPOTIFY_MAX_GAP_S}s)"
        if (strict or symbols_only) and gap > 3:
            return None, f"duration off by {gap:.1f}s; the {'strict last-resort query' if strict else 'symbol-only title'} needs 3s or less"
        pts = 4 if gap <= 3 else 2 if gap <= 8 else 0
        score += pts
        parts.append(f"duration gap {gap:.1f}s {pts:+d}")
    if main_artist and main_artist in channel:
        score += 1; parts.append("main artist in channel +1")
    if channel.endswith("- topic"):
        score += 3; parts.append("'- Topic' channel +3")
    if "official audio" in ctitle:
        score += 1; parts.append("'official audio' +1")
    if "lyric" in ctitle:
        score -= 1; parts.append("'lyric' -1")
    own = _norm(track.get("title"))
    for w in _BAD_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", ctitle) and not re.search(rf"\b{re.escape(w)}\b", own):
            score -= 5; parts.append(f"penalty: {w!r} in the upload's title -5")
    return score, ", ".join(parts) or "no bonuses"


def spotify_score(track, cand, strict=False, artist_required=True):
    """Score one search hit for a track (higher is better); None = never acceptable.
    cand = {title, channel, duration (s)}. Length is the main signal (+4 within 3 s, +2 within 8 s, hard
    reject beyond SPOTIFY_MAX_GAP_S); the upload's title must contain the track's title as whole words (a title
    of three letters or fewer must BE a piece of it); a listed artist must be named in the upload's title or channel
    (artist_required; spotify_match drops it only when none of the hits carries any channel at all);
    'Artist - Topic' channels and 'official audio' score up; live / remix / cover / sped up / slowed / karaoke /
    instrumental score down unless the track's own title says so. A hit with no length is never accepted (live
    streams, 77-minute sets). A symbol-only title has no words to match, so the symbols themselves must be in the
    upload's title and the length within 3 s. strict (the shortened last-resort query) also needs length within 3 s:
    never a blind first hit."""
    return spotify_score_why(track, cand, strict, artist_required)[0]


# yt-dlp runs are the scarce thing (YouTube's bot gate, the cookie stores): one track may start this many in all,
# searches and downloads together. spotify_track opens the budget; a bare call of the helpers is unlimited.
YT_RUNS_PER_TRACK = 10
_budget = None


def _budget_take():
    if _budget is None:
        return True
    if _budget["left"] <= 0:
        slog.warning("yt-dlp run budget for this track is used up (%d runs): no further attempt", YT_RUNS_PER_TRACK)
        return False
    _budget["left"] -= 1
    slog.debug("yt-dlp run budget: %d of %d left after this run", _budget["left"], YT_RUNS_PER_TRACK)
    return True


@contextlib.contextmanager
def _track_budget():
    global _budget
    mine = _budget is None
    if mine:
        _budget = {"left": YT_RUNS_PER_TRACK}
        slog.debug("yt-dlp run budget opened: %d runs for this track", YT_RUNS_PER_TRACK)
    try:
        yield
    finally:
        if mine:
            _budget = None


def _network_down(lines):
    return _yt_explain(lines).startswith("Network error")


def _yt_order():
    return ([_yt_working] if _yt_working is not None else []) + [s for s in YT_STRATEGIES if s != _yt_working]


def _yt_search(query, n=5):
    """Top n hits as [{title, channel, duration, url}] via yt-dlp (cookie strategies, last good one first).
    A search that ran and found nothing, an offline machine and a spent run budget all end the loop: another
    cookie store would not change the answer."""
    global _yt_working
    err = []
    for extra in _yt_order():
        if not _budget_take():
            err = err or ["yt-dlp run budget for this track used up"]
            break
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
            if _yt_working != extra:
                slog.info("search: strategy [%s] now remembered as the working one (was [%s])", _sname(extra), _sname(_yt_working) if _yt_working is not None else "none")
            _yt_working = extra
            slog.info("search %r: strategy [%s] OK, %d hit(s)", query, _sname(extra), len(hits))
            return hits, []
        if code == 0:
            slog.info("search %r: strategy [%s] ran fine but found nothing; other strategies would not change that", query, _sname(extra))
            return [], []
        err = (out + e).splitlines()
        slog.warning("search %r: strategy [%s] FAILED: %s", query, _sname(extra), _errline(err))
        if _network_down(err):
            slog.error("search %r: the network looks down, not trying other strategies", query)
            break
    return [], err


def _tl(track):
    """A track as it is written in the log: 'Artists - Title'."""
    return f"{(track or {}).get('artists') or '?'} - {(track or {}).get('title') or '?'}"


def spotify_queries(track):
    """Search phrasings, best first. The last is short, for titles made of symbols that find nothing otherwise."""
    artists, title = track.get("artists") or "", _core_title(track.get("title")) or track.get("title") or ""
    first = artists.split(",")[0].strip()
    qs = [f"{artists} - {title}", f"{first} {title} audio", f"{first} {(title.split() or [''])[0]}"]
    return [q.strip() for i, q in enumerate(qs) if q.strip() and q not in qs[:i]]


GOOD_ENOUGH = 6      # stop searching once a hit scores this well (length within 3 s + a channel / audio bonus)
MIN_SCORE = 3        # below this nothing is saved: a wrong upload is worse than a listed failure


def spotify_candidates(track, search=None, limit=3):
    """([urls, best first], message). Every acceptable upload (score >= MIN_SCORE) of up to three searches,
    at most `limit` of them. search(query) -> (hits, err); default = yt-dlp."""
    search = search or _yt_search
    scored, seen_any, last_err = {}, False, []
    tl = _tl(track)
    qs = spotify_queries(track)
    slog.info("candidates for %s (%ss): %d queries %s", tl, round((track.get("duration_ms") or 0) / 1000, 1), len(qs), qs)
    with _track_budget():
        for i, q in enumerate(qs):
            hits, last_err = search(q)
            slog.info("  query %d/%d %r: %d hit(s)%s", i + 1, len(qs), q, len(hits), f", error: {_errline(last_err)}" if last_err and not hits else "")
            need_artist = any(h.get("channel") for h in hits)      # no channel anywhere = nothing to compare the artist with
            for h in hits:
                s, why = spotify_score_why(track, h, strict=(i == 2), artist_required=need_artist)
                desc = f"{h.get('title')!r} by {h.get('channel')!r} {h.get('duration')}s {h.get('url')}"
                if s is None:
                    slog.debug("    REJECT %s: %s", desc, why)
                    continue
                seen_any = True
                if s >= MIN_SCORE and s > scored.get(h["url"], -99):
                    scored[h["url"]] = s
                    slog.debug("    ACCEPT score=%d %s: %s", s, desc, why)
                elif s < MIN_SCORE:
                    slog.debug("    REJECT score=%d < %d %s: %s", s, MIN_SCORE, desc, why)
                else:
                    slog.debug("    keep earlier score for %s (this one %d): %s", h.get("url"), s, why)
            if scored and max(scored.values()) >= GOOD_ENOUGH:
                slog.info("  best score %d >= %d: enough, no more queries", max(scored.values()), GOOD_ENOUGH)
                break
            if last_err and _network_down(last_err):
                slog.error("  the network looks down: no more queries")
                break
    ranked = [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]
    if ranked:
        slog.info("candidates for %s: chosen %s", tl, [(u, scored[u]) for u in ranked])
        return ranked, ""
    if not seen_any and last_err:
        slog.error("candidates for %s: none; every search failed, last error: %s", tl, _errline(last_err))
        return [], _yt_explain(last_err)
    slog.warning("candidates for %s: none acceptable (all hits rejected or none found)", tl)
    return [], "no matching upload found"


def spotify_match(track, search=None):
    """(url or None, message): the best acceptable upload. See spotify_candidates for the whole ranking."""
    urls, msg = spotify_candidates(track, search)
    return (urls[0], "") if urls else (None, msg)


# ---- saving + tagging
def _fetch_bytes(url, limit=15_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(limit)


def _yt_download_native(url, outtmpl, extra, mp3=False):
    """Audio exactly as the upload carries it: AAC in .m4a when there is one, else the best audio stream
    in its own container (Opus -> .opus, a stream copy). Never an --audio-format conversion - unless the
    mp3 switch is on, which converts to MP3 V0. outtmpl is a yt-dlp template: build it with tmpl_escape."""
    code, out, err = _yt(*audio_args(mp3), "-o", outtmpl,
                         "--no-playlist", "--quiet", "--no-progress", "--print", "after_move:filepath", *extra, url)
    if code != 0:
        return False, None, (out + err).splitlines()
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    path = Path(lines[-1]) if lines else None
    if not (path and path.exists()):
        plain = tmpl_unescape(outtmpl)
        base, stem = Path(plain).parent, Path(plain).name.replace(".%(ext)s", "")
        found = sorted(base.glob(f"{glob.escape(stem)}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = found[0] if found else None
    return True, path, []


_ID_ATOM, _ID_KEY = "----:com.jownloader:spotifyid", "jownloader_spotify_id"


def _track_id(meta):
    return ((meta or {}).get("url") or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def spotify_tag(path, meta, cover=None, year=""):
    """Write title / artist / album / track number / cover (and the Spotify track id) into the saved file. m4a -> MP4
    atoms, opus -> Vorbis comments + picture. Other containers are left as they are. Returns '' or a caveat."""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return "mutagen not installed — run native/install.sh (saved untagged)"
    p = Path(path)
    ext = p.suffix.lower()
    n, total = meta.get("number") or 0, meta.get("total") or 0
    tid = _track_id(meta)
    is_png = bool(cover) and cover[:4] == b"\x89PNG"
    try:
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
            f = MP4(str(p))
            f["\xa9nam"], f["\xa9ART"] = [meta.get("title") or p.stem], [meta.get("artists") or ""]
            if meta.get("album"):
                f["\xa9alb"] = [meta["album"]]
            if n:
                f["trkn"] = [(n, total or 0)]
            if year:
                f["\xa9day"] = [year]
            if tid:
                f[_ID_ATOM] = [MP4FreeForm(tid.encode("utf-8"))]
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
            if tid:
                f[_ID_KEY] = [tid]
            if cover:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, "image/png" if is_png else "image/jpeg", cover
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            f.save()
        elif ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TRCK, TDRC, APIC, TXXX
            try:
                f = ID3(str(p))
            except ID3NoHeaderError:
                f = ID3()
            f.delall("TIT2"); f.delall("TPE1")
            f.add(TIT2(encoding=3, text=meta.get("title") or p.stem))
            f.add(TPE1(encoding=3, text=meta.get("artists") or ""))
            if meta.get("album"):
                f.delall("TALB"); f.add(TALB(encoding=3, text=meta["album"]))
            if n:
                f.delall("TRCK"); f.add(TRCK(encoding=3, text=f"{n}/{total}" if total else str(n)))
            if year:
                f.delall("TDRC"); f.add(TDRC(encoding=3, text=year))
            if tid:
                f.delall("TXXX:" + _ID_KEY); f.add(TXXX(encoding=3, desc=_ID_KEY, text=tid))
            if cover:
                f.delall("APIC")
                f.add(APIC(encoding=3, mime="image/png" if is_png else "image/jpeg", type=3, desc="Cover", data=cover))
            f.save(str(p), v2_version=3)
        else:
            return f"{ext or 'this'} files can't be tagged — saved untagged"
    except Exception as exc:
        slog.exception("tagging %s failed", p.name)
        return f"saved, but tagging failed: {exc}"
    return ""


def _read_track_id(path):
    """The Spotify track id spotify_tag stored in a saved file, '' if none (older files, or no mutagen)."""
    try:
        import mutagen  # noqa: F401
        ext = Path(path).suffix.lower()
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            v = (MP4(str(path)).tags or {}).get(_ID_ATOM)
            return bytes(v[0]).decode("utf-8", "replace") if v else ""
        if ext == ".opus":
            from mutagen.oggopus import OggOpus
            v = OggOpus(str(path)).get(_ID_KEY)
            return v[0] if v else ""
        if ext == ".mp3":
            from mutagen.id3 import ID3
            fr = ID3(str(path)).getall("TXXX:" + _ID_KEY)
            return str(fr[0].text[0]) if fr else ""
    except Exception:
        pass
    return ""


def _is_image(b):
    return bool(b) and (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n")


def _cover_bytes(url):
    """The picture at url, or None: a dead link, a 404 or a page that is not a JPEG / PNG (a captcha, an error
    page served with status 200) never fails a song and is never embedded."""
    if not url:
        return None
    try:
        b = _fetch_bytes(url)
    except Exception as e:
        slog.warning("cover: %s could not be fetched: %s", url, e)
        return None
    if not _is_image(b):
        slog.warning("cover: %s is not a JPEG / PNG (%d bytes, starts %r): not embedded", url, len(b), b[:16])
        return None
    slog.debug("cover: %s -> %d bytes", url, len(b))
    return b


# ---- what is already downloaded: matched on "Artists - Title", not on the file's exact name
_NUM_PREFIX = re.compile(r"^\d{1,4}\s*(?:-\s+|_)\s*")
_BPM_SUFFIX = re.compile(r"\s\(\d+(?:\.\d+)? BPM [^)]*\)$")
AUDIO_EXT = {".m4a", ".mp4", ".mp3", ".opus", ".ogg", ".oga", ".webm", ".flac", ".wav", ".aif", ".aiff", ".aac", ".alac", ".wma"}
NAME_CLIP = 170          # the 'Artists - Title' part of a saved name is cut here (the number, BPM suffix and extension come on top)
LONG_KEY = 120           # a saved name this long (letters + digits) may be a clipped one: a prefix of the track's key then counts


def _alnum(text):
    return re.sub(r"[\W_]+", "", _norm(text))


def _rawkey(text):
    """Letters, digits AND symbols, no spaces / dashes / underscores: the key of a title made only of symbols."""
    return re.sub(r"[\s_\-]+", "", _norm(text))


def _symbolic(track):
    return not re.search(r"\w", _norm(track.get("title")))


def _clip(text, n=NAME_CLIP):
    text = text[:n]
    while len(text.encode("utf-8")) > 200:
        text = text[:-1]
    return text.strip(". ")


def _base_name(meta):
    """'Artists - Title' as it is written into a file name (no number)."""
    return _clip(" - ".join(x for x in (safe_name(meta.get("artists") or ""), safe_name(meta.get("title") or "")) if x and x != "download"))


def spotify_key_levels(track):
    """The spellings of 'Artists - Title' a saved file may carry, best first, as sets of letters+digits:
    [all artists + the exact title, first artist or no '(feat. x)']. Never a title cut at ' - ': a remix, live or
    remastered version is its own song. A symbol-only title has none (see _symbolic)."""
    if _symbolic(track):
        return [set(), set()]
    artists, title = track.get("artists") or "", track.get("title") or ""
    first = artists.split(",")[0]
    l0 = {_alnum(artists + title), _alnum(_base_name(track))}
    l1 = {_alnum(a + t) for a in (artists, first) for t in (title, _strip_feat(title))} - l0
    return [l0 - {""}, l1 - {""}]


def spotify_keys(track):
    return set().union(*spotify_key_levels(track))


def _key_hit(filekey, keys):
    return filekey in keys or (len(filekey) >= LONG_KEY and any(k.startswith(filekey) for k in keys))


def _file_stem(name):
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_EXT else name
    return _BPM_SUFFIX.sub("", _NUM_PREFIX.sub("", stem))


def _file_key(name):
    """A saved file's name without extension, leading 'NN - ' / 'NN_' and BPM/key suffix, reduced to letters + digits."""
    return _alnum(_file_stem(name))


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


def match_files(files, tracks):
    """{track index: path}: which saved file is which track. files = [(path, key)] from _scan. One file serves one
    track. Exact 'artists - title' first for every track, only then the looser spellings, so a track never takes a file
    another track names exactly. Symbol-only titles are matched by the Spotify track id inside the file, else by the
    symbols themselves in the name (never by the artist's name alone)."""
    got, used = {}, set()
    sym = [i for i, t in enumerate(tracks) if _symbolic(t)]
    if sym:
        ids = {p: _read_track_id(p) for p, _ in files}
        for i in sym:
            tid = _track_id(tracks[i])
            hit = next((p for p, _ in files if p not in used and tid and ids.get(p) == tid), None)
            if not hit:
                want = _rawkey(_base_name(tracks[i]))
                hit = next((p for p, _ in files if p not in used and not ids.get(p) and _rawkey(_file_stem(os.path.basename(p))) == want), None)
            if hit:
                got[i] = hit
                used.add(hit)
    levels = [spotify_key_levels(t) for t in tracks]
    for lv in (0, 1):
        for i in range(len(tracks)):
            if i in got:
                continue
            keys = levels[i][lv]
            hit = next((p for p, k in files if p not in used and k and _key_hit(k, keys)), None)
            if hit:
                got[i] = hit
                used.add(hit)
    return got


def find_existing(folder, track):
    """Path of a file in folder that is this track (any number, any extension), else None."""
    return match_files(_scan(folder), [track]).get(0)


def library_folder_name(name):
    """A list name as a library tool writes it to disk."""
    return re.sub(r'[/\\?%*:|"<>]', "-", name or "").strip()


def spotify_stem(meta, total=None):
    """NN - Artists - Title (no number for a lone song). The 'Artists - Title' part is cut at NAME_CLIP characters."""
    width = max(2, len(str(total or meta.get("total") or 0)))
    return (f"{int(meta['number']):0{width}d} - " if meta.get("number") and meta.get("folder") else "") + _base_name(meta)


@contextlib.contextmanager
def _folder_lock(folder):
    """One list sync at a time per folder (the popup can be open twice, every sendNativeMessage is its own process)."""
    fd = None
    try:
        fd = os.open(str(folder), os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _move_noclobber(src, dst):
    """Rename src to dst without ever replacing something already at dst. True when it moved."""
    try:
        os.link(src, dst)       # atomic, and refuses an existing dst
    except FileExistsError:
        return False
    except OSError as e:        # a volume without hard links: check, then rename
        if os.path.lexists(dst):
            return False
        log.info("move: no hard links here (%s), renaming %s -> %s instead", e, src, dst)
        try:
            os.rename(src, dst)
            return True
        except OSError as e2:
            log.warning("move: rename %s -> %s failed: %s", src, dst, e2)
            return False
    try:
        os.unlink(src)
    except OSError:
        pass
    return True


def _place(src, directory, name, first_try_plain=True):
    """Move src into directory as name, or 'name (2)', 'name (3)' ... when taken. The final Path, or None."""
    stem, suf = Path(name).stem, Path(name).suffix
    for k in range(1, 1000):
        cand = Path(directory) / (name if k == 1 else f"{stem} ({k}){suf}")
        if _move_noclobber(src, cand):
            if k > 1:
                log.info("name taken, placed as %s instead of %s", cand.name, name)
            return cand
    log.error("could not place %s in %s as %r (1000 names taken?)", src, directory, name)
    return None


_SYNC_TMP = re.compile(r"^\.jown-sync-[0-9a-f]+-\d+~(.+)$")


def _recover_sync_leftovers(dest):
    """A sync that died between its two renames leaves hidden .jown-sync-* files. Called under the folder lock (so
    no live sync owns them): put each back under its old name, or 'Recovered song ...' when the name is unknown."""
    try:
        names = [e.name for e in os.scandir(dest) if e.is_file() and e.name.startswith(".jown-sync-")]
    except OSError:
        return
    for nm in sorted(names):
        m = _SYNC_TMP.match(nm)
        back = m.group(1) if m else "Recovered song " + secrets.token_hex(3) + Path(nm).suffix
        slog.warning("sync: leftover %s from a sync that died; putting it back as %s", nm, back)
        _place(Path(dest) / nm, dest, back)


def spotify_sync(dst, folder, tracks, libs=(), total=None):
    """Before a list is queued, look at what is already there.
      - files in <dst>/<folder>/ that are songs of this list are renamed in place to the list's current
        numbering (two-phase through unique hidden names, so a reorder that swaps numbers never collides; a clash
        with an unrelated file gets ' (2)'; nothing is ever deleted or overwritten; a file whose song left the
        list is never touched; matching is exact 'artists - title', see match_files)
      - songs found in <lib>/<folder as a library tool writes it>/ count as downloaded too (never copied)
    Returns {ok, have: [indexes already in the folder], inlib: [indexes found in a library], renamed, notes}."""
    dest = Path(dst or DOWNLOADS).expanduser() / safe_name(folder)
    have, plan = [], []
    slog.info("sync %r: %d song(s), destination %s, %d library folder(s)", folder, len(tracks), dest, len(libs or []))
    with _folder_lock(dest):
        _recover_sync_leftovers(dest)
        files = _scan(dest)
        got = match_files(files, tracks)
        slog.info("sync %r: %d audio file(s) in the folder, %d matched to the list", folder, len(files), len(got))
        for i in sorted(got):
            have.append(i)
            cur = Path(got[i])
            bpm = _BPM_SUFFIX.search(cur.stem)
            new = spotify_stem({**tracks[i], "folder": folder}, total or len(tracks)) + (bpm.group(0) if bpm else "") + cur.suffix
            slog.debug("  have #%s %s <- %s", tracks[i].get("number"), _tl(tracks[i]), cur.name)
            if new != cur.name:
                slog.info("  rename %s -> %s", cur.name, new)
                plan.append((cur, new))
        token, moved = secrets.token_hex(4), []
        for n, (src, new) in enumerate(plan):       # phase 1: out of the way (a name nobody else uses); phase 2: into place
            for tmp in (src.with_name(f".jown-sync-{token}-{n}~{src.name}"), src.with_name(f".jown-sync-{token}-{n}~{src.suffix}")):
                try:
                    src.rename(tmp)
                    moved.append((tmp, src, new))
                    break
                except OSError as e:
                    slog.warning("  rename step 1 %s -> %s failed: %s", src.name, tmp.name, e)
                    continue
        renamed = 0
        for tmp, src, new in moved:
            if _place(tmp, src.parent, new):
                renamed += 1
            elif not _place(tmp, src.parent, src.name):     # could not even go back: it stays hidden, recovered next time
                slog.error("  %s could not be renamed to %s nor put back; it stays hidden as %s until the next sync", src.name, new, tmp.name)
    inlib, notes = [], []
    for lib in libs or []:
        lp = Path(lib).expanduser()
        if not lp.is_dir():
            notes.append(f"library folder not found (drive not connected?): {lp.name or lib}")
            continue
        ld = lp / library_folder_name(folder)
        if not ld.is_dir():
            continue
        rest = [i for i in range(len(tracks)) if i not in have and i not in inlib]
        found = match_files(_scan(ld), [tracks[i] for i in rest])
        for j, fp in found.items():
            slog.info("  in library %s: #%s %s <- %s", lp.name or lib, tracks[rest[j]].get("number"), _tl(tracks[rest[j]]), Path(fp).name)
        inlib += [rest[j] for j in found]
    for n in notes:
        slog.warning("sync note: %s", n)
    slog.info("sync %r done: %d have, %d in a library, %d renamed, %d to download", folder, len(have), len(inlib), renamed, len(tracks) - len(have) - len(inlib))
    return {"ok": True, "have": have, "inlib": sorted(inlib), "renamed": renamed, "notes": notes}


def _download_candidates(urls, outtmpl, mp3):
    """Try each acceptable upload in turn (best first), each with the cookie strategies, inside the per-track run
    budget. A failure that belongs to the upload (removed, private, region-blocked) moves to the next upload; one
    that belongs to the connection ends the attempt. Returns (ok, path, error lines)."""
    global _yt_working
    last_err = []
    for ui, yt in enumerate(urls, 1):
        slog.info("download: upload %d/%d %s", ui, len(urls), yt)
        for extra in _yt_order():
            if not _budget_take():
                return False, None, last_err or ["yt-dlp run budget for this track used up"]
            ok, path, last_err = _yt_download_native(yt, outtmpl, extra, mp3)
            if ok and path:
                if _yt_working != extra:
                    slog.info("download: strategy [%s] now remembered as the working one (was [%s])", _sname(extra), _sname(_yt_working) if _yt_working is not None else "none")
                _yt_working = extra
                slog.info("download: upload %d/%d strategy [%s] OK -> %s", ui, len(urls), _sname(extra), path)
                return True, path, []
            if ok:
                slog.error("download: strategy [%s] exited 0 but no file was found for %s", _sname(extra), yt)
                return False, None, ["the download finished but no file was found"]
            text = " ".join(last_err).lower()
            slog.warning("download: upload %d/%d strategy [%s] FAILED: %s", ui, len(urls), _sname(extra), _errline(last_err))
            if _network_down(last_err):
                slog.error("download: the network looks down, giving up on this song")
                return False, None, last_err
            if "requested format" not in text and re.search(r"private video|not available|unavailable|removed|blocked|copyright|terminated|does not exist", text):
                slog.warning("download: the failure belongs to the upload itself, moving to the next upload")
                break           # this upload is out; the next candidate gets its turn
    slog.error("download: no upload could be downloaded; last error: %s", _errline(last_err))
    return False, None, last_err


def spotify_track(meta, outdir, analyze=False, mp3=False):
    """Download one Spotify track described by meta ({title, artists, album, number, total, duration_ms,
    cover, url, folder}) into outdir/<folder>/NN - Artists - Title.<ext>, tagged. Never overwrites."""
    tl = _tl(meta)
    t0 = time.monotonic()
    slog.info("track #%s %s: start (%ss, folder=%r, outdir=%s, analyze=%s, mp3=%s, %s)", meta.get("number"), tl,
              round((meta.get("duration_ms") or 0) / 1000, 1), meta.get("folder"), outdir, analyze, mp3, meta.get("url"))
    err = _need("yt-dlp", "ffmpeg")
    if err:
        slog.error("track %s: FAILED: %s", tl, err)
        return {"ok": False, "output": err}
    folder = Path(outdir) / safe_name(meta["folder"]) if meta.get("folder") else Path(outdir)
    stem = spotify_stem(meta)
    os.makedirs(folder, exist_ok=True)
    there = find_existing(folder, meta)
    if there:
        slog.info("track %s: already saved as %s (matched on Artists - Title), nothing to do", tl, there)
        return {"ok": True, "path": there, "files": [], "skipped": 1, "output": ""}
    with _track_budget():
        urls, msg = spotify_candidates(meta)
        if not urls:
            slog.error("track %s: FAILED, no upload to download: %s", tl, msg)
            return {"ok": False, "output": f"{meta.get('title')}: {msg}"}
        tid = _track_id(meta)
        year, cover_url, album = "", meta.get("cover") or "", meta.get("album") or ""
        try:        # this track's own embed: the album art of THIS song (a playlist's cover is not it) + release year + album
            e = _spotify_embed("track", tid)
            cover_url = _best_image(e) or cover_url
            year = ((e.get("releaseDate") or {}).get("isoString") or "")[:10]
            album = album or _embed_album(e)
            slog.info("track %s: own embed page: year=%r album=%r cover=%s", tl, year, album, cover_url)
        except Exception as ex:
            slog.warning("track %s: its own embed page could not be read (%s: %s); using the list's cover and album", tl, type(ex).__name__, ex)
        outtmpl = tmpl_escape(folder / stem) + ".%(ext)s"
        slog.info("track %s: downloading %d candidate upload(s) to %s", tl, len(urls), outtmpl)
        ok, path, last_err = _download_candidates(urls, outtmpl, mp3)
        if not ok or not path:
            reason = _yt_explain(last_err, urls[0])
            slog.error("track %s: FAILED after %.1fs: %s (raw: %s)", tl, time.monotonic() - t0, reason, _errline(last_err))
            return {"ok": False, "output": f"{meta.get('title')}: {reason}"}
    cover = _cover_bytes(cover_url) or (_cover_bytes(meta.get("cover")) if meta.get("cover") != cover_url else None)
    slog.info("track %s: cover %s", tl, f"{len(cover)} bytes" if cover else "none (not embedded)")
    notes = []
    caveat = spotify_tag(path, {**meta, "album": album}, cover, year)
    slog.log(20 if not caveat else 30, "track %s: tags %s", tl, caveat or "written")
    if caveat:
        notes.append(caveat)
    if analyze:        # read-only analysis of the saved file; only its name changes
        res = _analyze(path)
        if res:
            bpm, keys = res
            new = path.with_name(f"{path.stem} ({bpm} BPM {' '.join(keys)}){path.suffix}")
            if not new.exists():
                slog.info("track %s: rename %s -> %s", tl, path.name, new.name)
                path.rename(new)
                path = new
            else:
                slog.warning("track %s: %s already exists, keeping %s", tl, new.name, path.name)
        else:
            notes.append("no BPM/key analyzer installed — run native/install.sh")
    slog.info("track %s: DONE in %.1fs -> %s", tl, time.monotonic() - t0, path)
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
    log.info("session probe for %s: gallery-dl's python is %s", site, interp)
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
    log.info("session probe for %s: browsers holding a session: %s", site, found or "none")
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
    downloaded, skipped, messages, everything = [], [], [], []
    cmd = build_cmd(site, url, cookie_args, outdir)
    t0 = jlog.exec_start(log, cmd)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as exc:
        jlog.exec_done(log, cmd, 1, t0, "", str(exc), "could not start")
        return [], [], [str(exc)], 1
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        everything.append(line)
        if line.startswith("# "):
            skipped.append(line[2:].strip())
        elif line.startswith("/"):
            downloaded.append(line.strip())
        else:
            messages.append(line)
    proc.wait()
    jlog.exec_done(log, cmd, proc.returncode, t0, "\n".join(everything), "", f"{len(downloaded)} downloaded, {len(skipped)} skipped")
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
    strategies = cookie_strategies(site)
    log.info("%s: %s (%s, %s) outdir=%s, login sources to try: %s", site, url, kind, label, outdir, [n for n, _ in strategies])
    for source, cookie_args in strategies:
        log.info("%s: trying login source [%s]", site, source)
        downloaded, skipped, messages, code = attempt(site, url, cookie_args, outdir)
        if downloaded or skipped:
            log.info("%s: source [%s] worked: %d downloaded, %d already there", site, source, len(downloaded), len(skipped))
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
            log.warning("%s: source [%s] failed (rc=%s) and it is not a login problem, not trying other sources", site, source, code)
            break   # not a login problem — another cookie source won't help
        log.warning("%s: source [%s] bounced to the login page (rc=%s), trying the next source", site, source, code)
    out = _social_explain(site, messages, code, kind)
    log.error("%s: FAILED: %s", site, out)
    return {"ok": False, "output": out}


# ══════════════════════════════════════════════════════════════════════════════ finishing steps
IMAGE_META = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff", ".gif", ".avif"}
VIDEO_META = {".mp4", ".m4v", ".mov", ".m4a", ".mp3", ".webm", ".opus", ".ogg", ".oga", ".flac"}


def strip_metadata(path):
    """Remove all metadata in place: images with exiftool (EXIF, XMP, IPTC, GPS …), audio/video with
    ffmpeg (tags, chapters, encoder/handler strings; streams copied, not re-encoded). Returns (ok, message)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMAGE_META:
        if not shutil.which("exiftool"):
            return False, "exiftool not installed — click Install missing dependencies or run native/install.sh; by hand: " + tool_howto("exiftool")
        code, out, err = _run(["exiftool", "-all=", "-overwrite_original", "-q", str(p)], timeout=300)
        log.info("strip metadata (exiftool) %s: %s", p.name, "ok" if code == 0 else f"FAILED rc={code}")
        return (True, "") if code == 0 else (False, (err or out).strip().splitlines()[-1] if (err or out).strip() else "exiftool failed")
    if ext in VIDEO_META:
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg not installed — click Install missing dependencies or run native/install.sh; by hand: " + tool_howto("ffmpeg")
        tmp = p.with_name(p.stem + ".strip-tmp" + p.suffix)
        code, _, err = _run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-map", "0", "-map_metadata", "-1", "-map_chapters", "-1",
                             "-c", "copy", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", *(["-movflags", "+faststart"] if ext in (".mp4", ".m4v", ".mov", ".m4a") else []), str(tmp)], timeout=1800)
        if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(p)
            log.info("strip metadata (ffmpeg) %s: ok", p.name)
            return True, ""
        if tmp.exists():
            tmp.unlink()
        log.error("strip metadata (ffmpeg) %s: FAILED rc=%s", p.name, code)
        return False, (err.strip().splitlines() or ["ffmpeg failed"])[-1]
    log.debug("strip metadata: %s has no metadata step for %r, left alone", p.name, ext)
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
        log.error("webp -> %s: sips failed for %s (rc=%s)", fmt, p.name, code)
        return False, (err.strip() or "sips failed")
    p.unlink()
    log.info("webp -> %s: %s -> %s (original removed)", fmt, p.name, out.name)
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
        log.info("finish %s: strip=%s webp=%s -> %s ok=%s%s", path, bool(strip), webp or False, cur, ok, f" ({msg})" if msg else "")
    return {"results": results}


# ══════════════════════════════════════════════════════════════════════════════ entry
def fetch(url, mode="video", outdir="", meta=None, analyze=False, mp3=False):
    """Run the engine for a page URL; the whole call and its outcome are in the log."""
    t0 = time.monotonic()
    log.info("fetch: %s site=%s mode=%s outdir=%s analyze=%s mp3=%s%s", url, site_of(url), mode, outdir or "~/Downloads", analyze, mp3,
             f" meta={jlog.summarize({'meta': meta})}" if meta else "")
    r = _fetch(url, mode, outdir, meta, analyze, mp3)
    (log.info if r.get("ok") else log.error)("fetch done in %.1fs: ok=%s path=%s output=%s", time.monotonic() - t0, r.get("ok"), r.get("path"), (r.get("output") or "")[:400])
    return r


def _fetch(url, mode="video", outdir="", meta=None, analyze=False, mp3=False):
    outdir = str(Path(outdir or DOWNLOADS).expanduser())
    try:
        os.makedirs(outdir, exist_ok=True)
        site = site_of(url)
        if site == "youtube":
            if os.environ.get("JOWNLOADER_DRYRUN") and mode == "audio":
                return {"ok": True, "output": "dry-run " + json.dumps({"mp3": bool(mp3), "args": audio_args(bool(mp3))})}
            return youtube(url, "audio" if mode == "audio" else "video", outdir, bool(mp3))
        if site in ("instagram", "twitter"):
            return social(site, url, outdir)
        if site == "spotify":
            if not meta:
                return {"ok": False, "output": "Spotify links go through the list first (spotify_list)"}
            if os.environ.get("JOWNLOADER_DRYRUN"):      # tests: show what reached the engine, touch nothing
                return {"ok": True, "output": "dry-run " + json.dumps({"analyze": bool(analyze), "mp3": bool(mp3), "args": audio_args(bool(mp3)), "folder": meta.get("folder"), "number": meta.get("number")})}
            return spotify_track(meta, outdir, bool(analyze), bool(mp3))
        return {"ok": False, "output": "no engine for this site"}
    except Exception as e:      # never let a site module take the host down
        log.exception("fetch of %s raised", url)
        return {"ok": False, "output": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if "--list" in sys.argv:        # engine.py --list <spotify url>  -> the track list as JSON
        print(json.dumps(spotify_list(sys.argv[sys.argv.index("--list") + 1]), indent=1))
        sys.exit(0)
    MP3 = "--mp3" in sys.argv
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
            res.append(fetch(t["url"], "audio", args[1] if len(args) > 1 else "", meta, "--bpm" in sys.argv, MP3))
            if len(lst["tracks"]) > 1:
                time.sleep(SPOTIFY_GAP_S)
        print(json.dumps(res, indent=1))
        sys.exit(0 if all(r.get("ok") for r in res) else 1)
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    r = fetch(pos[0], pos[2] if len(pos) > 2 else "video", pos[1] if len(pos) > 1 else "", mp3=MP3)
    print(json.dumps(r, indent=1))
    sys.exit(0 if r.get("ok") else 1)
