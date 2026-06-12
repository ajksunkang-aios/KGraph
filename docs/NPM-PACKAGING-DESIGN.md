# KGraph NPM Packaging Design

> 目标：`npm install -g @ajksunkang-aios/kgraph` 一条命令装好，`kgraph init .` 直接可用。

## 整体架构

参考 codegraph 的 **shim + platform bundle** 模式，但简化为 Linux-only：

```
npm install -g @ajksunkang-aios/kgraph
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  @ajksunkang-aios/kgraph (主包)                       │
│  package.json                                        │
│  ├── bin: { "kgraph": "npm-shim.js" }               │
│  ├── optionalDependencies:                           │
│  │     @ajksunkang-aios/kgraph-linux-x64: "^x.y.z"  │
│  └── npm-shim.js (薄壳，~50 行)                      │
│         │                                            │
│         ▼ 运行时解析                                   │
│  @ajksunkang-aios/kgraph-linux-x64 (平台包)           │
│  ├── python3.10          # 独立 Python 运行时         │
│  ├── lib/                                           │
│  │   ├── site-packages/  # protobuf, mcp 等          │
│  │   └── kgraph/         # KGraph 源码               │
│  │       ├── src/                                   │
│  │       ├── mcp/                                   │
│  │       ├── scripts/                               │
│  │       └── thirdparty/                            │
│  ├── bin/                                           │
│  │   ├── scip-clang      # scip-clang 二进制          │
│  │   └── kgraph-launcher # Shell 启动器               │
│  └── package.json  { os: ["linux"], cpu: ["x64"] }  │
└─────────────────────────────────────────────────────┘
```

## 发布包结构

### 1. 主包 `@ajksunkang-aios/kgraph`

```jsonc
{
  "name": "@ajksunkang-aios/kgraph",
  "version": "0.1.0",
  "description": "Compiler-aware kernel code graph engine",
  "bin": { "kgraph": "npm-shim.js" },
  "files": ["npm-shim.js", "README.md"],
  "optionalDependencies": {
    "@ajksunkang-aios/kgraph-linux-x64": "0.1.0"
  },
  "engines": { "node": ">=18" },
  "license": "MIT"
}
```

- 无 `scripts`（无 postinstall，无副作用）
- `optionalDependencies` 让 npm 只装匹配平台的包（通过 os/cpu 过滤）
- 目前只有 `linux-x64`，后续可加 `linux-arm64`

### 2. 平台包 `@ajksunkang-aios/kgraph-linux-x64`

```jsonc
{
  "name": "@ajksunkang-aios/kgraph-linux-x64",
  "version": "0.1.0",
  "os": ["linux"],
  "cpu": ["x64"],
  "files": ["python", "lib", "bin"],
  "license": "MIT"
}
```

- `os` + `cpu` 字段让 npm 自动跳过不匹配的平台
- 包含完整自包含运行时，用户无需预装 Python

### 3. npm-shim.js（薄壳启动器）

```javascript
#!/usr/bin/env node
const path = require('path');
const { execSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');

// 1. 尝试解析 optionalDependencies 平台包
const platformPkg = '@ajksunkang-aios/kgraph-linux-x64';
let bundleDir;
try {
  bundleDir = path.dirname(require.resolve(`${platformPkg}/package.json`));
} catch {}

// 2. 如果平台包不存在（npmmirror 未镜像等），从 GitHub Releases 下载
if (!bundleDir || !fs.existsSync(path.join(bundleDir, 'bin', 'kgraph-launcher'))) {
  const fallbackDir = path.join(os.homedir(), '.kgraph', 'bundles', 'latest');
  if (!fs.existsSync(path.join(fallbackDir, 'bin', 'kgraph-launcher'))) {
    console.error('Platform bundle not found. Downloading from GitHub Releases...');
    execSync(`curl -fsSL ... | tar xz -C ...`, { stdio: 'inherit' });
  }
  bundleDir = fallbackDir;
}

// 3. exec 启动器，透传所有参数
const launcher = path.join(bundleDir, 'bin', 'kgraph-launcher');
const result = spawnSync(launcher, process.argv.slice(2), {
  stdio: 'inherit',
  env: { ...process.env, KGRAPH_BUNDLE: bundleDir },
});
process.exit(result.status ?? 1);
```

### 4. kgraph-launcher（Shell 启动器）

```bash
#!/bin/bash
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$DIR/python"
export PYTHONPATH="$DIR/lib/kgraph/src:$DIR/lib/kgraph/scripts:$DIR/lib/site-packages"
export PATH="$DIR/bin:$PATH"
exec "$PYTHON" -m mcp.server "$@"
```

