#!/bin/bash
# Registers the Jownloader native host with Brave, Chrome and Chromium. Rerun if this folder moves.
# The host is a stdio Python script; the browser launches it with the login PATH (/usr/bin:/bin:…), so
# python3 must resolve there — macOS provides it with the Xcode Command Line Tools (xcode-select --install).
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
