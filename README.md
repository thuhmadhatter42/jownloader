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
  fetched in order with your session, joined by `ffmpeg` (`brew install ffmpeg`; without it the raw
  tracks are kept and the popup says so). Clear-key AES-128 HLS is handled; DRM is refused.

## What it won't do

- **DRM video** (Widevine / PlayReady) is reported as protected and never saved. Encrypted bytes are
  refused before a file is written. There is no circumvention here and none is planned.
- **YouTube** — its tracks are throttled, split and signature-scrambled; that is `yt-dlp`'s job.

## Install (macOS)

Files are written by a small native-messaging host, a Python script the browser launches on demand.
That is what makes "any folder, no dialog" possible; Chrome's own download API can't do it.

1. Clone this repo somewhere it can stay (the browser calls the host by absolute path).
2. `bash native/install.sh` — registers the host with Brave, Chrome and Chromium. Needs `python3`
   on the system path; macOS provides it with the Xcode Command Line Tools (`xcode-select --install`).
3. `brave://extensions` (or `chrome://extensions`) → **Developer mode** on → **Load unpacked** →
   choose the `extension/` folder.
4. Pin Jownloader from the puzzle-piece menu.

If you move the folder, run `native/install.sh` again. If the host isn't reachable, every file in a
batch fails with a message saying so; nothing is saved silently anywhere else.

## Layout

```
extension/   the extension (manifest v3, service worker, content script, popup / side panel)
native/      the native host and its installer
tests/       Playwright harness (no host) and a real-browser CDP test of the write path
```

## Tests

```
python3 tests/ext_test.py          # Chromium via Playwright: scanning, naming, dedupe, DRM check, no-host failure
python3 tests/stream_test_cdp.py   # real Brave over CDP: host writes, chosen folder, [date] names, skip-if-on-disk
python3 tests/host_test.py         # the host alone: batch rename / move / copy, collisions, missing files
```

Both need `pip install playwright` and `playwright install chromium`. The second needs Brave in
`/Applications` and the host installed.

## License

MIT
