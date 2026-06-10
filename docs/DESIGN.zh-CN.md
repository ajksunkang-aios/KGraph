[English](DESIGN.md) | [中文](DESIGN.zh-CN.md)

# KGraph — 编译器感知的内核图谱引擎

> 把内核源码经由 `compile_commands.json + SCIP-clang` 抽取成**编译器语义级**代码知识图谱，
> 持久化到 SQLite，通过 MCP 暴露精准、紧凑、可裁剪的图查询能力，
> 让 LLM Agent 在分析内核问题（crash 根因定位、patch 影响分析）时，
> 用**最少的 token / tool-call** 拿到最相关的上下文。

## 1. 技术品牌与差异化定位

**核心定位**：**compiler-aware kernel graph engine** —— 只索引编译器真正看到的代码。

### 1.1 与竞品对比

| 维度 | **codegraph** (colbymchenry) | **semcode** (facebookexperimental) | **KGraph** |
|---|---|---|---|
| 解析后端 | tree-sitter（语法级） | tree-sitter（语法级） | **SCIP-clang（编译器语义级）** |
| 存储后端 | SQLite + FTS5 | LanceDB（向量库） | SQLite + FTS5 |
| 语言范围 | 20+ 语言通用 | C/C++/Rust | **C（内核），MVP 先 Linux** |
| 目标场景 | AI agent 通用提效 | 内核工程（git/lore/向量） | **内核 crash/patch 根因定位的 token×tool-call 能效** |
| 调用图 | 启发式名字匹配 | tree-sitter AST 名字匹配 | **clang 精确符号解析 + ops_bind 间接调用** |
| 亮点 UX | 多 agent 安装、framework 路由、iOS/RN 桥接 | git range、lore/LKML、向量搜索、overlay | config-aware、宏展开、函数指针表可达 |
| 实现语言 | TypeScript/Node | Rust | **C (ingest) + Python (query/MCP)** |

### 1.2 五个核心差异化优势

> codegraph 求广（20+ 语言、语法级、装得快）；semcode 求内核工程化（git/lore/向量，但仍是语法级）；
> **KGraph 求"编译器看到的真相"**——专为内核 crash/patch 根因定位的 token×tool-call 能效而生。

1. **Config-aware**：基于 `compile_commands.json`，只索引该 build 真实编译进去的代码。
   tree-sitter 把所有 `#ifdef CONFIG_X` 分支都当代码，产生大量死分支噪声和错误调用边；
   KGraph 看到的是 `x86_64 defconfig` 真实激活的那一份。内核里同一函数在不同 config 下行为完全不同。

2. **宏正确展开**：`EXPORT_SYMBOL`、`SYSCALL_DEFINE`、`container_of`、per-cpu 宏、tracepoint 宏——
   这些是内核的骨架。SCIP-clang 在预处理后索引，符号定位准确；
   tree-sitter 对内核重宏只能启发式猜（semcode 自己说 macro 只保留 function-like "for better signal-to-noise"）。

3. **真实类型/符号解析**：clang 知道 `f_op` 的真实类型是 `struct file_operations *`，
   知道跨 TU 的同名 static 函数是不同符号。tree-sitter 靠名字匹配，内核大量同名 static helper 会张冠李戴。

4. **间接调用 / ops_bind（杀手锏）**：`.read_iter = ext4_file_read_iter` 这种 ops 表初始化，
   clang 能精确归属字段→实现函数，派生 `ops_bind` 边。
   内核 90% 的核心控制流是函数指针表（VFS/驱动/net），**纯 tree-sitter 工具在这里基本断链**。
   这是 crash 根因定位最值钱的边。

5. **SYSCALL_DEFINE → 系统调用入口可达**：clang 展开后能把 `sys_read` 这类宏生成的入口接进调用图，
   crash 栈往往从 syscall 入口往下走。

**诚实承认的代价**：必须先成功编译内核、索引慢且重、按 commit 索引成本高。
semcode 的 git/lore/overlay 我们短期没有——但这些是周边能力，不是图质量，可后补。

---

## 2. 整体架构

精简三层 + 两个领域抽象，无多余抽象层：

