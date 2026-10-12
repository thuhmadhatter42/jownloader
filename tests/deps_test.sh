#!/bin/bash
# deps.sh fallbacks, with brew and pipx stubbed in a temp PATH (nothing is installed or removed):
#  - install_tool: a failing `brew install` falls back to `pipx install` (yt-dlp / gallery-dl only), never dies under set -e
#  - gallery_dl_interp: a /bin/sh shim resolves to the pipx venv's python, a real shebang is returned as is
# Run: bash tests/deps_test.sh
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE="${JOWN_NATIVE:-$HERE/../native}"   # JOWN_NATIVE: test another copy of native/ (e.g. the pre-fix one)
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
  source "$NATIVE/deps.sh"
  install_tool yt-dlp >/dev/null 2>&1; echo "rc_ytdlp=$?" > "$T/rc"
  install_tool exiftool >/dev/null 2>&1; echo "rc_exiftool=$?" >> "$T/rc"
)
check "brew failing for yt-dlp -> pipx install runs" "$(grep -c '^pipx install yt-dlp' "$T/log")" "1"
check "brew failing for exiftool -> no pipx fallback (not a pipx package)" "$(grep -c '^pipx install exiftool' "$T/log")" "0"
check "install_tool returns 0 either way (set -e survives)" "$(tr '\n' ' ' < "$T/rc")" "rc_ytdlp=0 rc_exiftool=0 "
# no pipx at all: brew failure is reported, not fatal
rm "$T/bin/pipx"
out="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; install_tool gallery-dl; echo rc=\$?" 2>&1)"
check "no pipx: brew failure reported, rc 0" "$(echo "$out" | tail -1)" "rc=0"

# gallery_dl_interp
printf '#!/bin/sh\nexit 0\n' > "$T/bin/gallery-dl"; chmod +x "$T/bin/gallery-dl"
printf '#!/bin/sh\nexit 0\n' > "$T/venvs/gallery-dl/bin/python"; chmod +x "$T/venvs/gallery-dl/bin/python"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; gallery_dl_interp")"
check "sh shim, default pipx venv dir -> not the shim's /bin/sh (no pipx: falls to HOME paths -> empty)" "$r" ""
mkdir -p "$T/home/Library/Application Support/pipx/venvs/gallery-dl/bin"
cp "$T/venvs/gallery-dl/bin/python" "$T/home/Library/Application Support/pipx/venvs/gallery-dl/bin/python"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; gallery_dl_interp")"
check "sh shim -> pipx venv python under Application Support" "$r" "$T/home/Library/Application Support/pipx/venvs/gallery-dl/bin/python"
printf '#!/bin/sh\necho "$STUB_VENVS"\n' > "$T/bin/pipx"; chmod +x "$T/bin/pipx"
mv "$T/home/Library" "$T/home/Library.gone"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; gallery_dl_interp")"
check "sh shim -> venv python from pipx environment" "$r" "$T/venvs/gallery-dl/bin/python"
printf '#!/opt/some/python3.14\nimport sys\n' > "$T/bin/gallery-dl"
r="$(PATH="$T/bin:/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; gallery_dl_interp")"
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
run() { PATH="$T/bin:/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; $1" 2>&1; }

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
out="$(JOWN_TOOL_PATHS="$T/found" PATH="/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; engine_tools_report; echo rc=\$?")"
check "F present line lists real tools incl ~/.local/bin" "$(echo "$out" | grep -c '^engine tools present: yt-dlp, ffmpeg, gallery-dl$')" "1"
check "F missing section lists exiftool with a hint" "$(echo "$out" | grep -c '^  exiftool — install the macOS package')" "1"
check "F yt-dlp not reported missing" "$(echo "$out" | grep -c '^  yt-dlp')" "0"
check "F report returns 0 (optional tools)" "$(echo "$out" | tail -1)" "rc=0"
out="$(JOWN_TOOL_PATHS="$T/found" PATH="/usr/bin:/bin" bash -c "source '$NATIVE/deps.sh'; HOME='$T/empty'; engine_tools_report")"
check "F nothing in ~/.local/bin -> gallery-dl missing" "$(echo "$out" | grep -c '^  gallery-dl — pipx install gallery-dl')" "1"

