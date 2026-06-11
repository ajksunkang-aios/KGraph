# KGraph Lazy-Indexing 设计与评估

> 增量索引更新机制——构建驱动、编译器对齐、git 暂存区感知。
> 本文档评估方案的**可行性 / 复杂度 / 必要性**，并给出设计。

## 0. TL;DR

| 维度 | 评级 | 一句话 |
|---|---|---|
| **可行性** | ✅ 高 | 所有零件都已存在：文件级 DB 粒度、`sha` 字段、Kbuild 依赖追踪、git 状态 |
| **复杂度** | ⚠️ 中 | 主要难点是跨文件边失效和头文件依赖闭包——但**构建已经替我们算好了** |
| **必要性** | ✅ 高（KBench）/ ⚠️ 中（交互开发） | 全量重索引 ~16 分钟；KBench 多 commit 场景下增量是刚需 |

**核心洞察**：codegraph 监听**文件保存**（语法级，存了就能解析）；
KGraph 应监听**构建**（语义级，编译过才算数）。
一个文件被改但没构建，它还不是「编译真相」，只是草稿——所以不索引它，fallback 到 grep。
这让 lazy-indexing 与 KGraph「编译器感知」的身份天然一致。

---

## 1. 背景：与 codegraph auto-sync 的本质区别

| | **codegraph auto-sync** | **KGraph lazy-indexing** |
|---|---|---|
| 触发器 | 文件保存（FSEvents/inotify watcher + debounce） | 构建完成（make 流水线 / 显式 sync） |
| 解析层次 | 语法级（tree-sitter，不需编译） | 语义级（scip-clang，需要编译） |
| 常驻进程 | 需要 daemon 持续监听 | 无 daemon，按需触发 |
| 对未构建文件 | 立即重解析（语法总能解析） | **不索引**（未编译 = 非真相），grep fallback |
| 新鲜度 | 始终最新（含草稿） | 始终是「最后一次成功构建」的稳定快照 |

**为什么 KGraph 不能照搬 auto-sync？**
- scip-clang 索引一个 TU 需要**完整编译上下文**（宏展开、头文件、config）——
  一个保存到一半的文件、引用了还没定义的符号，scip-clang 会报错或产出错误符号。
- 内核源文件的语义只在「编译通过」时才确定。监听保存 = 索引大量瞬态草稿，浪费且不可靠。

**「lazy」的含义**：不 eager 地监听每次保存，而是**惰性地把索引更新推迟到构建自然重新验证这些文件时**。
构建就是那个 lazy 触发点。

---

## 2. 核心机制：构建已经替我们算好了依赖闭包

增量索引最难的子问题是**头文件依赖**：
改 `fs/ext4/inode.c` → 只影响这一个 TU（容易）；
改 `include/linux/fs.h` → 影响数千个 TU（头文件依赖闭包，难）。

**关键洞察：我们不需要自己算头文件→TU 的映射，Kbuild 已经算过了。**

`make` 之后：
- 被重新编译的 `.o` 文件，mtime 更新了
- Kbuild 在 `.<obj>.o.cmd` 文件里记录了每个对象的完整头文件依赖
- **「构建后哪些 .o 被重建」这个集合，就是「需要重新索引的 TU 集合」**

所以增量索引的成本天然 ∝ 构建成本：
- 改一个叶子 `.c` → 重建一个 `.o` → 重索引一个 TU（秒级）
- 改 `fs.h` → 重建数千 `.o` → 重索引数千 TU（接近全量，但这本来就该全量）

这是一个非常漂亮的性质——**lazy-indexing 的代价精确追踪构建的代价**，不多不少。

---

## 3. 稳定 vs 不稳定：git 暂存区感知

DB 始终反映一个**已提交、已编译、稳定的快照**。区分三类文件：

```
┌─────────────────────────────────────────────────────────────┐
│ 文件状态分类（git + 构建）                                    │
├─────────────────────────────────────────────────────────────┤
│ ① 已提交 + 已构建 + 内容 hash 与 DB 不同                       │
│    → 稳定且过时 → ★ 增量重索引目标                            │
│                                                               │
│ ② 已提交 + 内容 hash 与 DB 相同                               │
│    → 稳定且最新 → 跳过（已索引）                              │
│                                                               │
│ ③ 工作区/暂存区已修改（git status 显示 dirty）                │
│    → 不稳定（草稿，高概率构建不过/频繁变动）                  │
│    → ★ 不索引，MCP 返回 grep fallback 信号                    │
└─────────────────────────────────────────────────────────────┘
```

