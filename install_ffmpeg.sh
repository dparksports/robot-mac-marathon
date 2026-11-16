#!/bin/bash

# Robot Mac Marathon - FFmpeg Installer Script
# Extracts bundled static macOS binaries for ffmpeg, ffprobe, and ffplay
# from the local zip file included in the repository.

set -e

echo "======================================================="
echo "   FFmpeg Locally Auto-Installer (macOS Apple Silicon) "
echo "======================================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR"
ZIP_FILE="$SCRIPT_DIR/ffmpeg-mac.zip"

if [ ! -f "$ZIP_FILE" ]; then
    echo "[!] Error: Bundled $ZIP_FILE was not found in the repository."
    exit 1
fi

echo "[-] Extracting local binaries from ffmpeg-mac.zip..."
unzip -q -o "$ZIP_FILE" -d "$BIN_DIR"

echo "[-] Configuring execution permissions..."
for binary in ffmpeg ffprobe ffplay; do
    if [ -f "$BIN_DIR/$binary" ]; then
        chmod +x "$BIN_DIR/$binary"
        # Clear the Apple quarantine flag so macOS doesn't block it from running silently
        xattr -d com.apple.quarantine "$BIN_DIR/$binary" 2>/dev/null || true
    fi
done

echo ""
echo "======================================================="
echo "All utilities successfully installed in this folder!"
echo "Test versions:"
echo "-------------------------------------------------------"
"$BIN_DIR/ffmpeg" -version | head -n 1
"$BIN_DIR/ffprobe" -version | head -n 1
echo "======================================================="