```
┌──────────────────────────────────────────────────────┐
│              MCP Server (Python)                       │
│  工具集 + token 预算控制 (limit/depth/summary)         │
│  search / definition / callers / callees / neighborhood│
│  call_path / struct_layout / ops_impls / subsystem     │
└───────────────┬──────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────┐
│              Query Engine (Python)                     │
│  图算法：k-hop / 反向调用 / 最短路径 / 排序裁剪         │
│  直接写 SQLite 递归 CTE（无 GraphStore 抽象层）        │
└───────────────┬──────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────┐
│          libkgraph Ingest Core (C)                     │
│  scip.proto 解析 → 规范化 → 批量灌库 SQLite            │
│  派生：调用图(enclosing) + ops_bind + includes         │
│  直接对接 SCIP（无 IndexAdapter 抽象层）               │
└───────────────┬──────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────┐
│          KernelProfile (Python)   ★保留抽象            │
│                                                        │
│  ├── BuildPipeline                                     │
│  │     单仓: build_cmd, config_cmd, post_build_hook    │
│  │     多仓: manifest_parser, repo_sync, cc_merge      │
│  │     无CC: intercept_build (bear/scan-build)         │
│  │                                                     │
│  └── DomainEnrichment                                  │
│  │     Linux: MAINTAINERS→subsystem, Kconfig过滤,       │
│  │           syscall table, ops struct field mapping    │
│  │     Android: .hal→HIDL, selinux, binder (future)    │
│  │     Zephyr: Kconfig(different syntax), DT, west     │
│  │     FreeBSD: __FreeBSD_version, sys/kconf           │
└──────────────────────────────────────────────────────┘

Pipeline (Python 编排):
  kernel src → KernelProfile.BuildPipeline → compile_commands.json
            → scip-clang → index.scip
            → libkgraph ingest → SQLite
            → KernelProfile.DomainEnrichment → 写入 subsystem/config 元数据
```

**设计决策记录**：

| 决策 | 选择 | 理由 |
|---|---|---|
| IndexAdapter 抽象层 | **删除**，SCIP 直接对接 | YAGNI；技术品牌 = compiler-aware，当前只走 SCIP-clang |
| GraphStore 抽象层 | **删除**，SQLite 直写 | 部署精简（单 .db 文件）、WAL 批写吞吐够、递归 CTE 读性能够；codegraph 验证可行；未来换 Neo4j 只需重构 Query Engine 内部 SQL→Cypher |
| KernelProfile | **保留** | 构建系统差异（单仓/多仓）和领域富化都是内核身份绑定的知识，扩展其他内核时只写新 Profile |
| C/Python 分工 | C 做 ingest 热点，Python 做 query/MCP/编排 | 千万级记录批量灌库走 C；迭代快的逻辑走 Python |

---

## 3. 数据模型

### 3.1 节点与边类型

**节点类型**：`function / struct / field / macro / typedef / global_var / file`

**边类型**：

| 边类型 | 含义 | 来源 |
|---|---|---|
| `calls` | A 直接调用 B | enclosing_range 派生 |
| `references` | A 引用 B（非调用） | occurrence role 派生 |
| `defines` | 文件/宏 定义符号 | SCIP definition occurrence |
| `contains` | 结构体 包含 字段；文件 包含 符号 | SCIP 关系 |
| `includes` | 文件 #include 文件 | SCIP 关系 |
| `ops_bind` | ops 变量 绑定 字段→实现函数（间接调用） | enclosing + 初始化模式派生，★核心差异化 |
| `type_of` | 变量/参数 的类型是 某类型 | SCIP signature 信息 |
| `macro_expands` | 宏 在某位置展开 | SCIP occurrence |

### 3.2 SQLite Schema