# G. the Apple-Python venv message is the exact text the host also sends
check "G APPLE_PY_HINT text" "$(PATH=/usr/bin:/bin bash -c "source '$NATIVE/deps.sh'; echo \"\$APPLE_PY_HINT\"")" "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"
check "G host sends the same text" "$(grep -c '^APPLE_PY_HINT = "BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"$' "$NATIVE/jownloader_host.py")" "1"

# ---- 0.1.43: install path (venv health, Apple Python, set -e kill sites, pipx hints) — all offline, stubbed ----
rm -f "$T/bin/pipx" "$T/bin/brew" "$T/bin/gallery-dl"
mkdir -p "$T/n1" "$T/n2" "$T/wbin" "$T/bp" "$T/nobp/bin"
cp "$NATIVE/deps.sh" "$T/n1/"; cp "$NATIVE/deps.sh" "$T/n2/"
# python3 that builds venvs offline (--upgrade-deps would reach for PyPI) and logs that it was the builder
cat > "$T/wbin/python3" <<'EOF'
#!/bin/sh
echo "builder $0 $*" >> "$STUB_LOG"
for x; do shift; [ "$x" = --upgrade-deps ] || set -- "$@" "$x"; done
exec /usr/bin/python3 "$@"
EOF
chmod +x "$T/wbin/python3"
mkdir -p "$T/bp/bin"; cp "$T/wbin/python3" "$T/bp/bin/python3"
printf '#!/bin/sh\n[ "$1" = --prefix ] && echo "$STUB_BREWPREFIX"\nexit 0\n' > "$T/wbin/brew"; chmod +x "$T/wbin/brew"
export STUB_BREWPREFIX="$T/bp"
runv() { env -i HOME="$T/home" PATH="$T/wbin:/usr/bin:/bin" STUB_LOG="$T/log" STUB_BREWPREFIX="$STUB_BREWPREFIX" bash -c "source '$T/n1/deps.sh'; load_brew() { :; }; $1" 2>&1; }
: > "$T/log"

# H1 (#9 #20): a venv built on Apple's Python is recognised from pyvenv.cfg / base_prefix, not _base_executable
/usr/bin/python3 -m venv "$T/n1/.venv"
check "H1 Apple-Python venv detected" "$(runv 'venv_is_apple_python; echo rc=$?' | tail -1)" "rc=0"
printf '#!/bin/sh\necho /opt/homebrew/Cellar/python@3.14/3.14.0/Frameworks/Python.framework/Versions/3.14\n' > "$T/fakepy"; chmod +x "$T/fakepy"
cp "$T/n1/.venv/pyvenv.cfg" "$T/cfg.keep"; printf 'home = /opt/homebrew/opt/python@3.14/bin\nversion = 3.14.0\n' > "$T/n1/.venv/pyvenv.cfg"
check "H1 Homebrew-Python venv is not Apple" "$(runv "VENV_PY='$T/fakepy'; venv_is_apple_python; echo rc=\$?" | tail -1)" "rc=1"
cp "$T/cfg.keep" "$T/n1/.venv/pyvenv.cfg"

# H2 (#19): healthy Apple venv, no analyzer: rebuilt once Homebrew's python3 exists; left alone when it does not
touch "$T/n1/.venv/marker"; : > "$T/log"
out="$(STUB_BREWPREFIX="$T/nobp" runv 'ensure_venv; echo rc=$?')"
check "H2 no Homebrew python -> venv untouched" "$([ -e "$T/n1/.venv/marker" ] && echo kept)" "kept"
: > "$T/log"; out="$(runv 'ensure_venv; echo rc=$?')"
check "H2 Homebrew python present -> rebuild announced" "$(echo "$out" | grep -c 'built on Apple.s Python and Homebrew')" "1"
check "H2 old venv replaced" "$([ -e "$T/n1/.venv/marker" ] && echo kept || echo gone)" "gone"
check "H2 new venv built by Homebrew's python" "$(grep -c "^builder $T/bp/bin/python3 -m venv" "$T/log")" "1"
check "H2 new venv runs" "$("$T/n1/.venv/bin/python3" -c 'print(1)')" "1"

