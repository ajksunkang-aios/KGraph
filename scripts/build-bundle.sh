#!/bin/bash
# KGraph — Build self-contained Linux x86-64 platform bundle.
#
# Produces a tarball containing:
#   - Standalone Python 3.10 runtime
#   - KGraph source + dependencies (protobuf, mcp)
#   - scip-clang binary
#   - Launcher script
#
# Usage: bash scripts/build-bundle.sh [VERSION]
#
# Requirements: runs on ubuntu-latest, needs curl + tar.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="${1:-0.1.0}"

# python-build-standalone: https://github.com/astral-sh/python-build-standalone
# Use the latest release with cpython-3.10 + x86_64-unknown-linux-gnu + install_only_stripped
PYTHON_STANDALONE_RELEASE="20260610"
PYTHON_STANDALONE_FILE="cpython-3.10.20+20260610-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PYTHON_STANDALONE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_STANDALONE_RELEASE}/${PYTHON_STANDALONE_FILE}"

BUNDLE_NAME="kgraph-linux-x64"
RELEASE_DIR="${PROJECT_ROOT}/release"
BUNDLE_DIR="${RELEASE_DIR}/${BUNDLE_NAME}"

echo "=== Building KGraph ${VERSION} bundle ==="
echo "Bundle dir: ${BUNDLE_DIR}"

# ── Clean ──
rm -rf "${BUNDLE_DIR}"
mkdir -p "${BUNDLE_DIR}"/{bin,lib/kgraph,lib/site-packages}

# ── 1. Download standalone Python 3.10 ──
echo "[1/5] Downloading Python 3.10 standalone..."
curl -fsSL "${PYTHON_STANDALONE_URL}" \
  | tar xz --strip-components=1 -C "${BUNDLE_DIR}"
# Rename python3 → python for consistent reference
if [ -f "${BUNDLE_DIR}/bin/python3" ]; then
  cp "${BUNDLE_DIR}/bin/python3" "${BUNDLE_DIR}/python"
elif [ -f "${BUNDLE_DIR}/bin/python3.10" ]; then
  cp "${BUNDLE_DIR}/bin/python3.10" "${BUNDLE_DIR}/python"
fi
chmod +x "${BUNDLE_DIR}/python"

# ── 2. Install Python dependencies ──
echo "[2/5] Installing Python dependencies..."
"${BUNDLE_DIR}/python" -m pip install --target "${BUNDLE_DIR}/lib/site-packages" \
  --no-cache-dir --no-compile \
  -r "${PROJECT_ROOT}/requirements.txt"

# ── 3. Copy KGraph source ──
echo "[3/5] Copying KGraph source..."
cp -r "${PROJECT_ROOT}/src"        "${BUNDLE_DIR}/lib/kgraph/"
cp -r "${PROJECT_ROOT}/mcp"        "${BUNDLE_DIR}/lib/kgraph/"
cp -r "${PROJECT_ROOT}/scripts"    "${BUNDLE_DIR}/lib/kgraph/"
cp -r "${PROJECT_ROOT}/thirdparty" "${BUNDLE_DIR}/lib/kgraph/"
cp -r "${PROJECT_ROOT}/view"       "${BUNDLE_DIR}/lib/kgraph/"
cp -r "${PROJECT_ROOT}/graphview"  "${BUNDLE_DIR}/lib/kgraph/"

# Clean __pycache__ from bundle
find "${BUNDLE_DIR}" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ── 4. Generate launcher scripts ──
echo "[4/5] Generating launcher scripts..."

# kgraph-launcher: the main entry point used by npm-shim
cat > "${BUNDLE_DIR}/bin/kgraph-launcher" << 'LAUNCHER_EOF'
#!/bin/bash
# KGraph launcher — sets up PYTHONPATH and delegates to the right module.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$DIR/python"

export PYTHONPATH="$DIR/lib/kgraph/src:$DIR/lib/kgraph/scripts:$DIR/lib/site-packages"
export PATH="$DIR/bin:$PATH"

# Default: run MCP server (what npm-shim invokes)
# Subcommands are routed by the CLI module
CMD="${1:-serve}"
shift || true

case "$CMD" in
  install|detect|uninstall)
    # Agent configuration commands → installer CLI
    exec "$PYTHON" "$DIR/lib/kgraph/src/installer/cli.py" "$CMD" "$@"
    ;;
  serve)
    # MCP stdio server (used by AI agents via MCP protocol)
    exec "$PYTHON" "$DIR/lib/kgraph/mcp/server.py" "$@"
    ;;
  init)
    # Build code graph: scip-clang -> parse index.scip -> SQLite
    exec "$PYTHON" "$DIR/lib/kgraph/src/cli/init_cmd.py" "$@"
    ;;
  view)
    # Local interactive code-graph explorer (HTTP API + browser UI)
    exec "$PYTHON" "$DIR/lib/kgraph/view/server.py" "$@"
    ;;
  sync)
    # Incrementally refresh the graph after a build (lazy-indexing)
    exec "$PYTHON" "$DIR/lib/kgraph/src/cli/sync_cmd.py" "$@"
    ;;
  ingest|status)
    # Reserved for future index lifecycle commands
    echo "kgraph: '$CMD' is not yet implemented. See https://github.com/ajksunkang-aios/KGraph/issues"
    exit 1
    ;;
  *)
    echo "kgraph: unknown command '$CMD'"
    echo "Usage: kgraph {install|detect|uninstall|init|view|serve|sync} [options]"
    exit 1
    ;;
esac
LAUNCHER_EOF
chmod +x "${BUNDLE_DIR}/bin/kgraph-launcher"

# ── 5. Copy scip-clang (if available) ──
echo "[5/5] Copying scip-clang..."
if [ -f "${PROJECT_ROOT}/thirdparty/scip-clang" ]; then
  cp "${PROJECT_ROOT}/thirdparty/scip-clang" "${BUNDLE_DIR}/bin/"
  chmod +x "${BUNDLE_DIR}/bin/scip-clang"
  echo "  scip-clang bundled."
else
  echo "  WARNING: thirdparty/scip-clang not found — bundle won't include it."
  echo "  Users will need to install scip-clang separately."
fi

# ── Tarball ──
echo "=== Packaging ==="
cd "${RELEASE_DIR}"
tar czf "${BUNDLE_NAME}-${VERSION}.tar.gz" "${BUNDLE_NAME}"

SIZE=$(du -sh "${BUNDLE_NAME}-${VERSION}.tar.gz" | cut -f1)
echo "=== Done: release/${BUNDLE_NAME}-${VERSION}.tar.gz (${SIZE}) ==="
