#!/bin/bash

# Name of your swift script
SWIFT_SCRIPT="record.swift"
BINARY="record"
INFO_PLIST="Info.plist"

# Compile with embedded Info.plist for proper camera/mic permission prompts
echo "--- Compiling $SWIFT_SCRIPT ---"
swiftc "$SWIFT_SCRIPT" -o "$BINARY" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$INFO_PLIST"

if [ $? -ne 0 ]; then
    echo "Compilation failed. Exiting."
    exit 1
fi

echo "Starting recording loop. Press [Ctrl+C] to exit the loop completely."

while true; do
    echo "--- Launching $BINARY ---"

    # Run the compiled binary
    ./"$BINARY"

    # Wait for 3 seconds before the next iteration
    echo "--- Script terminated. Restarting in 3 seconds... ---"
    sleep 3
done
