#!/bin/bash
# Jownloader's engine dependencies: check + install. Sourced by install.sh (ensure_deps) and usable on
# its own: `source native/deps.sh && ensure_deps`. Homebrew installs yt-dlp (YouTube), ffmpeg (streams,
# remux, transcode), gallery-dl (Instagram, Twitter/X; yt-dlp injected for 1080p Instagram video) and
# exiftool (strip metadata). The Python side — pypdf, mutagen (song tags) and the BPM/key analyzer (essentia-tensorflow or,
# failing a wheel, librosa) — lives in a private venv, native/.venv, never the system/browser Python:
# Brave launches the host with PATH=/usr/bin:/bin (Xcode CLT python3, no current wheels and no pip
# access under PEP 668), while this script runs under bash with brew loaded (brew's python3, whose
# site-packages the host never sees). One venv both sides agree on removes that split; see
# ensure_venv() and jownloader_host.py's re-exec at the top of the file.

DEPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$DEPS_DIR/.last-ytdlp-upgrade"
UPGRADE_EVERY_DAYS=7
VENV_PY="$DEPS_DIR/.venv/bin/python3"

load_brew() {
    [ -n "$JOWN_SKIP_BREW_ENV" ] && return 0   # tests: keep the machine's real Homebrew out of a stubbed PATH
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -f /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
}

# Creates native/.venv if it doesn't exist yet. Prefers Homebrew's python3 (current wheels for
# essentia/librosa) over the browser-PATH python3 (Xcode CLT, often years old) — either way, once
# built, the venv's own interpreter ($VENV_PY) is what every dep install below uses, and what
# jownloader_host.py re-execs into, so it no longer matters which python3 built it or launched the host.
APPLE_PY_HINT="BPM/key needs Homebrew Python: brew install python, then Install missing dependencies again"