**为什么暂存区文件不索引（用户的核心诉求）**：
1. 高概率构建不过——scip-clang 索引会失败或产出错误符号
2. 频繁变动——索引了立刻又脏，浪费
3. 即使编译过，也是瞬态草稿，不值得污染稳定快照

**grep fallback 信号**：MCP server 在每次查询时检查目标符号所在文件的 git 状态。
若文件 dirty，在返回结果前加一行 banner：

```
⚠ fs/ext4/inode.c 有未提交修改——索引版本可能过时，请直接 read/grep 它获取实时内容。
```

这与 codegraph 的 staleness banner 同理，但**由 git status 驱动，而非文件 watcher**。

---

## 4. 增量索引流程（7 步）

```
触发：kgraph sync  （或 kgraph build 包装的 make 后置钩子）

P1. 读基线
    从 meta 表读 last_index_timestamp T、last_index_commit C

P2. 找重建的 TU
    扫描 compile_commands.json 列出的所有 .o
    保留 mtime > T 的 → 这些 TU 在上次索引后被重新编译

P3. git 稳定性过滤
    对每个候选 TU 的 .c 源文件：
      - git status 显示 dirty（工作区/暂存区修改）→ 标记 unstable，剔除
      - 已提交且 content-hash 与 files.sha 不同 → 保留为增量目标
      - hash 相同 → 剔除（mtime 变了但内容没变，如 git checkout）

P4. 局部 scip-clang
    生成 filtered_compile_commands.json（仅含增量目标 TU）
    scip-clang --compdb-path filtered_compile_commands.json → partial.scip

P5. 局部灌库（事务内）
    BEGIN TRANSACTION
    对每个增量目标文件 F：
      - 删除 F 的旧记录：occurrences WHERE file_id=F、edges WHERE file_id=F、
        symbols WHERE def_file_id=F（见 §5 边失效处理）
    解析 partial.scip → IngestBatch → 写入新记录
    COMMIT

P6. 更新 unstable 标记
    把 P3 中 dirty 文件的路径写入 meta（或专用 unstable_files 表）
    供 MCP server 查询时做 grep fallback 信号

P7. 更新基线
    meta.last_index_timestamp = now
    meta.last_index_commit = git HEAD
```

---

## 5. 跨文件边失效处理（复杂度核心）

边表 `edges(src_id, dst_id, type, file_id, ...)` 的失效分两类：

**① 边的「出处」在变化文件内（`edges.file_id = F`）—— 容易**
删除 `edges WHERE file_id = F`，重新派生即可。这覆盖了大部分调用边
（调用边的 file_id = 调用发生的文件）。

**② 边指向变化文件内定义的符号（`dst_id` 是 F 里的符号）—— 需谨慎**
- 若符号改名/删除：旧 `dst_id` 可能悬空。
- **SCIP 符号的稳定性救了我们**：`scip_symbol` 是内容稳定的全局标识
  （如 `... ext4_file_read_iter().`）。只要函数名不变，scip_symbol 不变，
  symbol_id 不变，指向它的边继续有效。
- 只有**改名/删除**的符号会产生悬空边。处理策略：
  - **MVP**：悬空边指向的 symbol 标记 `is_external=1`（定义已不存在），保留边但降权。
    查询时若 dst 符号无定义，提示「符号可能已重命名/删除」。
  - **完善**：增量后跑一次轻量 GC——删除 dst_id 既无定义 occurrence 又无 SymbolInformation 的边。

**符号 ID 复用问题**：
重索引文件 F 时，F 里的符号若 scip_symbol 不变，应**复用原 symbol_id**
（`INSERT OR IGNORE` + 更新 def 位置），而非删除重建。
否则其他文件指向这些符号的边会全部悬空。
→ 删除时**只删 occurrences 和 edges，不删 symbols**；symbols 用 upsert 更新定义位置。

