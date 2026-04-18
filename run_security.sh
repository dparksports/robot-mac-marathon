#!/bin/bash

# Security Timelapse - Audio-triggered recording with periodic snapshots
# Optimized for long battery life on MacBook Air

SWIFT_SCRIPT="timelapse_security.swift"
BINARY="timelapse_security"
INFO_PLIST="Info.plist"

# Compile with embedded Info.plist for proper camera/mic permission prompts
echo "--- Compiling $SWIFT_SCRIPT ---"
swiftc "$SWIFT_SCRIPT" -o "$BINARY" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$INFO_PLIST" \
  -framework AVFoundation \
  -framework CoreGraphics \
  -framework IOKit \
  -framework VideoToolbox \
  -O

if [ $? -ne 0 ]; then
    echo "Compilation failed. Exiting."
    exit 1
fi

echo "--- Compiled successfully ---"

# Prevent system sleep (but allow display sleep) using caffeinate
# -i = prevent idle sleep
# -s = prevent system sleep on AC power
# The & runs caffeinate in background; we kill it on exit
caffeinate -i -s -w $$ &
CAFFEINATE_PID=$!
trap "kill $CAFFEINATE_PID 2>/dev/null" EXIT

# Pass all arguments through to the binary
echo "Starting security timelapse. Press [Ctrl+C] to exit completely."

while true; do
    echo "--- Launching $BINARY ---"
    ./"$BINARY" "$@"
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "--- Clean shutdown (exit 0). Done. ---"
        break
    fi

    echo "--- Process crashed (exit $EXIT_CODE). Restarting in 3 seconds... ---"
    sleep 3
done