# Is this a path inside Apple's Python (Xcode CLT / Xcode / /usr/bin / the system framework)?
apple_path() {
    case "$1" in
        /Library/Developer/CommandLineTools/*|/Applications/Xcode*|/usr/bin|/usr/bin/*|/System/Library/Frameworks/*) return 0 ;;
    esac
    return 1
}

# True when the venv's base interpreter is Apple's (Xcode CLT / Xcode / /usr/bin): Python 3.9 there.
# Read from the venv's pyvenv.cfg (`home`, `base-executable`) — inside a venv sys._base_executable is the
# venv's own python3 — then from sys.base_prefix, which names the framework the venv was built from.
venv_is_apple_python() {
    local cfg="$DEPS_DIR/.venv/pyvenv.cfg" l
    if [ -f "$cfg" ]; then
        while IFS= read -r l; do
            case "$l" in
                "home = "*|"base-executable = "*) apple_path "${l#*= }" && return 0 ;;
            esac
        done < "$cfg"
    fi
    apple_path "$("$VENV_PY" -c 'import sys; print(sys.base_prefix)' 2>/dev/null)"
}

brew_python_available() {
    local pre
    pre="$(brew --prefix 2>/dev/null)" || return 1
    [ -n "$pre" ] && [ -x "$pre/bin/python3" ]
}

# A venv is usable when it has its pyvenv.cfg (without it Python does not treat the dir as a venv and the
# host would never see the libraries) and its python3 runs (a dangling symlink is not -x).
venv_healthy() {
    [ -f "$DEPS_DIR/.venv/pyvenv.cfg" ] && [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import sys' 2>/dev/null
}

# Moves native/.venv aside (the Trash when there is one); never fatal.
venv_set_aside() {
    local vdir="$DEPS_DIR/.venv"
    /usr/bin/trash "$vdir" 2>/dev/null && return 0
    mv "$vdir" "$DEPS_DIR/.venv-dead-$(date +%Y%m%d-%H%M%S)" 2>/dev/null && return 0
    echo "❌ could not move native/.venv out of the way — delete that folder by hand and rerun"
    return 1
}

ensure_venv() {
    load_brew
    local builder
    if [ -e "$DEPS_DIR/.venv" ] || [ -L "$DEPS_DIR/.venv" ]; then
        if venv_healthy; then
            # A venv built on Apple's Python (Xcode CLT 3.9) while Homebrew's python3 has since appeared, and no
            # BPM/key analyzer in it: rebuild on Homebrew's python (the libraries are reinstalled right after).
            if venv_is_apple_python && brew_python_available && ! have_essentia && ! have_librosa; then
                echo "▸ native/.venv was built on Apple's Python and Homebrew's python3 is now available — rebuilding on it..."
                venv_set_aside || return 0
            else
                return 0
            fi
        else
            # its base interpreter was upgraded or removed (dangling python3 link, brew python@3.x -> 3.y) or
            # pyvenv.cfg is gone: dead, rebuild it
            echo "▸ native/.venv no longer works (its Python was removed or pyvenv.cfg is missing) — rebuilding..."
            venv_set_aside || return 0
        fi
    fi
    if brew_python_available; then builder="$(brew --prefix)/bin/python3"
    else builder="$(command -v python3 2>/dev/null || true)"; fi
    [ -z "$builder" ] && builder="/usr/bin/python3"
    echo "▸ Creating native/.venv ($builder)..."
    if ! "$builder" -m venv --upgrade-deps "$DEPS_DIR/.venv" 2>/dev/null; then
        "$builder" -m venv "$DEPS_DIR/.venv" && "$VENV_PY" -m pip install -U --quiet pip || true
    fi
    [ -x "$VENV_PY" ] || echo "❌ could not create native/.venv with $builder"
    return 0
}

have_librosa() {
    # find_spec avoids importing librosa (slow); we only need to know it's there
    "$VENV_PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('librosa') else 1)" 2>/dev/null
}

have_essentia() {
    # essentia-tensorflow (TempoCNN BPM + HPCP key detector); same find_spec trick
    "$VENV_PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('essentia') else 1)" 2>/dev/null
}

numba_imports() {
    # librosa needs numba, and numba refuses to import when it lags numpy (seen 2026-09-11:
    # numpy 2.5.3 needed numba >= 0.67). ~0.5 s; a full "import librosa.beat" would be ~3 s.
    "$VENV_PY" -c "import numba" 2>/dev/null
}

# Formulae brew would have to COMPILE to install the given ones (themselves, or any runtime dependency
# not already installed, that has no bottle for this Mac's macOS). Empty output = everything is a
# prebuilt download, or brew could not tell (then brew is allowed to try). Source builds are what turned
# one `brew install yt-dlp` into 31 minutes of llvm on a macOS release Homebrew ships no bottles for
# (Tier 3), ending in a deno build that needs full Xcode. Uses Homebrew's own bottle tag for this
# machine (Utils::Bottles.tag) and bottle_specification.tag?, through `brew ruby`
# (https://docs.brew.sh/Manpage#ruby-options-ruby-file). `brew install --force-bottle` only retries a
# bottle for the current or newest macOS; brew has no flag that refuses a source build.
brew_source_builds() {
    command -v brew &>/dev/null || return 0
    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 brew ruby -e '
      ARGV.each do |n|
        begin
          f = Formulary.factory(n)
          ([f] + f.recursive_dependencies.map(&:to_formula)).uniq.each do |x|
            next if x.any_version_installed?
            puts x.full_name unless x.bottle_specification.tag?(Utils::Bottles.tag, no_older_versions: false)
          end
        rescue StandardError
          next
        end
      end' "$@" 2>/dev/null | sort -u | tr '\n' ' '
}

# One line a person can act on, per tool, when it can't be installed for them.
manual_install_hint() {
    case "$1" in
        yt-dlp|gallery-dl) echo "pipx install $1   (no pipx yet: python3 -m pip install --user pipx && python3 -m pipx install $1 — pip puts pipx in ~/Library/Python/3.x/bin, which is not on PATH, so run it as python3 -m pipx)" ;;
        ffmpeg)   echo "download a static build from https://ffmpeg.org/download.html, put ffmpeg and ffprobe in /usr/local/bin" ;;
        exiftool) echo "install the macOS package from https://exiftool.org" ;;
        *)        echo "see the tool's own website" ;;
    esac
}

# Python version (major.minor) of the pipx venv that holds app $1, read from its pyvenv.cfg; empty if unknown.
pipx_app_python() {
    local venvs v
    venvs="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null)"
    for v in "$venvs" "$HOME/Library/Application Support/pipx/venvs" "$HOME/.local/pipx/venvs"; do
        if [ -n "$v" ] && [ -f "$v/$1/pyvenv.cfg" ]; then
            sed -n 's/^version[ ]*=[ ]*\([0-9]*\.[0-9]*\).*/\1/p' "$v/$1/pyvenv.cfg" | head -1
            return 0
        fi
    done
    return 0
}