---

## 6. 需要的 Schema / 代码改动

**已有、可复用**：
- `files.sha`（当前空）→ 填入文件内容 hash
- `meta.index_timestamp` → 重命名/扩展为 `last_index_timestamp` + 新增 `last_index_commit`
- 文件级外键 `file_id` / `def_file_id` → 增量删除的粒度基础

**需新增**：
```sql
-- 不稳定文件清单（dirty，grep fallback）
CREATE TABLE unstable_files(
  path        TEXT PRIMARY KEY,
  reason      TEXT,        -- 'working_tree' | 'staged' | 'build_failed'
  detected_at INTEGER
);

-- files 表补充
ALTER TABLE files ADD COLUMN indexed_at INTEGER;   -- 该文件最后索引时间
ALTER TABLE files ADD COLUMN content_sha TEXT;     -- 内容 hash（或复用 sha）
```

**新增代码模块**：
```
src/sync/
├── change_detector.py   # P2-P3: mtime 扫描 + git 稳定性过滤 + hash 比对
├── incremental.py       # P4-P5: filtered compile_commands + 局部 scip-clang + 事务灌库
└── git_status.py        # git diff / status 封装（稳定性判定）
```

**SQLiteStore 新增方法**：
```python
delete_file_records(file_path)      # 删 occurrences + edges（不删 symbols）
upsert_symbol(...)                   # scip_symbol 不变则复用 id
mark_unstable(paths, reason)         # 写 unstable_files
get_unstable_files()                 # MCP banner 用
get_file_sha(path) / set_file_sha    # hash 比对
```

**MCP server 改动**：
查询返回前调 `get_unstable_files()`，若命中目标文件则加 grep fallback banner。

---

## 7. 触发方式：如何「在 make 流水线自动隐藏索引更新」

三个选项，按侵入性排序：

| 方式 | 侵入性 | 说明 |
|---|---|---|
| **A. `kgraph build` 包装器**（推荐） | 低 | `kgraph build -- make CC=clang LLVM=1 -j$(nproc)`：记录 pre-build 基线 → 透传执行 make → 后置触发 P1-P7。用户只需把 `make` 换成 `kgraph build -- make` |
| **B. 显式 `kgraph sync`** | 零 | 用户构建后手动跑 `kgraph sync`，自己比对 mtime+git 增量更新。最简单，但需用户记得 |
| **C. git hook（post-commit/post-merge）** | 中 | 提交/拉取后触发。但 git 事件 ≠ 构建事件，文件可能还没编译，与「编译器对齐」哲学冲突 |

**推荐 A + B 组合**：
- `kgraph build -- <make命令>` 做到「make 流水线自动隐藏索引更新」（用户诉求）
- `kgraph sync` 作为兜底，任何时候手动触发增量

**不推荐 C**：git 提交时文件未必编译过，违背「只索引编译真相」原则。

---

## 8. 可行性 / 复杂度 / 必要性 详评

### 8.1 可行性 ✅ 高
所有依赖已就位，无需外部新组件：
- ✅ 文件级 DB 粒度（file_id 外键）
- ✅ `sha` 字段已在 schema（只是没填）
- ✅ Kbuild 的 `.o` mtime + `.o.cmd` 依赖文件（构建自带依赖追踪）
- ✅ git 做稳定性判定
- ✅ scip-clang 支持 filtered compile_commands.json（按 TU 子集索引）

**唯一需验证**：scip-clang 对单 TU 子集索引时，跨 TU 符号引用是否完整解析。
预期没问题（SCIP 设计上每个 TU 独立索引 + 全局 scip_symbol 合并），但需实测。

### 8.2 复杂度 ⚠️ 中
| 子问题 | 复杂度 | 缓解 |
|---|---|---|
| 文件变更检测 | 低 | mtime + git status + content hash |
| 头文件依赖闭包 | **低**（本以为高） | **构建已算好**——读重建的 .o 集合即可 |
| 局部 scip-clang | 中 | filter compile_commands.json，实测验证子集索引正确性 |
| 跨文件边失效 | 中 | scip_symbol 稳定性 + symbol upsert（不删 symbol）+ 轻量 GC |
| 事务原子性 | 低-中 | 单事务包裹删除+插入，scip-clang 失败则回滚 |
| grep fallback 信号 | 低 | unstable_files 表 + MCP banner |