```sql
-- ===== 符号节点 =====
CREATE TABLE symbols(
  id              INTEGER PRIMARY KEY,
  scip_symbol     TEXT UNIQUE,            -- SCIP 全局唯一符号串（跨 TU 解析靠它）
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL,           -- function / struct / field / macro / typedef / global_var
  signature       TEXT,                   -- 函数签名 / 结构体声明
  documentation   TEXT,                   -- SCIP documentation
  def_file_id     INTEGER REFERENCES files(id),
  def_start_line  INTEGER,
  def_end_line    INTEGER,                -- 来自 enclosing_range
  is_external     INTEGER DEFAULT 0,      -- 1 = 未在本次索引中找到定义（头文件符号等）
  subsystem       TEXT                    -- 由 KernelProfile.DomainEnrichment 写入
);

CREATE TABLE files(
  id          INTEGER PRIMARY KEY,
  path        TEXT UNIQUE NOT NULL,
  language    TEXT,                        -- C / header
  subsystem   TEXT,                        -- 由 KernelProfile 写入（MAINTAINERS 解析）
  sha         TEXT                         -- 文件内容 hash（用于增量更新检测）
);

-- ===== 定义/引用 occurrence =====
CREATE TABLE occurrences(
  id                  INTEGER PRIMARY KEY,
  symbol_id           INTEGER NOT NULL REFERENCES symbols(id),
  file_id             INTEGER NOT NULL REFERENCES files(id),
  start_line          INTEGER NOT NULL,
  start_col           INTEGER NOT NULL,
  end_line            INTEGER NOT NULL,
  end_col             INTEGER NOT NULL,
  role                INTEGER NOT NULL,    -- SCIP SymbolRole 位掩码
  enclosing_symbol_id INTEGER REFERENCES symbols(id)  -- ★派生：该引用落在哪个函数体内
);

CREATE INDEX idx_occ_symbol ON occurrences(symbol_id);
CREATE INDEX idx_occ_file ON occurrences(file_id, start_line);
CREATE INDEX idx_occ_enclosing ON occurrences(enclosing_symbol_id);

-- ===== 通用边表 =====
CREATE TABLE edges(
  src_id      INTEGER NOT NULL REFERENCES symbols(id),
  dst_id      INTEGER NOT NULL REFERENCES symbols(id),
  type        TEXT NOT NULL,               -- calls / references / defines / contains / includes / ops_bind / type_of / macro_expands
  file_id     INTEGER REFERENCES files(id),
  line        INTEGER,
  weight      INTEGER DEFAULT 1,           -- 调用频次（同位置合并则为 >1）
  confidence  REAL DEFAULT 1.0,            -- 间接调用/宏 用低置信 (0.3-0.7)
  metadata    TEXT,                         -- JSON: ops_bind 有 field_name; macro_expands 有 expansion_text
  PRIMARY KEY(src_id, dst_id, type, file_id, line)
);

CREATE INDEX idx_edge_src ON edges(src_id, type);
CREATE INDEX idx_edge_dst ON edges(dst_id, type);     -- ★反向调用图靠它
CREATE INDEX idx_edge_type ON edges(type);

-- ===== 全文搜索（可选 FTS5） =====
-- CREATE VIRTUAL TABLE symbols_fts USING fts5(name, signature, documentation, content=symbols, content_rowid=id);

-- ===== 元信息 =====
CREATE TABLE meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 存储: kernel_name, kernel_version, config_name, index_timestamp, scip_version 等
```

### 3.3 关键派生步骤（C 侧 ingest 完成）

**调用图派生**：
对每个非定义 occurrence（函数 B 在文件 F 位置 P），找其 `enclosing_range` 覆盖 P 的函数定义 A
→ 写 `A --calls--> B` 边。

**ops_bind 派生（核心差异化）**：
```
static struct file_operations ext4_file_operations = {
    .read_iter      = ext4_file_read_iter,    ← SCIP 记录对 ext4_file_read_iter 的引用
    .write_iter     = ext4_file_write_iter,
};
```
SCIP 对 `ext4_file_read_iter` 的引用 enclosing 到全局变量 `ext4_file_operations`
→ 写 `ext4_file_operations --ops_bind{field=read_iter}--> ext4_file_read_iter`。
后续 `f_op->read_iter()` 可经「字段名→所有 ops_bind 绑定」做候选解析（confidence=0.5，低置信标注）。

---

## 4. MCP 工具集

面向「省 token」设计，每个工具带预算参数。

| 工具 | 作用 | 关键参数 | 省 token 机制 |
|---|---|---|---|
| `search_symbols(q)` | 名称/模式/全文检索符号 | `kind, limit` | limit 截断 |
| `get_definition(sym)` | 定义位置 + 签名（不返回整文件） | — | 只返定位+签名 |
| `find_callers(sym)` | 反向调用图（谁调用我） | `depth, limit` | depth 控爆炸 |
| `find_callees(sym)` | 正向调用图 | `depth, limit` | 同上 |
| `get_neighborhood(sym)` | N-hop 子图 | `depth, edge_types, summary` | summary=true 只返名字+位置 |
| `call_path(a, b)` | 两函数间调用路径 | `max_len` | 只返路径，不返源码 |
| `get_struct_layout(t)` | 结构体字段 | — | 紧凑表格格式 |
| `find_ops_impls(field)` | 函数指针字段→候选实现 | `struct_type` | ★间接调用核心工具 |
| `which_subsystem(sym/file)` | MAINTAINERS 子系统归属 | — | 单值返回 |
| `expand_macro(name)` | 宏定义 | — | 只返宏体 |