pipx_install() {
    local pv
    pipx install "$1" || return 1
    [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH"
    # pip stops at the last release that supports an old Python: on Apple's 3.9 yt-dlp freezes months behind
    # the sites, and "present" would hide it
    pv="$(pipx_app_python "$1")"
    case "$pv" in
        3.[0-9]) echo "  ⚠ $1 was installed on Python $pv, which pip no longer updates past an old release — it may stop working as sites change. Install a newer Python (brew install python, or python.org), then: pipx reinstall --python python3 $1" ;;
    esac
    return 0
}

# yt-dlp and gallery-dl are Python packages: pipx first whenever it exists (prebuilt wheels, nothing to
# compile, no Xcode); Homebrew only when it does not. Before any brew install, brew_source_builds says
# whether brew would compile something; if so brew is skipped with a one-line message. Never dies under
# `set -e`: a tool that stays missing is reported, and the host's own checks show it in the popup.
install_tool() {
    local tool="$1" py=0 nb
    { [ "$tool" = yt-dlp ] || [ "$tool" = gallery-dl ]; } && py=1
    if [ "$py" = 1 ] && command -v pipx &>/dev/null; then
        if pipx_install "$tool"; then return 0; fi
        echo "  pipx could not install $tool — trying brew..."
    fi
    if command -v brew &>/dev/null; then
        nb="$(brew_source_builds "$tool")"
        if [ -n "$nb" ]; then
            # no pipx and brew would compile: pipx itself is the way in, when it is a prebuilt download
            if [ "$py" = 1 ] && ! command -v pipx &>/dev/null && [ -z "$(brew_source_builds pipx)" ]; then
                echo "  no prebuilt $tool for this macOS — installing pipx (prebuilt) to get it..."
                if brew install pipx && pipx_install "$tool"; then return 0; fi
            fi
            echo "  no prebuilt package for this macOS — install $tool by hand: $(manual_install_hint "$tool")"
            return 0
        fi
        if brew install "$tool"; then return 0; fi
    fi
    echo "  could not install $tool — install it by hand: $(manual_install_hint "$tool")"
    return 0
}

# Is the tool there the way the host will look for it? Same search path as engine.py: the browser's bare
# PATH plus Homebrew, /usr/local/bin and ~/.local/bin (pipx).
tool_present() {
    PATH="${JOWN_TOOL_PATHS-/opt/homebrew/bin:/usr/local/bin}:$HOME/.local/bin:$PATH" command -v "$1" &>/dev/null
}

# After installing: what is really there, and how to get what is not. Always returns 0 (the tools are
# optional); install.sh decides separately whether the host itself can run.
engine_tools_report() {
    local have="" miss="" t
    for t in yt-dlp ffmpeg gallery-dl exiftool; do
        if tool_present "$t"; then have="${have:+$have, }$t"; else miss="$miss $t"; fi
    done
    [ -n "$have" ] && echo "engine tools present: $have"
    if [ -n "$miss" ]; then
        echo "engine tools MISSING (what needs them will not work):"
        for t in $miss; do echo "  $t — $(manual_install_hint "$t")"; done
    fi
    return 0
}

