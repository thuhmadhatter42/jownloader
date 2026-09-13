#!/bin/bash
# Jownloader's engine dependencies: check + install. Sourced by install.sh (ensure_deps) and usable on
# its own: `source native/deps.sh && ensure_deps`. Homebrew installs yt-dlp (YouTube), ffmpeg (streams,
# remux, transcode), gallery-dl (Instagram, Twitter/X; yt-dlp injected for 1080p Instagram video) and
# exiftool (strip metadata); the BPM/key analyzer is essentia-tensorflow or, failing a wheel, librosa.

DEPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$DEPS_DIR/.last-ytdlp-upgrade"
UPGRADE_EVERY_DAYS=7

load_brew() {
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -f /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
}

have_librosa() {
    # find_spec avoids importing librosa (slow); we only need to know it's there
    python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('librosa') else 1)" 2>/dev/null
}

have_essentia() {
    # essentia-tensorflow (TempoCNN BPM + HPCP key detector); same find_spec trick
    python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('essentia') else 1)" 2>/dev/null
}

numba_imports() {
    # librosa needs numba, and numba refuses to import when it lags numpy (seen 2026-09-11:
    # numpy 2.5.3 needed numba >= 0.67). ~0.5 s; a full "import librosa.beat" would be ~3 s.
    python3 -c "import numba" 2>/dev/null
}

# Installs whatever is missing. Prints nothing when everything is present.
ensure_deps() {
    load_brew

    if ! command -v brew &>/dev/null; then
        echo "▸ Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        load_brew
    fi

    for tool in yt-dlp ffmpeg gallery-dl exiftool; do
        if ! command -v "$tool" &>/dev/null; then
            echo "▸ Installing $tool..."
            brew install "$tool"
        fi
    done
    [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH"

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
        if pip3 install essentia-tensorflow --break-system-packages --prefer-binary >/dev/null 2>&1 \
           && python3 -c "import essentia.standard" >/dev/null 2>&1; then
            echo "  ✓ essentia (TempoCNN + HPCP)"
        else
            pip3 uninstall -y essentia-tensorflow >/dev/null 2>&1
            echo "  no essentia build for this Mac — installing librosa instead"
            # --prefer-binary avoids compiling llvmlite from source
            pip3 install librosa --break-system-packages --prefer-binary >/dev/null 2>&1
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
        pip3 install -U numba --break-system-packages --prefer-binary >/dev/null 2>&1
        numba_imports || echo "❌ librosa still broken; no BPM/key tags until fixed."
    fi

    chmod +x "$DEPS_DIR"/jownloader_host.py 2>/dev/null
}

# Upgrades the two deps that go stale as sites change (yt-dlp, gallery-dl) and stamps the time.
upgrade_ytdlp() {
    echo "▸ Updating yt-dlp and gallery-dl..."
    brew upgrade yt-dlp gallery-dl >/dev/null 2>&1
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
