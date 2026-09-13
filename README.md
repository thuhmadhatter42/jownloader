# Jownloader

A Brave / Chrome extension that saves every image, video and document on the page you're looking at,
at the best quality the page offers, into any folder you pick. No save dialogs, no re-downloading what
you already have.

- **Download all images / videos / documents** — one click each, counts shown before you click.
- **Best quality, one file per item** — the largest `srcset` / `<picture>` candidate per image; for
  videos, the best variant the player offers (`<source size>`, KVS `flashvars`, JW Player-style
  `sources` lists), else the file that is playing.
- **⬇ button on every video player**, labeled with the real pixel size of what is playing (or the
  site's own quality label when a bigger variant is offered).
- **Collect as I scroll** — turn it on, browse a feed or lightbox, and every full-size picture shown
  on screen is remembered; thumbnails are not. One button saves the lot.
- **Any folder** — pick it once in a Finder dialog; files stream straight there.
- **Naming** — files keep their own names, or type a prefix (`trip` → `trip_1.jpg, trip_2.jpg …`).
  `[yy-mm-dd]`, `[mm-d-yyyy]`, `[jan-d-yyyy]` … in the prefix become the item's upload date
  (from the page markup), else the page's date, else today.
- **Never twice** — every saved URL is remembered and skipped while its file is still in its
  folder. Trash the file and it downloads again. Nothing is ever overwritten (`name (2).ext`).
- **Batch rename** tab — forgot the prefix? Type it, pick the files in one Finder dialog, and they
  become `trip_1.mp4, trip_2.mp4 …` (oldest first, numbering continues where that folder's downloads
  left off; `[date]` tokens take each file's modification date). Tick *Move the files?* to send them
  to another folder instead, moved or copied. Nothing is ever overwritten.
- **Popup or side panel** — the panel stays open and rescans as you browse.
- **Uses your login** — files are fetched by the browser itself, so anything you can see, it can save.
- **Streams too** — an HLS / DASH player (`.m3u8` / `.mpd`) is saved as one `.mp4`: best variant, segments
  fetched in order with your session, joined by `ffmpeg` (without it the raw tracks are kept and the
  popup says so). Clear-key AES-128 HLS is handled; DRM is refused.
- **YouTube, Instagram, Twitter/X** — on those pages the button reads *Download this video / this post /
  everything from @name* and the whole thing comes down through the site's own API with your browser
  session: YouTube as H.264 + AAC `.mp4` (QuickTime-native), or *Audio only* as `.mp3` with the BPM and
  top-3 keys detected and written into the name (`Song (128.0 BPM Am C F).mp3`); an Instagram post,
  reel or carousel, or a whole profile (posts, reels, highlights, avatar) into `<username>/`, 1080p
  video converted from VP9 to H.264; a tweet's media or a user's media timeline into `<name>/`.
- **Strip metadata** switch — EXIF, GPS, XMP and every other tag removed from each file as it is saved
  (images via exiftool, audio and video via ffmpeg, streams copied not re-encoded).
- **Convert WebP** switch — every `.webp` saved becomes a `.jpg` at quality 100, or a `.png`.
  Both are also actions in the Batch tab for files already on disk.

## Whole site → PDF / Markdown

Crawls every same-origin page reachable from the page you're on (breadth-first, capped at the max-pages
number you set, default 50) and saves the whole thing as one merged PDF or one Markdown file. No
external tools do the crawling — the extension drives it itself with `chrome.tabs` (navigate, wait for
load, follow same-origin links) and, for PDF, `chrome.tabs`'s DevTools debugger to render each page
exactly as Brave does. While a PDF crawl runs, Brave shows a "Jownloader started debugging this
browser" bar — harmless, and it goes away when the crawl finishes. Merging the per-page PDFs into one
file needs `pypdf`, installed automatically by `native/install.sh`; without it, the pages are saved
separately instead of merged.

## What it won't do

- **DRM video** (Widevine / PlayReady) is reported as protected and never saved. Encrypted bytes are
  refused before a file is written. There is no circumvention here and none is planned.
- **Live streams** have no end to save; they are refused until the site publishes the recording.

## Install (macOS)

Files are written by a small native-messaging host, a Python script the browser launches on demand.
That is what makes "any folder, no dialog" possible; Chrome's own download API can't do it.

1. Clone this repo somewhere it can stay (the browser calls the host by absolute path).
2. `bash native/install.sh` — registers the host with Brave, Chrome and Chromium and installs the
   engine's tools through Homebrew: `yt-dlp`, `ffmpeg`, `gallery-dl`, `exiftool` and the BPM/key
   analyzer (`essentia-tensorflow`, or `librosa` where no wheel exists). `--no-deps` skips that part.
   Needs `python3` on the system path; macOS provides it with the Xcode Command Line Tools
   (`xcode-select --install`).
3. `brave://extensions` (or `chrome://extensions`) → **Developer mode** on → **Load unpacked** →
   choose the `extension/` folder.
4. Pin Jownloader from the puzzle-piece menu.

If you move the folder, run `native/install.sh` again. If the host isn't reachable, every file in a
batch fails with a message saying so; nothing is saved silently anywhere else.

`native/install.sh` registers the host and installs the tools above through Homebrew. After that,
the extension keeps them current on its own: it checks on every install/update and installs anything
missing automatically (a badge on the icon shows while that runs), and the popup has its own
"Dependencies" row with a ✓/✗ per tool and an **Install missing dependencies** button for whenever
one needs a manual nudge (e.g. after a Homebrew upgrade removes something).

## Layout

```
extension/   the extension (manifest v3, service worker, content script, popup / side panel)
native/      the native host, the engine (site downloaders, finishing steps, BPM/key analyzer + model) and the installer
tests/       Playwright harness (no host) and real-browser CDP tests of the write path, streams and the engine
```

## Tests

```
python3 tests/ext_test.py          # Chromium via Playwright: scanning, naming, dedupe, DRM check, no-host failure
python3 tests/stream_test_cdp.py   # real Brave over CDP: host writes, chosen folder, [date] names, skip-if-on-disk
python3 tests/host_test.py         # the host alone: batch rename / move / copy, collisions, WebP → JPEG / PNG, strip metadata
python3 tests/streams_test_cdp.py  # real Brave: HLS (TS, AES-128, fMP4 + audio group) and DASH joined into .mp4, DRM refused
python3 tests/engine_test_cdp.py   # real Brave: a YouTube page → H.264 .mp4 and a BPM/key-named .mp3 (network)
python3 tests/site_test_cdp.py     # real Brave: Whole-site crawl (its own local fixture site) → merged PDF and Markdown
```

Both need `pip install playwright` and `playwright install chromium`. The second needs Brave in
`/Applications` and the host installed.

## License

MIT