# H3 (#8 #21): python3 is a dangling symlink -> rebuilt, not left dead
rm -f "$T/n1/.venv/bin/python3"; ln -s /nonexistent/python3 "$T/n1/.venv/bin/python3"
out="$(STUB_BREWPREFIX="$T/nobp" runv 'ensure_venv; echo rc=$?')"
check "H3 dangling python3 -> rebuild announced" "$(echo "$out" | grep -c 'no longer works')" "1"
check "H3 dangling python3 -> venv runs afterwards" "$("$T/n1/.venv/bin/python3" -c 'print(2)' 2>&1)" "2"
# H4 (#7): pyvenv.cfg gone -> rebuilt
/usr/bin/trash "$T/n1/.venv"; /usr/bin/python3 -m venv "$T/n1/.venv"; rm -f "$T/n1/.venv/pyvenv.cfg"
out="$(STUB_BREWPREFIX="$T/nobp" runv 'ensure_venv; echo rc=$?')"
check "H4 missing pyvenv.cfg -> rebuild announced" "$(echo "$out" | grep -c 'no longer works')" "1"
check "H4 missing pyvenv.cfg -> pyvenv.cfg back" "$([ -f "$T/n1/.venv/pyvenv.cfg" ] && echo yes)" "yes"

# H5 (#18): no brew, no pipx, under set -e: the upgrade check returns 0 and the script goes on
out="$(env -i HOME="$T/home" PATH=/usr/bin:/bin bash -c "set -e; source '$T/n2/deps.sh'; maybe_upgrade_ytdlp; echo reached" 2>&1 || true)"
check "H5 no brew no pipx: maybe_upgrade_ytdlp does not end the script" "$(echo "$out" | tail -1)" "reached"

# H6 (#22): brew owns yt-dlp but `brew upgrade` fails: set -e survives; no compile on a macOS without bottles
mkdir -p "$T/ubin"
cat > "$T/ubin/brew" <<'EOF'
#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
case "$1" in
  list) exit 0 ;;
  upgrade) exit 1 ;;
  ruby) shift 3; for a in "$@"; do case " $STUB_NOBOTTLE " in *" $a "*) echo "$a-dep";; esac; done; exit 0 ;;
esac
exit 0
EOF
chmod +x "$T/ubin/brew"
: > "$T/log"; rm -f "$T/n2/.last-ytdlp-upgrade"
out="$(env -i HOME="$T/home" PATH="$T/ubin:/usr/bin:/bin" STUB_LOG="$T/log" STUB_NOBOTTLE="" bash -c "set -e; source '$T/n2/deps.sh'; upgrade_ytdlp; echo reached" 2>&1 || true)"
check "H6 failing brew upgrade does not kill set -e" "$(echo "$out" | tail -1)" "reached"
check "H6 bottled -> brew upgrade attempted" "$(grep -c '^brew upgrade' "$T/log")" "1"
: > "$T/log"
env -i HOME="$T/home" PATH="$T/ubin:/usr/bin:/bin" STUB_LOG="$T/log" STUB_NOBOTTLE="yt-dlp" bash -c "set -e; source '$T/n2/deps.sh'; upgrade_ytdlp" >/dev/null 2>&1 || true
check "H6 no-bottle macOS -> brew upgrade NOT run (would compile)" "$(grep -c '^brew upgrade' "$T/log")" "0"

# H7 (#44): numba repair fails (pip exits 1): ensure_deps keeps going under set -e and says so
mkdir -p "$T/tbin"
for t in yt-dlp ffmpeg gallery-dl exiftool; do printf '#!/bin/sh\nexit 0\n' > "$T/tbin/$t"; chmod +x "$T/tbin/$t"; done
cat > "$T/fakepy2" <<'EOF'
#!/bin/sh
case "$*" in
  *"find_spec('essentia')"*) exit 1 ;;
  *"find_spec('librosa')"*) exit 0 ;;
  *"import numba"*) exit 1 ;;
  *"-m pip"*) exit 1 ;;