总体中等。最大的复杂度驱动（头文件依赖）被「构建自带依赖追踪」化解，这是关键。

### 8.3 必要性
**KBench 场景：✅ 高（刚需）**
- 全量索引：scip-clang 436s + 灌库 ~540s ≈ **16 分钟/次**
- KBench 每条用例一个 base_commit，数百条 → 全量重索引不可行
- 增量：相邻 commit 通常改几个到几十个文件 → 秒级到分钟级
- **没有 lazy-indexing，KBench 的 token/tool-call 能效评测跑不起来**

**交互开发场景：⚠️ 中**
- 开发者 `git pull` 后改动几十个文件、`make` 增量构建
- 全量重索引 16 分钟太久；增量保持图谱新鲜，成本可接受
- 但开发者也可接受「偶尔手动全量重建」，所以不是绝对刚需

---

## 9. 实施阶段建议

| 阶段 | 交付 | 验收 |
|---|---|---|
| **L1 hash 基线** | 灌库时填 `files.content_sha` + `meta.last_index_commit` | 全量索引后每个文件有 hash |
| **L2 变更检测** | `change_detector.py`：mtime 扫描 + git 过滤 + hash 比对 | 改一个文件能正确识别出增量目标集合 |
| **L3 局部索引** | filtered compile_commands + 局部 scip-clang → partial.scip | 单 TU 子集索引产出正确 partial 索引 |
| **L4 事务灌库** | `delete_file_records` + `upsert_symbol` + 事务 | 增量更新后查询结果与全量一致 |
| **L5 grep fallback** | `unstable_files` 表 + MCP banner | dirty 文件查询时返回 fallback 信号 |
| **L6 build 包装器** | `kgraph build -- <make>` + `kgraph sync` | 构建后图谱自动增量更新 |

L1-L2 低成本先做（为 KBench 铺路），L3-L4 是核心，L5-L6 是体验完善。

---

## 10. 开放问题 / 风险

1. **scip-clang 子集索引的正确性**——单 TU 索引时，它对该 TU 引用的、定义在其他
   未重索引文件中的符号，能否产出稳定一致的 scip_symbol？需 L3 阶段实测。
   若不一致，跨文件边会断。**这是最大风险点。**

2. **符号改名的边悬空**——MVP 用 is_external 标记 + 轻量 GC，但「改名」语义
   （旧符号删除 + 新符号新增，调用方的边该重连到新符号）SCIP 不直接给，
   需要靠调用方文件也重索引来自然修复。多数情况成立（改名通常连带改调用方），
   但跨文件改名（改定义不改调用，如宏间接）可能漏。

3. **暂存区「部分构建」的歧义**——开发者 dirty 工作区 + `make`，dirty 文件确实
   编译了。我们仍按 git status 判定为 unstable 跳过。这符合用户诉求，但意味着
   「构建过但没提交」的文件不进图谱——需在文档明确这个语义。

4. **多 config 场景**——lazy-indexing 假设单一固定 config（defconfig）。
   切换 config 会改变编译的文件集合，相当于换了一个图谱，应触发全量重建而非增量。
   需检测 config 变化（如 `.config` hash）并强制全量。

---

## 附录：与 KGraph 哲学的一致性

lazy-indexing 不是给 KGraph「加一个功能」，而是**把 KGraph 的核心哲学贯彻到索引更新**：

| KGraph 原则 | lazy-indexing 的体现 |
|---|---|
| 只索引编译器看到的真相 | 只在构建成功后更新索引；未编译的草稿不进图谱 |
| 配置感知 | 增量目标 = 当前 config 实际重建的 TU；换 config 触发全量 |
| 函数指针可达（ops_bind） | 增量重派生 ops_bind 边，与全量同源逻辑 |
| 对不稳定状态诚实 | dirty 文件明确标记 unstable + grep fallback，不假装索引是最新的 |

构建是 KGraph 的真相来源，所以**构建也应是 KGraph 的索引更新时机**——这就是 lazy-indexing 的全部立论。