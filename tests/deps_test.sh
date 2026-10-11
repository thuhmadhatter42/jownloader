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


# ---- 0.1.42: pipx preferred, no-bottle detection, honest success line (all stubbed) ----
# brew stub: `ruby` prints $STUB_NOBOTTLE_<tool> style answers (per the formulae asked about); `install` is
# logged; `install pipx` makes a pipx stub appear; `--prefix` is empty.
rm -f "$T/bin/pipx" "$T/bin/gallery-dl"
cat > "$T/bin/brew" <<'EOF'
#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
case "$1" in
  ruby) shift 3; for a in "$@"; do case " $STUB_NOBOTTLE " in *" $a "*) echo "$a-dep";; esac; done; exit 0 ;;
  install) if [ "$2" = pipx ]; then printf '#!/bin/sh\necho "pipx $*" >> "$STUB_LOG"\nexit 0\n' > "$STUB_BIN/pipx"; chmod +x "$STUB_BIN/pipx"; fi; exit 0 ;;
esac
exit 0
EOF
chmod +x "$T/bin/brew"
printf '#!/bin/sh\necho "pipx $*" >> "$STUB_LOG"\nexit 0\n' > "$T/pipx.stub"; chmod +x "$T/pipx.stub"
export STUB_BIN="$T/bin"
run() { PATH="$T/bin:/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; $1" 2>&1; }

# A. pipx present, brew could build everything from bottles: yt-dlp / gallery-dl go through pipx, exiftool through brew
: > "$T/log"; cp "$T/pipx.stub" "$T/bin/pipx"; export STUB_NOBOTTLE=""
run "install_tool yt-dlp; install_tool gallery-dl; install_tool exiftool" >/dev/null
check "A pipx present -> pipx install yt-dlp" "$(grep -c '^pipx install yt-dlp' "$T/log")" "1"
check "A pipx present -> pipx install gallery-dl" "$(grep -c '^pipx install gallery-dl' "$T/log")" "1"
check "A pipx present -> no brew install for the python tools" "$(grep -cE '^brew install (yt-dlp|gallery-dl)' "$T/log")" "0"
check "A exiftool still brew (bottled)" "$(grep -c '^brew install exiftool' "$T/log")" "1"

# B. no pipx, brew would compile ffmpeg's deps: brew skipped, one clear line
: > "$T/log"; rm -f "$T/bin/pipx"; export STUB_NOBOTTLE="ffmpeg"
out="$(run "install_tool ffmpeg")"
check "B no-bottle -> brew install NOT run" "$(grep -c '^brew install' "$T/log")" "0"
check "B no-bottle -> clear message" "$(echo "$out" | grep -c 'no prebuilt package for this macOS — install ffmpeg by hand: download a static build')" "1"

# C. no pipx, yt-dlp would compile but pipx is a bottle: brew installs pipx only, then pipx installs yt-dlp
: > "$T/log"; rm -f "$T/bin/pipx"; export STUB_NOBOTTLE="yt-dlp"
run "install_tool yt-dlp" >/dev/null
check "C brew install pipx runs" "$(grep -c '^brew install pipx' "$T/log")" "1"
check "C then pipx install yt-dlp" "$(grep -c '^pipx install yt-dlp' "$T/log")" "1"
check "C never brew install yt-dlp" "$(grep -c '^brew install yt-dlp' "$T/log")" "0"

# D. no pipx and pipx has no bottle either: nothing installed, message says how
: > "$T/log"; rm -f "$T/bin/pipx"; export STUB_NOBOTTLE="yt-dlp pipx"
out="$(run "install_tool yt-dlp; echo rc=\$?")"
check "D no brew install at all" "$(grep -c '^brew install' "$T/log")" "0"
check "D message names pipx by hand" "$(echo "$out" | grep -c 'install yt-dlp by hand: pipx install yt-dlp')" "1"
check "D rc 0 (set -e survives)" "$(echo "$out" | tail -1)" "rc=0"

# E. brew itself unable to answer (ruby fails): brew is allowed to try, as before 0.1.42
printf '#!/bin/sh\necho "brew $*" >> "$STUB_LOG"\n[ "$1" = ruby ] && exit 1\nexit 0\n' > "$T/bin/brew"
: > "$T/log"; rm -f "$T/bin/pipx"
run "install_tool exiftool" >/dev/null
check "E brew cannot tell -> brew install runs" "$(grep -c '^brew install exiftool' "$T/log")" "1"

# F. success line = what is really there. Search path is overridable so this Mac's real tools do not leak in.
mkdir -p "$T/found" "$T/home/.local/bin"
printf '#!/bin/sh\n' > "$T/found/yt-dlp"; printf '#!/bin/sh\n' > "$T/found/ffmpeg"; printf '#!/bin/sh\n' > "$T/home/.local/bin/gallery-dl"
chmod +x "$T/found/yt-dlp" "$T/found/ffmpeg" "$T/home/.local/bin/gallery-dl"
out="$(JOWN_TOOL_PATHS="$T/found" PATH="/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; engine_tools_report; echo rc=\$?")"
check "F present line lists real tools incl ~/.local/bin" "$(echo "$out" | grep -c '^engine tools present: yt-dlp, ffmpeg, gallery-dl$')" "1"
check "F missing section lists exiftool with a hint" "$(echo "$out" | grep -c '^  exiftool — install the macOS package')" "1"
check "F yt-dlp not reported missing" "$(echo "$out" | grep -c '^  yt-dlp')" "0"
check "F report returns 0 (optional tools)" "$(echo "$out" | tail -1)" "rc=0"
out="$(JOWN_TOOL_PATHS="$T/found" PATH="/usr/bin:/bin" bash -c "source '$HERE/../native/deps.sh'; HOME='$T/empty'; engine_tools_report")"
check "F nothing in ~/.local/bin -> gallery-dl missing" "$(echo "$out" | grep -c '^  gallery-dl — pipx install gallery-dl')" "1"

# G. the Apple-Python venv message is the exact text the host also sends
check "G APPLE_PY_HINT text" "$(PATH=/usr/bin:/bin bash -c "source '$HERE/../native/deps.sh'; echo \"\$APPLE_PY_HINT\"")" "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"
check "G host sends the same text" "$(grep -c '^APPLE_PY_HINT = "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"$' "$HERE/../native/jownloader_host.py")" "1"

[ "$fails" = 0 ] && echo "ALL GREEN" || { echo "$fails FAILED"; exit 1; }