esac
exit 0
EOF
chmod +x "$T/fakepy2"
runt() { env -i HOME="$T/home" PATH="$T/tbin:/usr/bin:/bin" STUB_LOG="$T/log" bash -c "set -e; source '$T/n2/deps.sh'; ensure_venv() { :; }; load_brew() { :; }; VENV_PY='$T/fakepy2'; $1" 2>&1; }
printf '#!/bin/sh\nexit 22\n' > "$T/tbin/curl"; chmod +x "$T/tbin/curl"
out="$(runt 'ensure_deps; echo reached' || true)"
check "H7 numba repair failure: ensure_deps survives set -e" "$(echo "$out" | tail -1)" "reached"
check "H7 ...and reports librosa still broken" "$(echo "$out" | grep -c 'librosa still broken')" "1"
# no Homebrew at all (and the Homebrew installer unable to run): said plainly, then on to the report
check "H7 no Homebrew: plain message, continues" "$(echo "$out" | grep -c 'Homebrew could not be installed here')" "1"

# H8 (#47): pipx on Apple's 3.9 warns; the hint works without pipx on PATH
mkdir -p "$T/pvenvs/yt-dlp"; printf 'home = /usr/bin\nversion = 3.9.6\n' > "$T/pvenvs/yt-dlp/pyvenv.cfg"
mkdir -p "$T/pbin"; printf '#!/bin/sh\ncase "$1" in environment) echo "$STUB_VENVS2";; esac\nexit 0\n' > "$T/pbin/pipx"; chmod +x "$T/pbin/pipx"
out="$(env -i HOME="$T/home" PATH="$T/pbin:/usr/bin:/bin" STUB_VENVS2="$T/pvenvs" bash -c "source '$T/n2/deps.sh'; pipx_install yt-dlp; echo rc=\$?" 2>&1)"
check "H8 pipx yt-dlp on Python 3.9 -> frozen-release warning" "$(echo "$out" | grep -c 'yt-dlp was installed on Python 3.9')" "1"
printf 'home = /opt/homebrew/bin\nversion = 3.14.0\n' > "$T/pvenvs/yt-dlp/pyvenv.cfg"
out="$(env -i HOME="$T/home" PATH="$T/pbin:/usr/bin:/bin" STUB_VENVS2="$T/pvenvs" bash -c "source '$T/n2/deps.sh'; pipx_install yt-dlp; echo rc=\$?" 2>&1)"
check "H8 pipx yt-dlp on Python 3.14 -> no warning" "$(echo "$out" | grep -c 'installed on Python')" "0"
check "H8 get-pipx hint is runnable without pipx on PATH" "$(env -i PATH=/usr/bin:/bin bash -c "source '$T/n2/deps.sh'; manual_install_hint yt-dlp" | grep -c 'python3 -m pip install --user pipx && python3 -m pipx install yt-dlp')" "1"

# H9 (#33): install.sh writes valid JSON for an install path containing " $ \
IN="$T/in/ha\"s\$d\\b"; mkdir -p "$IN"
cp "$NATIVE/install.sh" "$NATIVE/deps.sh" "$NATIVE/EXTENSION_ID" "$IN/"; printf '#!/bin/sh\n' > "$IN/jownloader_host.py"
mkdir -p "$T/ihome"
out="$(env -i JOWN_SKIP_BREW_ENV=1 HOME="$T/ihome" PATH="$T/wbin:/usr/bin:/bin" STUB_LOG="$T/log" STUB_BREWPREFIX="$T/nobp" bash -c 'bash "$1/install.sh" --no-deps' _ "$IN" 2>&1 || true)"
MF="$T/ihome/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.jshriver.jownloader.json"
check "H9 manifest for a \" \$ \\ path is valid JSON with the real path" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"] == sys.argv[2] + "/jownloader_host.py")' "$MF" "$IN" 2>&1 | tail -1)" "True"


[ "$fails" = 0 ] && echo "ALL GREEN" || { echo "$fails FAILED"; exit 1; }