**省 token 三板斧**：
1. `summary` 模式：只回名字+文件:行，省去签名/文档/源码
2. `depth / limit`：控图遍历爆炸
3. 结果按「图距离 + 调用频次」排序后截断，最相关的先返回

---

## 5. C / Python 分工

| 组件 | 语言 | 理由 |
|---|---|---|
| `libkgraph` ingest：SCIP protobuf 解析、批量灌库、调用图/ops_bind 派生 | **C** | 千万级记录性能关键路径；protobuf-c 高效；可独立 CLI 也可 CFFI 供 Python |
| Pipeline 编排、KernelProfile、Query Engine、MCP Server | **Python** | 迭代快、MCP SDK 好用、算法层不走热点 |

**契约 = SQLite Schema**。C 和 Python 通过同一个 .db 文件协作，完全解耦。

---

## 6. KernelProfile 详细设计

### 6.1 BuildPipeline

```
KernelProfile.BuildPipeline
  │
  ├── LinuxBuildPipeline (MVP)
  │     config_cmd:  make CC=clang LLVM=1 x86_64_defconfig
  │     build_cmd:   make CC=clang LLVM=1 -j$(nproc)
  │     cc_cmd:      scripts/clang-tools/gen_compile_commands.py
  │     scip_cmd:    scip-clang --compilation-database compile_commands.json
  │
  ├── AndroidBuildPipeline (future)
  │     manifest:    repo manifest.xml
  │     sync:        repo sync
  │     build:       soong_ui --makecode
  │     cc_merge:    合并多仓 compile_commands.json
  │
  ├── ZephyrBuildPipeline (future)
  │     manifest:    west manifest.yml
  │     sync:        west update
  │     build:       west build -b <board>
  │     cc_cmd:      自动生成在 build/zephyr/
  │     cc_merge:    合并多仓
  │
  └── FreeBSDBuildPipeline (future)
        build:      bear make buildworld
        cc_cmd:     bear 拦截
```

### 6.2 DomainEnrichment

```
KernelProfile.DomainEnrichment
  │
  ├── LinuxEnrichment (MVP)
  │     MAINTAINERS  → files.subsystem, symbols.subsystem
  │     Kconfig      → config-aware 标注（哪些符号只在某 CONFIG 下激活）
  │     syscall table → syscall 入口→内核函数映射
  │     ops struct registry → 常见函数指针表字段的语义映射
  │
  ├── AndroidEnrichment (future)
  │     .hal → HIDL/AIDL 接口定义
  │     selinux policy → 权限约束标注
  │     binder → IPC 跨进程调用边
  │
  ├── ZephyrEnrichment (future)
  │     devicetree → 硬件拓扑节点
  │     Kconfig (Zephyr syntax) → 配置标注
  │     west manifest → 多仓拓扑
  │
  └── FreeBSDEnrichment (future)
        __FreeBSD_version → 版本标注
        sys/kconf → 配置标注
```

### 6.3 多仓合并策略

```
manifest_parser(repo_xml / west_yml)
  → 仓列表 + 版本锁定 + 路径映射

per_repo_build()
  → 各子仓独立生成 compile_commands.json

cc_merge(cc_list[])
  → 合并多份 compile_commands.json
  → 修正相对路径为统一根目录路径
  → 去重（同一文件可能出现在多个子仓的 CC 中）
  → 输出: merged_compile_commands.json

后续 scip-clang → index.scip → libkgraph ingest (单库)
```

---

## 7. 设计流程（处理管线）

```
P0 环境校验  : clang / scip-clang / protobuf-c 工具链、内核可编译
P1 构建产出  : KernelProfile.BuildPipeline → compile_commands.json
P2 索引产出  : scip-clang --compilation-database compile_commands.json → index.scip
P3 解析灌库  : libkgraph ingest → SQLite (symbols / occurrences / edges)
P4 图派生    : calls(enclosing) + ops_bind + includes
P5 内核富化  : KernelProfile.DomainEnrichment → subsystem / config / syscall 元数据写入
P6 查询引擎  : k-hop / 反向调用 / 路径 / 排序裁剪（递归 CTE）
P7 MCP 服务  : 工具暴露 + token 预算控制
P8 KBench    : 接 kernel_bench_data.json，量化 token/tool-call/precision-recall
```

---

## 8. 预期效果

### 8.1 具体走查

**场景**：KBench 给一条 crash，栈顶函数 `ext4_file_read_iter`。

