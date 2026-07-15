#!/usr/bin/env bash
# Record the built-in 30-second KGraph Explorer demo on macOS.
#
# The demo captions are rendered by GraphView itself, so no post-production
# subtitle tool is needed.  This script intentionally runs the repository venv
# instead of the global `kgraph` shim: the latter is a Linux-x64 package.
#
# Usage:
#   scripts/record-graph-demo.sh [output-directory]
#
# Optional environment:
#   KGRAPH_DB, KGRAPH_ROOT, KGRAPH_DEMO_PORT, KGRAPH_PYTHON

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${KGRAPH_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
KGRAPH_ROOT="${KGRAPH_ROOT:-$PROJECT_ROOT/../linux}"
KGRAPH_DB="${KGRAPH_DB:-$KGRAPH_ROOT/.kgraph/kgraph.db}"
PORT="${KGRAPH_DEMO_PORT:-8787}"
OUT_DIR="${1:-$HOME/Movies/kgraph-demo}"
OUTPUT_MOV="$OUT_DIR/kgraph-code-graph-30s.mov"
LOG="$OUT_DIR/kgraph-view.log"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: this recording helper uses macOS screencapture." >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: KGraph Python runtime not found: $PYTHON" >&2
  exit 1
fi
if [[ ! -f "$KGRAPH_DB" || ! -d "$KGRAPH_ROOT" ]]; then
  echo "ERROR: set KGRAPH_DB and KGRAPH_ROOT to a real indexed kernel tree." >&2
  exit 1
fi
for tool in curl open screencapture; do
  command -v "$tool" >/dev/null || { echo "ERROR: missing macOS tool: $tool" >&2; exit 1; }
done

mkdir -p "$OUT_DIR"
"$PYTHON" "$PROJECT_ROOT/view/server.py" \
  --db "$KGRAPH_DB" --root "$KGRAPH_ROOT" --port "$PORT" --no-browser \
  >"$LOG" 2>&1 &
VIEW_PID=$!
cleanup() {
  kill "$VIEW_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..40}; do
  if curl --fail --silent "http://127.0.0.1:$PORT/api/status" >/dev/null; then
    break
  fi
  sleep 0.1
done
if ! curl --fail --silent "http://127.0.0.1:$PORT/api/status" >/dev/null; then
  echo "ERROR: KGraph View did not start. See $LOG" >&2
  exit 1
fi

URL="http://127.0.0.1:$PORT/graph.html?demo=video"
open "$URL"
echo
echo "A browser opened at: $URL"
echo "1. Put that browser on the primary display and frame it at 16:9."
echo "2. Press Return to open the macOS recorder."
echo "3. Start recording, then immediately click 'Start 30s demo' in GraphView."
echo "   The browser provides Chinese/English captions; no microphone is recorded."
read -r -p "Ready to record? "

screencapture -v -V30 -D1 -k -x "$OUTPUT_MOV"

echo "Recorded: $OUTPUT_MOV"
