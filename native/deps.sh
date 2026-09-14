#!/bin/bash
# Jownloader's engine dependencies: check + install. Sourced by install.sh (ensure_deps) and usable on
# its own: `source native/deps.sh && ensure_deps`. Homebrew installs yt-dlp (YouTube), ffmpeg (streams,
# remux, transcode), gallery-dl (Instagram, Twitter/X; yt-dlp injected for 1080p Instagram video) and
# exiftool (strip metadata). The Python side — pypdf and the BPM/key analyzer (essentia-tensorflow or,
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
ensure_venv() {
    load_brew
    if [ -x "$VENV_PY" ]; then
        "$VENV_PY" -c 'import sys' 2>/dev/null && return 0
        # a venv whose base interpreter was upgraded away (brew python@3.x -> 3.y) is dead: rebuild it
        echo "▸ native/.venv no longer runs (its Python was removed) — rebuilding..."
        [ -n "$DEPS_DIR" ] && mv "$DEPS_DIR/.venv" "$DEPS_DIR/.venv-dead-$(date +%Y%m%d-%H%M%S)"
    fi
    local builder
    builder="$(command -v python3 2>/dev/null || true)"
    [ -z "$builder" ] && builder="/usr/bin/python3"
    echo "▸ Creating native/.venv ($builder)..."
    if ! "$builder" -m venv --upgrade-deps "$DEPS_DIR/.venv" 2>/dev/null; then
        "$builder" -m venv "$DEPS_DIR/.venv" && "$VENV_PY" -m pip install -U --quiet pip
    fi
    [ -x "$VENV_PY" ] || echo "❌ could not create native/.venv with $builder"
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

# Installs whatever is missing. Prints nothing when everything is present.
ensure_deps() {
    ensure_venv
    load_brew

    if ! command -v brew &>/dev/null; then
        echo "▸ Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        load_brew
    fi

    # pipx puts its apps (gallery-dl on some Macs) in ~/.local/bin, which the browser's PATH lacks —
    # look there BEFORE deciding a tool is missing, or brew installs a second copy.
    [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH"
    for tool in yt-dlp ffmpeg gallery-dl exiftool; do
        if ! command -v "$tool" &>/dev/null; then
            echo "▸ Installing $tool..."
            brew install "$tool"
        fi
    done

    if ! "$VENV_PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pypdf') else 1)" 2>/dev/null; then
        echo "▸ Installing pypdf (PDF merge for Whole-site saves)..."
        "$VENV_PY" -m pip install --prefer-binary --quiet pypdf 2>/dev/null \
            || echo "  (pip failed — pypdf not installed; whole-site PDF pages will be kept separately)"
    fi

    # Instagram's best-quality video comes through a DASH manifest that gallery-dl hands to yt-dlp by
    # IMPORT, not by running the binary; without it videos still download, at the lower 720p copy.
    local gdl interp
    gdl="$(command -v gallery-dl)"
    interp="$(head -1 "$gdl" 2>/dev/null | sed 's|^#!||' | awk '{print $1}')"
    if [ -n "$interp" ] && [ -x "$interp" ] && ! "$interp" -c "import yt_dlp" &>/dev/null; then
        echo "▸ Adding yt-dlp to gallery-dl for best-quality Instagram video..."
        if command -v pipx &>/dev/null && pipx list 2>/dev/null | grep -q gallery-dl; then
            pipx inject gallery-dl yt-dlp
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
            "$VENV_PY" -m pip uninstall -y essentia-tensorflow >/dev/null 2>&1
            echo "  no essentia build for this Mac — installing librosa instead"
            # --prefer-binary avoids compiling llvmlite from source
            "$VENV_PY" -m pip install --prefer-binary --quiet librosa >/dev/null 2>&1
            if have_librosa; then
                echo "  ✓ librosa"
            else
                echo "❌ Neither analyzer installed. Downloads still work; no BPM/key tags."
                echo "   Try:  brew install llvm   then run native/install.sh again."
            fi
        fi
    fi

    # librosa's numba refuses to import when it lags numpy (seen 2026-09-11)
    if ! have_essentia && have_librosa && ! numba_imports; then
        echo "▸ Repairing librosa (numba/numpy mismatch)..."
        "$VENV_PY" -m pip install -U --prefer-binary --quiet numba >/dev/null 2>&1
        numba_imports || echo "❌ librosa still broken; no BPM/key tags until fixed."
    fi

    chmod +x "$DEPS_DIR"/jownloader_host.py 2>/dev/null
}

# Upgrades the two deps that go stale as sites change (yt-dlp, gallery-dl) and stamps the time.
# gallery-dl may be a brew formula or a pipx install depending on how it landed on this Mac (pipx when
# brew had no wheel at install time) — brew-upgrading a name brew never installed exits non-zero and,
# under install.sh's `set -e`, kills the whole install; only upgrade through brew what brew actually owns.
upgrade_ytdlp() {
    echo "▸ Updating yt-dlp and gallery-dl..."
    local brew_owned=()
    for t in yt-dlp gallery-dl; do brew list --formula "$t" &>/dev/null && brew_owned+=("$t"); done
    [ "${#brew_owned[@]}" -gt 0 ] && brew upgrade "${brew_owned[@]}" >/dev/null 2>&1
    command -v pipx &>/dev/null && pipx upgrade --include-injected gallery-dl >/dev/null 2>&1
    touch "$STAMP"
}

# Upgrade if the stamp is missing or older than UPGRADE_EVERY_DAYS.
maybe_upgrade_ytdlp() {
    command -v brew &>/dev/null || return
    if [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mtime +"$UPGRADE_EVERY_DAYS" 2>/dev/null)" ]; then
        upgrade_ytdlp
    fi
}
