#!/bin/bash
# Local CI — the gate for commit/push/release. Run: ./ci.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

fail=0
run() {
  local name="$1"; shift
  printf '=== %s ... ' "$name"
  local log; log="$(mktemp)"
  if "$@" >"$log" 2>&1; then
    echo "PASS"
  else
    echo "FAIL"; cat "$log"; fail=1
  fi
  rm -f "$log"
}

echo "--- Local CI ---"

# Swift: real build of the timelapse recorder (same flags as run_timelapse.sh)
run "build timelapse" swiftc timelapse.swift -o timelapse \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist
run "typecheck timelapse_security" swiftc -typecheck timelapse_security.swift
run "typecheck clock" swiftc -typecheck clock.swift

# Info.plist
run "lint Info.plist" plutil -lint Info.plist

# Shell scripts: syntax only
for f in *.sh; do run "bash -n $f" bash -n "$f"; done

# Python: compile without executing
for f in *.py; do run "py_compile $f" python3 -I -m py_compile "$f"; done

rm -rf __pycache__

if [ "$fail" -eq 0 ]; then
  echo "--- ALL CHECKS PASSED ---"
else
  echo "--- CI FAILED ---"
fi
exit "$fail"
