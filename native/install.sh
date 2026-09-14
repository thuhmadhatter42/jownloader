#!/bin/bash
# Registers the Jownloader native host with Brave, Chrome and Chromium, and installs the engine's tools
# (yt-dlp, ffmpeg, gallery-dl, exiftool) through Homebrew — `--no-deps` skips those brew installs (and
# the yt-dlp/gallery-dl upgrade). Rerun if this folder moves.
# The host is a stdio Python script; the browser launches it with the login PATH (/usr/bin:/bin:…), so
# python3 must resolve there — macOS provides it with the Xcode Command Line Tools (xcode-select
# --install) — but that's only the bootstrap: the host immediately re-execs into native/.venv, where
# pypdf and the BPM/key analyzer actually live. ensure_venv (below) builds that venv even with
# --no-deps, since it's cheap and the host is useless without it; only the brew installs are skipped.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ID="$(cat "$HERE/EXTENSION_ID")"
if ! env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin python3 -c 'import sys; assert sys.version_info >= (3, 8)' 2>/dev/null; then
  echo "error: no python3 (>= 3.8) on the browser's PATH (/usr/bin:/bin) — run: xcode-select --install" >&2
  exit 1
fi
chmod +x "$HERE/jownloader_host.py"
# Brave resolves user-level hosts from Chrome's directory (a manifest in Brave's own dir is "not found"),
# so all three are written.
for DIR in "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts" \
           "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts" \
           "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"; do
  mkdir -p "$DIR"
  cat > "$DIR/com.jshriver.jownloader.json" <<JSON
{
  "name": "com.jshriver.jownloader",
  "description": "Jownloader native host: writes the files the extension fetched",
  "path": "$HERE/jownloader_host.py",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$ID/"]
}
JSON
  echo "registered: $DIR/com.jshriver.jownloader.json"
done
source "$HERE/deps.sh"
if [ "$1" != "--no-deps" ]; then
  ensure_deps
  maybe_upgrade_ytdlp
  echo "engine tools ready: yt-dlp, ffmpeg, gallery-dl, exiftool"
else
  ensure_venv
fi