# The python that actually runs gallery-dl: its shebang when that is a real interpreter; a pipx shim whose
# shebang is /bin/sh resolves to the gallery-dl venv's python (pipx's own venv list, else the default paths).
gallery_dl_interp() {
    local gdl interp venvs v
    gdl="$(command -v gallery-dl)" || return 0
    interp="$(head -1 "$gdl" 2>/dev/null | sed 's|^#!||' | awk '{print $1}')"
    case "$interp" in
        /bin/sh|/bin/bash|/usr/bin/env|"")
            venvs=""
            command -v pipx &>/dev/null && venvs="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null)"
            for v in "$venvs" "$HOME/Library/Application Support/pipx/venvs" "$HOME/.local/pipx/venvs"; do
                if [ -n "$v" ] && [ -x "$v/gallery-dl/bin/python" ]; then echo "$v/gallery-dl/bin/python"; return 0; fi
            done
            return 0 ;;
    esac
    echo "$interp"
}

# Installs whatever is missing. Prints nothing when everything is present.
ensure_deps() {
    ensure_venv
    load_brew

    if ! command -v brew &>/dev/null; then
        echo "▸ Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || true
        load_brew
        command -v brew &>/dev/null || echo "  Homebrew could not be installed here (it needs an admin account) — continuing with pipx / by hand; the list at the end says what is missing and how to get it"
    fi

    # pipx puts its apps (gallery-dl on some Macs) in ~/.local/bin, which the browser's PATH lacks —
    # look there BEFORE deciding a tool is missing, or brew installs a second copy.
    [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH"
    # `pip install --user pipx` on Apple's Python lands in ~/Library/Python/3.x/bin, which no PATH has
    local ub
    for ub in "$HOME"/Library/Python/3.*/bin; do
        [ -x "$ub/pipx" ] && ! command -v pipx &>/dev/null && export PATH="$ub:$PATH"
    done
    for tool in yt-dlp ffmpeg gallery-dl exiftool; do
        if ! command -v "$tool" &>/dev/null; then
            echo "▸ Installing $tool..."
            install_tool "$tool"
        fi
    done

    if ! "$VENV_PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pypdf') else 1)" 2>/dev/null; then
        echo "▸ Installing pypdf (PDF merge for Whole-site saves)..."
        "$VENV_PY" -m pip install --prefer-binary --quiet pypdf 2>/dev/null \
            || echo "  (pip failed — pypdf not installed; whole-site PDF pages will be kept separately)"
    fi

    # mutagen writes the tags (title, artist, album, number, cover) into Spotify-link songs
    if ! "$VENV_PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('mutagen') else 1)" 2>/dev/null; then
        echo "▸ Installing mutagen (song tags for Spotify links)..."
        "$VENV_PY" -m pip install --prefer-binary --quiet mutagen 2>/dev/null \
            || echo "  (pip failed — mutagen not installed; Spotify-link songs will be saved untagged)"
    fi

    # Instagram's best-quality video comes through a DASH manifest that gallery-dl hands to yt-dlp by
    # IMPORT, not by running the binary; without it videos still download, at the lower 720p copy.
    local interp
    interp="$(gallery_dl_interp)"
    if [ -n "$interp" ] && [ -x "$interp" ] && ! "$interp" -c "import yt_dlp" &>/dev/null; then
        echo "▸ Adding yt-dlp to gallery-dl for best-quality Instagram video..."
        if command -v pipx &>/dev/null && pipx list 2>/dev/null | grep -q gallery-dl; then
            pipx inject gallery-dl yt-dlp || echo "  (skipped — Instagram videos download at 720p)"
        else
            "$interp" -m pip install --quiet yt-dlp 2>/dev/null || echo "  (skipped — Instagram videos download at 720p)"
        fi
    fi

    # One analyzer, picked automatically. bpm.py uses whichever is installed:
    #   essentia-tensorflow (best; needs a wheel for this Mac's macOS/CPU/Python)
    #   librosa             (fallback; installs almost everywhere)
    if ! have_essentia && ! have_librosa; then
        echo "▸ Installing the BPM/key analyzer..."
        if "$VENV_PY" -m pip install --prefer-binary --quiet essentia-tensorflow >/dev/null 2>&1 \
           && "$VENV_PY" -c "import essentia.standard" >/dev/null 2>&1; then
            echo "  ✓ essentia (TempoCNN + HPCP)"
        else
            "$VENV_PY" -m pip uninstall -y essentia-tensorflow >/dev/null 2>&1 || true
            echo "  no essentia build for this Mac — installing librosa instead"
            # --prefer-binary avoids compiling llvmlite from source
            "$VENV_PY" -m pip install --prefer-binary --quiet librosa >/dev/null 2>&1 || true
            if have_librosa; then
                echo "  ✓ librosa"
            elif venv_is_apple_python; then
                echo "❌ $APPLE_PY_HINT. Downloads still work; no BPM/key tags until then."
            else
                echo "❌ Neither analyzer installed. Downloads still work; no BPM/key tags."
                echo "   Try:  brew install llvm   then run native/install.sh again."
            fi
        fi
    fi

    # librosa's numba refuses to import when it lags numpy (seen 2026-09-11)
    if ! have_essentia && have_librosa && ! numba_imports; then
        echo "▸ Repairing librosa (numba/numpy mismatch)..."
        "$VENV_PY" -m pip install -U --prefer-binary --quiet numba >/dev/null 2>&1 || true
        if ! numba_imports; then
            if venv_is_apple_python; then echo "❌ $APPLE_PY_HINT."
            else echo "❌ librosa still broken; no BPM/key tags until fixed."; fi
        fi
    fi

    chmod +x "$DEPS_DIR"/jownloader_host.py 2>/dev/null || true
}

# Upgrades the two deps that go stale as sites change (yt-dlp, gallery-dl) and stamps the time.
# gallery-dl may be a brew formula or a pipx install depending on how it landed on this Mac (pipx when
# brew had no wheel at install time) — brew-upgrading a name brew never installed exits non-zero and,
# under install.sh's `set -e`, kills the whole install; only upgrade through brew what brew actually owns.
upgrade_ytdlp() {
    echo "▸ Updating yt-dlp and gallery-dl..."
    local brew_owned=()
    for t in yt-dlp gallery-dl; do brew list --formula "$t" &>/dev/null && brew_owned+=("$t"); done
    # never kills the caller (install.sh runs under set -e), and never compiles on a macOS without bottles
    if [ "${#brew_owned[@]}" -gt 0 ]; then
        if [ -n "$(brew_source_builds "${brew_owned[@]}")" ]; then
            echo "  (brew has no prebuilt update for this macOS — not compiling; update yt-dlp / gallery-dl by hand)"
        else
            brew upgrade "${brew_owned[@]}" >/dev/null 2>&1 || echo "  (brew upgrade failed — continuing)"
        fi
    fi
    if command -v pipx &>/dev/null; then
        pipx upgrade yt-dlp >/dev/null 2>&1 || true
        pipx upgrade --include-injected gallery-dl >/dev/null 2>&1 || true
    fi
    touch "$STAMP"
}

# Upgrade if the stamp is missing or older than UPGRADE_EVERY_DAYS.
maybe_upgrade_ytdlp() {
    command -v brew &>/dev/null || command -v pipx &>/dev/null || return 0
    if [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mtime +"$UPGRADE_EVERY_DAYS" 2>/dev/null)" ]; then
        upgrade_ytdlp
    fi
}
