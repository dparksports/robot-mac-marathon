#!/bin/bash

# Robot Mac Marathon - FFmpeg Installer Script
# Downloads and extracts static macOS binaries for ffmpeg, ffprobe, and ffplay
# without requiring Homebrew. Files are installed locally in the project directory.

set -e

echo "======================================================="
echo "   FFmpeg Locally Auto-Installer (macOS Apple Silicon) "
echo "======================================================="
echo ""

# The directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR"

echo "[*] Destination folder: $BIN_DIR"
echo ""

download_binary() {
    local name=$1
    local url=$2
    local zip_file="/tmp/${name}.zip"

    echo "[-] Downloading $name..."
    curl -sL "$url" -o "$zip_file"

    echo "[-] Extracting $name..."
    # The evermeet zip contains the binary at the root
    unzip -q -o "$zip_file" -d "$BIN_DIR"
    
    # Ensure it's executable
    chmod +x "$BIN_DIR/$name"
    
    # Cleanup zip
    rm -f "$zip_file"
    
    # Clear the Apple quarantine flag so macOS doesn't block it from running silently
    xattr -d com.apple.quarantine "$BIN_DIR/$name" 2>/dev/null || true
    
    echo "[✓] $name installed successfully!"
}

# Download latest stable ffmpeg, ffprobe, and ffplay from evermeet (macOS static builds)
download_binary "ffmpeg"  "https://evermeet.cx/ffmpeg/getrelease/zip"
download_binary "ffprobe" "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"
download_binary "ffplay"  "https://evermeet.cx/ffmpeg/getrelease/ffplay/zip"

echo ""
echo "======================================================="
echo "All utilities successfully installed in this folder!"
echo "Test versions:"
echo "-------------------------------------------------------"
"$BIN_DIR/ffmpeg" -version | head -n 1
"$BIN_DIR/ffprobe" -version | head -n 1
echo "======================================================="
