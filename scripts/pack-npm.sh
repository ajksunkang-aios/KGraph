#!/bin/bash
# KGraph — Generate npm publish structure.
#
# Reads the bundle built by build-bundle.sh and produces:
#   release/npm/main/        — @ajksunkang-aios/kgraph (shim package)
#   release/npm/linux-x64/   — @ajksunkang-aios/kgraph-linux-x64 (platform package)
#
# Usage: bash scripts/pack-npm.sh VERSION
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VERSION="${1:?Usage: pack-npm.sh VERSION}"
SCOPE="@ajksunkang-aios"

BUNDLE_DIR="${PROJECT_ROOT}/release/kgraph-linux-x64"
NPM_DIR="${PROJECT_ROOT}/release/npm"

echo "=== Packing npm ${VERSION} ==="

# ── Validate bundle exists ──
if [ ! -d "${BUNDLE_DIR}" ]; then
  echo "ERROR: Bundle not found at ${BUNDLE_DIR}"
  echo "Run build-bundle.sh first."
  exit 1
fi

# ── Clean ──
rm -rf "${NPM_DIR}"
mkdir -p "${NPM_DIR}/main" "${NPM_DIR}/linux-x64"

# ── Platform package: kgraph-linux-x64 ──
echo "[1/2] Packing platform package ${SCOPE}/kgraph-linux-x64..."

cp -r "${BUNDLE_DIR}"/* "${NPM_DIR}/linux-x64/"

cat > "${NPM_DIR}/linux-x64/package.json" << EOF
{
  "name": "${SCOPE}/kgraph-linux-x64",
  "version": "${VERSION}",
  "description": "KGraph platform bundle — Linux x86-64",
  "os": ["linux"],
  "cpu": ["x64"],
  "files": ["python", "lib", "bin"],
  "license": "MIT"
}
EOF

# ── Main package: kgraph (shim) ──
echo "[2/2] Packing main package ${SCOPE}/kgraph..."

cp "${PROJECT_ROOT}/npm-shim.js" "${NPM_DIR}/main/"
cp "${PROJECT_ROOT}/README.md"   "${NPM_DIR}/main/" 2>/dev/null || true

cat > "${NPM_DIR}/main/package.json" << EOF
{
  "name": "${SCOPE}/kgraph",
  "version": "${VERSION}",
  "description": "Compiler-aware kernel code graph engine — MCP tool service",
  "bin": {
    "kgraph": "npm-shim.js"
  },
  "files": [
    "npm-shim.js",
    "README.md"
  ],
  "optionalDependencies": {
    "${SCOPE}/kgraph-linux-x64": "${VERSION}"
  },
  "engines": {
    "node": ">=18"
  },
  "license": "MIT",
  "repository": {
    "type": "git",
    "url": "https://github.com/ajksunkang/KGraph.git"
  },
  "homepage": "https://github.com/ajksunkang/KGraph#readme"
}
EOF

echo "=== npm packages ready ==="
echo "  main:      ${NPM_DIR}/main/"
echo "  linux-x64: ${NPM_DIR}/linux-x64/"
echo ""
echo "Publish order:"
echo "  1. npm publish ${NPM_DIR}/linux-x64/ --access public"
echo "  2. npm publish ${NPM_DIR}/main/      --access public"