```
Agent: find_callers("ext4_file_read_iter", depth=2)
  → 返回 ~10 个 caller（含经 ops_bind 反推的 VFS 间接入口），仅名字+文件:行

Agent: get_neighborhood("ext4_file_read_iter", depth=1, summary=true)
  → 1跳子图：调用的 inode 锁/page cache 相关函数，紧凑结构化

Agent: find_ops_impls("read_iter", struct_type="file_operations")
  → 返回所有 file_operations.read_iter 的绑定实现，含 ext4_file_read_iter

→ Agent 在 3 次 tool-call、~1.5k token 内拿到根因相关函数集合
  对比基线（grep + 整文件读入）通常 10k+ token、命中更散
```

### 8.2 可量化的预期收益（待 KBench 实测验证）

相同根因定位质量下：
- **token 降一个量级**
- **tool-call 个位数**
- 对 `oracle_methods` 的 **recall 因 ops_bind 间接边而提升**

---

## 9. 可行性分析

| 维度 | 结论 | 关键风险 | 缓解策略 |
|---|---|---|---|
| compile_commands 生成 | ✅ 成熟 | 内核需先成功编译 | MVP 用 `CC=clang LLVM=1` x86_64 defconfig，最稳 |
| SCIP-clang 索引 | ✅ 可行 | 内核 GCC 扩展/内联汇编；全量索引耗时长 | MVP 支持按子系统/目录裁剪索引（如只 `fs/`） |
| 直接调用图 | ✅ 强 | — | SCIP Occurrence 带 enclosing_range，直接推导 caller→callee |
| 间接调用（函数指针/ops） | ⚠️ 部分可解 | `file->f_op->read_iter()` SCIP 不解析 | ops_bind 派生 + 字段名→实现函数启发式（低置信标注） |
| 宏 | ⚠️ 中等 | 内核宏极重 | SCIP-clang 预处理后索引，多数可解；复杂宏标注低置信 |
| 按 base_commit 索引 | ⚠️ 成本高 | KBench 每条用例一个 commit | 分层：按 (commit,config) 缓存 + 依赖闭包裁剪索引；MVP 先固定快照 |
| 存储规模 | ✅ SQLite 扛得住 | 百万级 occurrence | WAL + 批量事务 + 合理索引 + FTS5 |

---

## 10. KBench 能效评测设计

复用 `kernel_bench_data.json`（含 `crash_report_data / base_commit / oracle_methods`）。

**指标**：
- **效能**：每条用例的总 token、tool-call 次数、wall-clock
- **质量**：预测方法集 vs `oracle_methods` 的 file-level / method-level **precision / recall / F1 / IoU**
- **对照组**：A=纯文本(grep/读文件) B=semcode MCP C=KGraph MCP

**评测脚本**：Python，读 bench data → 对每条用例构造 prompt → 调 Claude + MCP → 收集结果 → 计算 metrics。

---

## 11. MVP 里程碑

| 里程碑 | 交付 | 验收 |
|---|---|---|
| M1 构建打通 | `fs/ext4` 子系统 compile_commands.json | clang 编译无致命错误 |
| M2 索引产出 | `fs/ext4` 的 index.scip | scip-clang 无致命错误 |
| M3 灌库+Schema | libkgraph C loader + SQLite | 符号/occurrence/边数量合理 |
| M4 图派生 | calls + ops_bind | 抽样函数 caller/callee 正确 |
| M5 内核富化 | MAINTAINERS→subsystem | subsystem 字段填充正确 |
| M6 查询引擎 | k-hop/反向/路径（递归 CTE） | 查询性能 <100ms |
| M7 MCP 服务 | 工具集 + 预算控制 | Claude 能调通所有工具 |
| M8 KBench 接入 | 评测脚本 + 报告 | 跑通 N 条用例出指标 |

---

## 12. 开放决策点（待确认）

| # | 决策 | 当前倾向 | 备注 |
|---|---|---|---|
| D1 | 索引范围 | 先 `fs/ext4` 子系统打通全链路，再放大 | 全量 defconfig 贵但更接近 KBench 真实需求 |
| D2 | 按 base_commit 索引策略 | MVP 用固定快照，KBench 阶段再上增量 | 接受与 bench commit 的漂移 |
| D3 | C 边界 | 先 Python-only 跑通，再把 ingest 换 C | 迭代快；但你说 C+Python 实现 |
| D4 | ops_bind 在 MVP | 建议 MVP 就做 | 核心差异化，不做等于没优势 |
| D5 | 后补 semcode 能力 | Roadmap 列入 git-range/overlay/lore | 不影响 MVP，但规划里要写明 |