## 构建流程

### build-bundle.sh

在 GitHub Actions (ubuntu-latest) 上执行：

```bash
#!/bin/bash
set -euo pipefail
VERSION=$(jq -r .version package.json)
BUNDLE_DIR="release/kgraph-linux-x64"
mkdir -p "$BUNDLE_DIR"/{bin,lib/kgraph,lib/site-packages}

# 1. 下载独立 Python 3.10 (python-build-standalone 项目)
curl -fsSL https://github.com/indygreg/python-build-standalone/releases/download/20241016/\
cpython-3.10.16+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz \
  | tar xz --strip-components=1 -C "$BUNDLE_DIR"
mv "$BUNDLE_DIR/bin/python3" "$BUNDLE_DIR/python"

# 2. 安装 Python 依赖到 site-packages
"$BUNDLE_DIR/python" -m pip install --target "$BUNDLE_DIR/lib/site-packages" \
  "protobuf>=7.35.0,<8" mcp

# 3. 复制 KGraph 源码
cp -r src mcp scripts thirdparty "$BUNDLE_DIR/lib/kgraph/"

# 4. 复制 scip-clang 二进制
cp scip-tools/scip-clang "$BUNDLE_DIR/bin/"
chmod +x "$BUNDLE_DIR/bin/scip-clang"

# 5. 生成启动器
cat > "$BUNDLE_DIR/bin/kgraph-launcher" << 'EOF'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec "$DIR/python" "$@"
EOF
chmod +x "$BUNDLE_DIR/bin/kgraph-launcher"

# 6. 打包
tar czf "release/kgraph-linux-x64-$VERSION.tar.gz" -C release kgraph-linux-x64
```

### pack-npm.sh

生成 npm 发布结构：

```bash
#!/bin/bash
VERSION=$1

# 平台包
mkdir -p release/npm/linux-x64
cp -r release/kgraph-linux-x64/* release/npm/linux-x64/
cat > release/npm/linux-x64/package.json << EOF
{
  "name": "@ajksunkang-aios/kgraph-linux-x64",
  "version": "$VERSION",
  "os": ["linux"],
  "cpu": ["x64"],
  "files": ["python", "lib", "bin"]
}
EOF

# 主包
cp npm-shim.js release/npm/main/
cat > release/npm/main/package.json << EOF
{
  "name": "@ajksunkang-aios/kgraph",
  "version": "$VERSION",
  "bin": { "kgraph": "npm-shim.js" },
  "files": ["npm-shim.js", "README.md"],
  "optionalDependencies": {
    "@ajksunkang-aios/kgraph-linux-x64": "$VERSION"
  }
}
EOF
```

## CI/CD 工作流

```yaml
# .github/workflows/release.yml
name: Release

on:
  push:
    tags: ['v*']

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Build platform bundle
        run: bash scripts/build-bundle.sh

      - name: Pack npm packages
        run: bash scripts/pack-npm.sh ${GITHUB_REF_NAME#v}

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: release/*.tar.gz

      - name: Publish platform package
        run: npm publish release/npm/linux-x64/ --access public
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}

      - name: Publish main package
        run: npm publish release/npm/main/ --access public
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
```

## 安装后的用户体验

```bash
$ npm install -g @ajksunkang-aios/kgraph

$ cd /path/to/linux   # 内核源码目录

$ kgraph init .
# → 使用内置 Python + scip-clang，无需用户预装任何东西

$ kgraph install      # 配置 AI agent

$ kgraph serve        # 启动 MCP 服务
```

## 与 codegraph 的差异

| | codegraph | kgraph |
|---|---|---|
| 语言 | TypeScript (Node.js) | Python |
| 运行时 | vendored Node 24 | vendored Python 3.10 (standalone) |
| 平台 | 6 个 (darwin/linux/win × x64/arm64) | **1 个** (linux-x64) |
| 原生依赖 | 无 (WASM + node:sqlite) | scip-clang 二进制 |
| optionalDependencies | 6 个平台包 | 1 个平台包 |
| 复杂度 | 高 (多平台 + fallback) | **低** (单平台，简单直接) |

## 新增文件清单

```
KGraph/
├── npm-shim.js                      # npm bin 入口（薄壳）
├── scripts/
│   ├── build-bundle.sh              # 构建平台 bundle
│   └── pack-npm.sh                  # 生成 npm 发布结构
└── .github/workflows/
    └── release.yml                  # 发布工作流
```
