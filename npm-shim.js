#!/usr/bin/env node
/**
 * KGraph — npm bin shim.
 *
 * Resolves the installed platform bundle (optionalDependency) and execs
 * the launcher. Falls back to downloading from GitHub Releases if the
 * platform package is missing (e.g. npmmirror didn't mirror it).
 *
 * Usage: kgraph <command> [args...]
 */

const path = require('path');
const { execSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const https = require('https');

const PKG = '@ajksunkang-aios/kgraph-linux-x64';
const LAUNCHER = 'kgraph-launcher';
const VERSION = '0.1.0'; // sync with package.json
const GITHUB_REPO = 'ajksunkang/KGraph';

// ── 1. Resolve platform bundle ──

function resolveBundle() {
  // Try optionalDependency
  try {
    const pkgJson = require.resolve(`${PKG}/package.json`);
    const dir = path.dirname(pkgJson);
    if (fs.existsSync(path.join(dir, 'bin', LAUNCHER))) {
      return dir;
    }
  } catch {}

  // Try fallback cache
  const cacheDir = path.join(os.homedir(), '.kgraph', 'bundles', VERSION);
  if (fs.existsSync(path.join(cacheDir, 'bin', LAUNCHER))) {
    return cacheDir;
  }

  return null;
}

// ── 2. Download from GitHub Releases ──

function downloadBundle() {
  const cacheDir = path.join(os.homedir(), '.kgraph', 'bundles', VERSION);
  const archiveName = `kgraph-linux-x64-${VERSION}.tar.gz`;
  const url = `https://github.com/${GITHUB_REPO}/releases/download/v${VERSION}/${archiveName}`;

  console.error(`KGraph: platform bundle not found, downloading v${VERSION}...`);

  fs.mkdirSync(cacheDir, { recursive: true });

  const tmpDir = path.join(os.tmpdir(), `kgraph-download-${process.pid}`);
  fs.mkdirSync(tmpDir, { recursive: true });
  const tmpArchive = path.join(tmpDir, archiveName);

  try {
    execSync(`curl -fsSL -o "${tmpArchive}" "${url}"`, { stdio: 'inherit' });
    execSync(`tar xzf "${tmpArchive}" -C "${cacheDir}" --strip-components=1`, { stdio: 'inherit' });
  } finally {
    try { fs.rmSync(tmpDir, { recursive: true }); } catch {}
  }

  if (!fs.existsSync(path.join(cacheDir, 'bin', LAUNCHER))) {
    console.error(`KGraph: failed to download platform bundle from ${url}`);
    console.error('Set KGRAPH_NO_DOWNLOAD=1 to skip this, or install manually.');
    process.exit(1);
  }

  return cacheDir;
}

// ── 3. Main ──

function main() {
  if (process.env.KGRAPH_NO_DOWNLOAD === '1') {
    console.error('KGraph: online download disabled (KGRAPH_NO_DOWNLOAD=1)');
    process.exit(1);
  }

  let bundleDir = resolveBundle();
  if (!bundleDir) {
    bundleDir = downloadBundle();
  }

  const launcher = path.join(bundleDir, 'bin', LAUNCHER);
  const result = spawnSync(launcher, process.argv.slice(2), {
    stdio: 'inherit',
    env: {
      ...process.env,
      KGRAPH_BUNDLE: bundleDir,
    },
  });

  process.exit(result.status ?? 1);
}

main();
