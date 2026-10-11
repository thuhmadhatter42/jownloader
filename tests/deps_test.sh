#!/bin/bash
# deps.sh fallbacks, with brew and pipx stubbed in a temp PATH (nothing is installed or removed):
#  - install_tool: a failing `brew install` falls back to `pipx install` (yt-dlp / gallery-dl only), never dies under set -e
#  - gallery_dl_interp: a /bin/sh shim resolves to the pipx venv's python, a real shebang is returned as is
# Run: bash tests/deps_test.sh
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d "$HERE/deps-test.XXXXXX")"
REAL_HOME="$HOME"; trap 'HOME="$REAL_HOME" /usr/bin/trash "$T"' EXIT
fails=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1: $2"; else echo "FAIL $1: $2   want $3"; fails=$((fails+1)); fi; }

mkdir -p "$T/bin" "$T/home/.local/bin" "$T/venvs/gallery-dl/bin"
cat > "$T/bin/brew" <<'EOF'
#!/bin/sh
echo "brew $*" >> "$STUB_LOG"; exit 1
EOF
cat > "$T/bin/pipx" <<'EOF'
#!/bin/sh
echo "pipx $*" >> "$STUB_LOG"
if [ "$1" = environment ]; then echo "$STUB_VENVS"; fi
exit 0
EOF
chmod +x "$T/bin/brew" "$T/bin/pipx"
export STUB_LOG="$T/log" STUB_VENVS="$T/venvs" HOME="$T/home"

# stubbed PATH: only the stubs + the system tools; the real brew / pipx are not reachable
(
  export PATH="$T/bin:/usr/bin:/bin"
  set +e
  source "$HERE/../native/deps.sh"
  install_tool yt-dlp >/dev/null 2>&1; echo "rc_ytdlp=$?" > "$T/rc"
  install_tool exiftool >/dev/null 2>&1; echo "rc_exiftool=$?" >> "$T/rc"
)
check "brew failing for yt-dlp -> pipx install runs" "$(grep -c '^pipx install yt-dlp' "$T/log")" "1"
check "brew failing for exiftool -> no pipx fallback (not a pipx package)" "$(grep -c '^pipx install exiftool' "$T/log")" "0"
check "install_tool returns 0 either way (set -e survives)" "$(tr '\n' ' ' < "$T/rc")" "rc_ytdlp=0 rc_exiftool=0 "
# no pipx at all: brew failure is reported, not fatal
rm "$T/bin/pipx"
out="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; install_tool gallery-dl; echo rc=\$?" 2>&1)"
check "no pipx: brew failure reported, rc 0" "$(echo "$out" | tail -1)" "rc=0"

# gallery_dl_interp
printf '#!/bin/sh\nexit 0\n' > "$T/bin/gallery-dl"; chmod +x "$T/bin/gallery-dl"
printf '#!/bin/sh\nexit 0\n' > "$T/venvs/gallery-dl/bin/python"; chmod +x "$T/venvs/gallery-dl/bin/python"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; gallery_dl_interp")"
check "sh shim, default pipx venv dir -> not the shim's /bin/sh (no pipx: falls to HOME paths -> empty)" "$r" ""
mkdir -p "$T/home/Library/Application Support/pipx/venvs/gallery-dl/bin"
cp "$T/venvs/gallery-dl/bin/python" "$T/home/Library/Application Support/pipx/venvs/gallery-dl/bin/python"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; gallery_dl_interp")"
check "sh shim -> pipx venv python under Application Support" "$r" "$T/home/Library/Application Support/pipx/venvs/gallery-dl/bin/python"
printf '#!/bin/sh\necho "$STUB_VENVS"\n' > "$T/bin/pipx"; chmod +x "$T/bin/pipx"
mv "$T/home/Library" "$T/home/Library.gone"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; gallery_dl_interp")"
check "sh shim -> venv python from pipx environment" "$r" "$T/venvs/gallery-dl/bin/python"
printf '#!/opt/some/python3.14\nimport sys\n' > "$T/bin/gallery-dl"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; gallery_dl_interp")"
check "real shebang returned as is" "$r" "/opt/some/python3.14"

[ "$fails" = 0 ] && echo "ALL GREEN" || { echo "$fails FAILED"; exit 1; }
