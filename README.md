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
- **Rename files** (Tools) — forgot the prefix? Type it, pick the files in one Finder dialog, and they
  become `trip_1.mp4, trip_2.mp4 …` (oldest first, numbering continues where that folder's downloads
  left off; `[date]` tokens take each file's modification date). Switch on *Move them to another folder* to send them
  to another folder instead, moved or copied. Nothing is ever overwritten.
- **Popup or side panel** — the panel stays open and rescans as you browse. One page, three views:
  *This page* (what the page offers, with counts, plus the finishing switches), *PDF / Markdown* and *Tools*;
  the folder everything lands in is always shown at the top.
- **Stop** — whatever is running (a batch, a Spotify list, a site crawl, a stream) has a Stop button in the
  run panel at the bottom of the popup and the side panel. Nothing new starts, files in flight are cut off and
  their partial files removed (the host kills a running yt-dlp / gallery-dl / ffmpeg), finished files stay:
  *Stopped — 12 saved, 30 not started*. Running again skips what was saved.
- **One running view** — n of m, a cell per file (saved, failed, in flight, waiting), what is downloading
  now, and every failure with its reason; the toolbar badge counts the files still to go.
- **Uses your login** — files are fetched by the browser itself, so anything you can see, it can save.
- **Streams too** — an HLS / DASH player (`.m3u8` / `.mpd`) is saved as one `.mp4`: best variant, segments
  fetched in order with your session, joined by `ffmpeg` (without it the raw tracks are kept and the
  popup says so). Clear-key AES-128 HLS is handled; DRM is refused.
- **YouTube, Instagram, Twitter/X** — on those pages the button reads *Save this video / this post /
  everything from @name* and the whole thing comes down through the site's own API with your browser
  session: YouTube at its best resolution as a QuickTime-playable `.mp4` (H.264 + AAC as is; 1440p/4K, which exist only as VP9/AV1, are re-encoded to HEVC with the Mac's hardware encoder), or *Audio only*: the upload's AAC
  saved exactly as delivered (`.m4a`, never re-encoded; Opus stays `.opus`), with the BPM and top-3 keys
  detected and written into the name (`Song (128.0 BPM Am C F).m4a`) — switch on **Save audio as MP3**
  (right under the button, and on the Spotify card) to get an MP3 (V0) instead; an Instagram post,
  reel or carousel, or a whole profile (posts, reels, highlights, avatar) into `<username>/`, 1080p
  video converted from VP9 to H.264; a tweet's media or a user's media timeline into `<name>/`.
- **Strip metadata** switch — EXIF, GPS, XMP and every other tag removed from each file as it is saved
  (images via exiftool, audio and video via ffmpeg, streams copied not re-encoded).
- **Convert WebP** switch — every `.webp` saved becomes a `.jpg` at quality 100, or a `.png`.
  Both are also actions in Tools → *Clean up the selected files* for files already on disk.

## Spotify links → tagged audio

Open a Spotify track, album, playlist or artist page and the popup offers **Download this list as audio
(N songs)**. The song list is read from the link's public embed page — no sign-in, no developer key (an
artist link lists the artist's top tracks only). Each song is matched to the best upload by length (within
3 s; anything more than 15 s off is never taken), title and uploader, and its audio is saved exactly as
the upload carries it — AAC in `.m4a`, never re-encoded (if an upload only has Opus, that is kept as
`.opus`). Files land in one folder per list: `<destination>/<list name>/NN - Artists - Title.m4a`, tagged
with title, artist, album, track number, release year and the song's own album art. A song nothing
matches is reported by name and the rest carry on. Songs are fetched one at a time, 20 s apart.
With **Save audio as MP3** on, songs are converted to MP3 (V0) and tagged with ID3 (title, artist,
album, track, year, cover) as `.mp3` instead; the switch is shared with the *Audio only* button.

- A playlist longer than the embed page's 100 songs is read off the open page by scrolling it.
- Run the same list again and only missing songs download; files already in the list's folder are kept
  and renamed in place to the list's current order. Nothing is ever deleted or overwritten — a song that
  left the list keeps its file.
- **Also count songs in these folders as already downloaded**: add library folders (one sub-folder per
  list, files named `NN_Artist - Title.ext`); songs found there are skipped, not copied. A folder that is
  not mounted is noted and skipped.
- **BPM + key in the names** (off by default): each file is analysed read-only and renamed
  `Title (128.0 BPM Am C F).m4a`.
- Needs `yt-dlp`, `ffmpeg` and `mutagen` (all installed by `native/install.sh`).

## Whole site → PDF / Markdown

By default (**This page only**) saves just the page you're on. Choose **Follow links on this site** and it
crawls every same-origin page reachable from there (breadth-first, capped at the max-pages number you set,
default 50). Either way the result is one merged PDF or one Markdown file, named by the first page's title.
A crawl skips links that download a file, pages that answer with an error (HTTP 400 and up), a redirect
target it already has, and links that change something just by being opened (log out, delete, remove,
unsubscribe, cart actions, or a link with an action/token query parameter). No
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

## Permissions and privacy

What the extension asks for, and why:

- **All sites (`<all_urls>`)**: it reads the page you are on to find images, videos and documents, and
  injects a small script into every page (in the page's own context) that watches the page's own
  `fetch` / XHR traffic for stream manifests (HLS / DASH). Nothing it reads is sent anywhere by the
  extension; the files it saves are fetched with your browser's own session, straight to disk.
- **`debugger`**: to read stream traffic and print pages to PDF on demand.
- **`nativeMessaging`**: to talk to the local host that writes files. The host runs only on your Mac.
- **`tabs`, `webNavigation`, `webRequest`, `storage`, `sidePanel`, `notifications`**: following the
  active tab, remembering what was already saved, the side panel and the finish notification.
- **`cookies`**: for a download the engine runs (a video link, a music-list song, a social post), the
  extension reads that site's session cookies and hands them to the local download tool for that one job,
  in a private temp file that is deleted when the job ends. They are never stored by the extension, never
  logged, and nothing leaves the machine.
- **Cookies**: for pages that need a login, the download tools (`yt-dlp`, `gallery-dl`) may read your
  browser's cookies for that site, locally, on your Mac, and only to make that one request. They are not
  copied, stored or sent anywhere else.
- **Install missing dependencies** can run Homebrew's official installer (the `curl | bash` line in
  `native/deps.sh`) when Homebrew is absent; read the script before pressing it.

## Install (macOS)

Files are written by a small native-messaging host, a Python script the browser launches on demand.
That is what makes "any folder, no dialog" possible; Chrome's own download API can't do it.

**Download:** [Install-Jownloader.zip](https://github.com/thuhmadhatter42/jownloader/releases/latest/download/Install-Jownloader.zip)
→ open it → double-click **Install Jownloader.command**. It is unsigned, so the first time macOS says it can't
check it: click Done, then System Settings → Privacy & Security → **Open Anyway** (once). It runs the same
install as the command below, in a Terminal window.

Or paste this into Terminal (no macOS warning; it asks for your Mac password once, and installs everything else itself:
Apple's Command Line Tools if missing, the code in `~/Applications/Jownloader`, Homebrew, the tools below):

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/thuhmadhatter42/jownloader/master/install)"
```

Then the one step a script cannot do (a browser will not load an unpacked extension on its own): the
installer copies the `extension` folder path to the clipboard, highlights the folder in Finder and opens
`brave://extensions` (or `chrome://extensions`) — turn on **Developer mode**, drag the `extension` folder onto
the page (or **Load unpacked**, Cmd+Shift+G, Cmd+V), and pin Jownloader from the puzzle-piece menu.

To update, run the same command again (the code is replaced; `native/.venv` is kept).

By hand instead: clone this repo somewhere it can stay (the browser calls the host by absolute path) and run
`bash native/install.sh` — it registers the host with Brave, Chrome and Chromium and installs the
engine's tools: `yt-dlp`, `ffmpeg`, `gallery-dl`, `exiftool` and the BPM/key
analyzer (`essentia-tensorflow`, or `librosa` where no wheel exists). `--no-deps` skips that part.
It needs `python3` on the system path; macOS provides it with the Xcode Command Line Tools
(`xcode-select --install`). Then load `extension/` as above.

If you move the folder, run `native/install.sh` again. If the host isn't reachable, every file in a
batch fails with a message saying so; nothing is saved silently anywhere else.

How the tools are installed: `yt-dlp` and `gallery-dl` through `pipx` when it is installed (prebuilt,
nothing to compile), otherwise Homebrew. Before any Homebrew install the script asks Homebrew whether
the formula or one of its dependencies has no prebuilt bottle for your macOS (newer and older macOS
releases outside Homebrew's support tiers have none, and Homebrew would then compile for a long time
and may need full Xcode). If so it skips Homebrew and prints a one-line "install it by hand" instruction
instead (for `yt-dlp` / `gallery-dl` it installs `pipx` first when that is a prebuilt download). The
last lines of the install list which tools are actually present and which are missing; only a host
that cannot run (no `native/.venv`) makes the script fail, a missing tool does not. The Python libraries
and the BPM/key analyzer live in `native/.venv`. If Homebrew's Python is not installed that venv is
built on Apple's Python 3.9, which works for the PDF and tag libraries and, where wheels exist, the
analyzer; if the analyzer cannot install there, run `brew install python` and then **Install missing
dependencies** again (the venv is rebuilt on Homebrew's Python).

With neither Homebrew nor `pipx` (for example a Mac whose user is not an administrator) the script does
not stop: it prints which tools are present and which are missing, with the exact command for each, and the
popup's Dependencies row shows the same. Without pipx: `python3 -m pip install --user pipx && python3 -m pipx install yt-dlp`
(pip's `--user` scripts are not on `PATH`, hence `python3 -m pipx`). On Apple's Python 3.9 alone, pip stops
at an old `yt-dlp`; the installer warns, and a newer Python (python.org or Homebrew) then
`pipx reinstall --python python3 yt-dlp` fixes it. A `native/.venv` whose Python was removed or whose
`pyvenv.cfg` is missing is rebuilt automatically.

`native/install.sh` registers the host and installs the tools above. After that,
the extension keeps them current on its own: it checks on every install/update and installs anything
missing automatically (a badge on the icon shows while that runs), and the popup has its own
"Dependencies" row with a ✓/✗ per tool and an **Install missing dependencies** button for whenever
one needs a manual nudge (e.g. after a Homebrew upgrade removes something).

## Logs

One developer log holds everything that happens on both sides: the extension (saves, dedupe decisions,
host connects, crawl and stream decisions, Spotify progress), the native host (every message in and out,
every subprocess with its full command line, exit code, timing and output tail) and the engine (each
download strategy tried and why it failed, every Spotify match with its score and the reason a result
was rejected).

```
~/Library/Logs/Jownloader/jownloader.log      (5 files of 5 MB: jownloader.log, .1 … .5)
```

- Tools → **Log** in the popup: **Open log** opens the file in TextEdit, **Copy last 200 lines**
  puts the tail on the clipboard (paste it into a bug report).
- The level is `debug` by default. Quiet it with `JOWN_LOG=info` (or `warning`, `error`) in the
  environment the host starts with, for example `launchctl setenv JOWN_LOG info` and then restart the browser.
- Cookie, token and password values are replaced by `***` before anything is written; file data is never logged
  (chunks are only counted). Cookie file *paths* do appear.
- `JOWN_LOG_DIR` moves the file (the tests use a temp folder).
- Failures also show in the popup: every failed song or file is listed with its reason, and a run that saved
  nothing ends on a red error line instead of a plain "done".

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
python3 tests/host_test.py         # the host alone: batch rename / move / copy, collisions, WebP → JPEG / PNG, strip metadata,
                                   # Spotify lists / matching / tags / re-run rules (saved pages in tests/fixtures, no network)
python3 tests/log_test.py          # the developer log: file, redaction, subprocess lines, rotation, two hosts at once
python3 tests/streams_test_cdp.py  # real Brave: HLS (TS, AES-128, fMP4 + audio group) and DASH joined into .mp4, DRM refused
python3 tests/engine_test_cdp.py   # real Brave: a YouTube page → H.264 .mp4 and a BPM/key-named audio file (network)
python3 tests/site_test_cdp.py     # real Brave: Whole-site crawl (its own local fixture site) → merged PDF and Markdown
```

Both need `pip install playwright` and `playwright install chromium`. The second needs Brave in
`/Applications` and the host installed.

## License

MIT
