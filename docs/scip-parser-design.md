# SCIP Protobuf 解析器设计思路

> 供 review——只给思路和关键决策点，不写代码。

## 1. 要解析什么？

SCIP protobuf 的顶层结构是 `Index`：

```
Index
  ├── Metadata          (1个：version, tool_info, project_root)
  ├── Document[]        (N个：每个源文件一个)
  │     ├── relative_path
  │     ├── language
  │     ├── Occurrence[]   ← 这是量大头（内核一个 defconfig ~百万级）
  │     │     ├── range / typed_range (single_line_range / multi_line_range)
  │     │     ├── symbol (字符串，SCIP 全局符号名)
  │     │     ├── symbol_roles (位掩码：Definition=0x1, Import=0x2, ...)
  │     │     └── enclosing_range / typed_enclosing_range  ← ★调用图派生的关键
  │     └── SymbolInformation[]
  │           ├── symbol, display_name, documentation[], kind
  │           ├── signature_documentation
  │           ├── enclosing_symbol
  │           └── Relationship[]
  │                 ├── symbol, is_reference, is_implementation, is_type_definition, is_definition
  └── SymbolInformation[]  (external_symbols：定义在本次索引之外的符号)
```

**核心量级**：Linux x86_64 defconfig 约 30k 文件，每文件数百 occurrence，总量 ~1-2M occurrence。
SCIP protobuf 文件大小：~2-5GB（取决于是否内嵌源码文本）。

## 2. 解析方案选择

### 方案对比

| 方案 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|
| **A: protobuf-c 生成的 C 代码 + 流式解析** | 零拷贝、内存可控、最快 | 需要先从 proto 生成 C 代码（依赖 protobuf-c 编译器）；代码量大 | libkgraph 的 C ingest 热点路径 |
| **B: 手写 C 解析器（只解析需要的字段）** | 无外部依赖、只解析关心的字段、极轻量 | 手写 varint + tag 解析有出错风险；不兼容 proto 未来变更 | 如果要极致精简、不依赖 protobuf-c |
| **C: Python protobuf 绑定（google.protobuf）** | 开箱即用、proto 生成代码一步到位；易调试 | 内存吃得多（整个 Index 一次加载 ~2-5GB）；慢 | MVP 验证 / 小子系统索引 / 原型 |
| **D: Python 手写解析器（纯 struct 解码）** | 不依赖 proto 编译、可控流式 | 同 B 的手写风险；Python 本身慢 | 不推荐——Python 手写不如直接用 C |

### 推荐：分阶段双轨

**MVP 阶段**：用 **方案 C（Python protobuf 绑定）** 快速跑通全链路。
- `pip install protobuf`，从 `scip.proto` 生成 `scip_pb2.py`
- Python 流式读（`Index` 的 `metadata` + 每个 `Document` 逐个处理）
- 先跑通 fs/ext4，验证数据模型正确性

**生产阶段**：换成 **方案 A（protobuf-c + C 流式解析）** 做性能热点。
- 从 `scip.proto` 生成 C 结构体（`protoc --c_out=scip.pb-c.h scip.pb-c.c`）
- C 侧流式逐 Document 解析，批量写 SQLite
- Python 通过 CFFI 调用或独立 CLI

**不建议方案 B（手写）**：protobuf wire format 虽然简单（varint + tag-value），但 SCIP proto 有 100+ 个 enum 值、嵌套 oneof、deprecated 字段兼容——手写维护成本高于用 protobuf-c 生成。而且 proto 有版本演进，生成代码自动兼容，手写要自己维护。

## 3. 流式解析（关键：不能一次加载整个 Index）

SCIP proto 的 `Index` 可达 5GB，不能 `Index.FromString(data)` 一次反序列化。

**protobuf 的天然优势**：proto3 的 wire format 是 tag-value 序列，每个字段独立编码。
`Index` 的字段编号是：
- `metadata = 1`
- `documents = 2`
- `external_symbols = 3`

这意味着可以从字节流中**按 tag 逐字段提取**，每拿到一个 `Document` 就立即处理、释放内存。

### 3.1 C 侧流式解析（protobuf-c）

```c
// 核心思路：不用 Index_unpack() 整体反序列化
// 而是逐 tag 读取，每遇到一个 Document 就处理

typedef struct {
    sqlite3 *db;
    // 当前批次状态
    uint64_t current_file_id;
    // 批量写缓冲
    symbol_batch_t symbols;
    occurrence_batch_t occurrences;
} ingest_state_t;

int ingest_scip_stream(ingest_state_t *state, const uint8_t *buf, size_t len) {
    ProtobufCBufferSimple buffer;  // 或自定义 buffer
    size_t pos = 0;

    while (pos < len) {
        uint32_t tag = read_varint(buf, &pos);
        uint32_t field_number = tag >> 3;
        uint32_t wire_type = tag & 0x7;

        switch (field_number) {
        case 1:  // metadata
            // 只读一次，记录 project_root / tool_info
            skip_or_read_metadata(buf, &pos, wire_type);
            break;
        case 2:  // documents
            // ★核心：逐 Document 解析 + 立即处理
            Document *doc = read_document_submessage(buf, &pos);
            process_document(state, doc);
            protobuf_c_message_free_unpacked(doc, NULL);  // 立即释放
            break;
        case 3:  // external_symbols
            SymbolInformation *sym = read_symbol_info_submessage(buf, &pos);
            process_external_symbol(state, sym);
            protobuf_c_message_free_unpacked(sym, NULL);
            break;
        default:
            skip_field(buf, &pos, wire_type);
        }
    }
    // 最终 flush 批量缓冲到 SQLite
    flush_batches(state);
}
```

### 3.2 Python 侧流式解析（MVP）

```python
# protobuf-c 生成的 Python 绑定不直接支持流式
# 但可以用底层 wire-format API 手动逐 tag 读取

# 更实用的做法：利用 protobuf 的 MessageToDict + 分片
# 或者用 scip-clang 自带的 Python bindings（如果有的话）

# MVP 最简方案：对 fs/ext4 小子系统，Index 不大，直接整体加载
from scip_pb2 import Index

with open("index.scip", "rb") as f:
    index = Index()
    index.ParseFromString(f.read())  # 小子系统 OK

for doc in index.documents:
    for occ in doc.occurrences:
        process_occurrence(occ)
    for sym_info in doc.symbols:
        process_symbol_info(sym_info)
```

对于**全量内核**（5GB 级），Python 必须走流式：
```python
# 用 protobuf 底层解码器逐 tag 读
# 或者更实际：把 index.scip 按 Document 边界切片（SCIP proto 的
# repeated Document 是连续 tag=2 的子消息，可按长度前缀切割）

def stream_documents(filepath):
    """从 index.scip 中逐 Document 流式读取"""
    with open(filepath, "rb") as f:
        # 1. 先读 metadata (tag=1)
        # 2. 循环读 Document (tag=2)
        # 3. 最后读 external_symbols (tag=3)
        while True:
            tag, wire_type = read_tag(f)
            field_num = tag >> 3
            if field_num == 2:  # Document
                msg_len = read_varint(f)
                raw = f.read(msg_len)
                doc = Document()
                doc.ParseFromString(raw)
                yield doc
            elif field_num == 1:
                # metadata
                msg_len = read_varint(f)
                raw = f.read(msg_len)
                meta = Metadata()
                meta.ParseFromString(raw)
                # 记录 project_root 等
            elif field_num == 3:
                # external_symbols
                ...
            else:
                skip_field(f, wire_type)
```

## 4. 从 SCIP 到 SQLite 的映射逻辑

### 4.1 一次 Document 的处理流程

```
Document(relative_path="fs/ext4/file.c")
  │
  ├── Step 1: 写入 files 表
  │     files(path="fs/ext4/file.c", language="C")
  │     → 得到 file_id
  │
  ├── Step 2: 处理 SymbolInformation[]
  │     for each sym_info in doc.symbols:
  │       ├── scip_symbol → symbols 表 (name, kind, signature, documentation)
  │       ├── display_name → symbols.name (fallback 用 symbol 解析出的 name)
  │       ├── kind enum → symbols.kind 映射：
  │       │     Function/Method/Constructor → "function"
  │       │     Struct/Class/Interface → "struct"
  │       │     Field → "field"
  │       │     Macro → "macro"
  │       │     TypeAlias/Typedef → "typedef"
  │       │     Variable/Constant → "global_var"
  │       │     其他 → 按需扩展
  │       ├── signature_documentation.text → symbols.signature
  │       ├── enclosing_symbol → 记录，用于 occurrence 的 enclosing 映射
  │       └── Relationship[] → edges 表（is_implementation, is_reference 等）
  │            关系类型映射：
  │            is_implementation → type "implements" (后可改为 ops_bind 判断)
  │            is_reference → type "references"
  │            is_type_definition → type "type_of"
  │            is_definition → type "defines"
  │
  ├── Step 3: 处理 Occurrence[]
  │     for each occ in doc.occurrences:
  │       ├── symbol → 查 symbols 表得 symbol_id（或先缓存 dict）
  │       ├── symbol_roles 位掩码解析：
  │       │     Definition (0x1) → role=1
  │       │     Import (0x2) → role=2
  │       │     WriteAccess (0x4) → role=4
  │       │     ReadAccess (0x8) → role=8
  │       │     ForwardDefinition (0x40) → role=64
  │       ├── range → start_line, start_col, end_line, end_col
  │       │     typed_range: single_line_range → (line, start, end)
  │       │     typed_range: multi_line_range → (s_line, s_col, e_line, e_col)
  │       │     deprecated range[3]: → (line, start, end) 同行
  │       │     deprecated range[4]: → (s_line, s_col, e_line, e_col)
  │       ├── enclosing_range → ★关键！
  │       │     解析出 enclosing 的 line range
  │       │     查：哪个 SymbolInformation 的 definition range 覆盖此 enclosing range
  │       │     → 得到 enclosing_symbol_id
  │       │     写入 occurrences.enclosing_symbol_id
  │       │
  │       │  ★注意：enclosing_range 在 definition occurrence 上表示
  │       │    "整个定义的范围"，在 reference occurrence 上表示
  │       │    "引用落在哪个父 AST 节点内"。后者是调用图派生的核心。
  │       │
  │       └── 写入 occurrences 表
  │
  └── Step 4: 派生调用图边（在同一 Document 内完成）
        for each occ where symbol_roles & Reference 且 enclosing_symbol_id != NULL:
          edge_type = "calls"  (如果被引用符号是 function/method)
          edge_type = "references" (如果是 struct/field/macro 等)
          写入 edges(src=enclosing_symbol, dst=referenced_symbol, type, file, line)
```

### 4.2 Symbol 名称解析（SCIP 符号串 → 我们需要的元数据）

SCIP symbol 字符串格式（来自 proto 注释）：
```
<scheme> ' ' <package> ' ' <descriptor>+
例：scip clang c linux v6.12 ext4_file_operations#read_iter().
```

**解析规则**：
- scheme: `scip`（clang indexer 的 scheme）
- package: `clang c linux v6.12`（manager=`clang`, name=`c`, version=`linux v6.12`）
- descriptors: 串联解析，每个 descriptor 的 suffix 决定类型：
  - `/` → namespace
  - `#` → type (struct/class)
  - `.` → term (variable/field)
  - `()` → method, `(disambiguator).` → method
  - `!` → macro
  - `:` → meta
  - `[]` → type parameter
  - `()` → parameter

**我们需要从 symbol 串提取**：
1. **短名**（最后一个 descriptor 的 name）→ `symbols.name`
2. **kind**（最后一个 descriptor 的 suffix）→ 映射到我们的 kind
3. **enclosing symbol**（去掉最后一个 descriptor 的前缀串）→ 关联父符号

```
"scip clang c linux v6.12 ext4_file_operations#read_iter()."
  → name = "read_iter"
  → kind = "function"  (suffix 是 ().  = Method)
  → enclosing = "scip clang c linux v6.12 ext4_file_operations#"  (struct)
```

### 4.3 ops_bind 派生（核心差异化逻辑）

**触发条件**：一个 occurrence 的 enclosing symbol 的 kind 是 `global_var`，
且被引用符号的 kind 是 `function`，且 enclosing symbol 的名字匹配
`*_operations / *_ops / *_handler / *_table` 模式。

```c
// 伪代码
if (enclosing_sym.kind == "global_var" &&
    match_ops_pattern(enclosing_sym.name) &&
    referenced_sym.kind == "function") {
    // 从 symbol 串或 occurrence 上下文提取字段名
    // 方法1: 从 SCIP Relationship 中看是否有 is_implementation 关系
    // 方法2: 从符号名推断 (.read_iter = ext4_file_read_iter)
    //         → 字段名 = referenced_sym.name 如果能和 enclosing 的类型字段对上

    write_edge(enclosing_sym_id, referenced_sym_id,
               "ops_bind", file_id, line,
               metadata = json({"field_name": inferred_field_name}),
               confidence = 0.5);
}
```

**更精确的方法**（推荐）：利用 SCIP `SymbolInformation` 的 `Relationship`。
如果 `SymbolInformation` 的 kind 是 `Field`，且它的 `Relationship` 里
有 `is_definition=true` 关联到某个函数——这就是 ops_bind。

## 5. 性能关键点

| 环节 | 预估量级 | 性能策略 |
|---|---|---|
| protobuf 解析 | ~2-5GB 原始字节 | 流式逐 Document，不整体加载；C 侧零拷贝解析 |
| symbol 名称字典 | ~30 万条 | ingest 阶段维护内存 dict（scip_symbol→symbol_id），避免每条 occurrence 都查 SQLite |
| occurrence 写入 | ~1-2M 条 | 批量 INSERT（每 10k 条一次事务）；SQLite WAL 模式 |
| enclosing 匹配 | 同一 Document 内 | 每个 Document 处理完立即做 enclosing 匹配（Document 内所有定义 occurrence 构建 range→symbol_id 索引，然后给 reference occurrence 查 enclosing） |
| ops_bind 派生 | ~数千条 | 在 occurrence 处理阶段标记候选，Document 处理完后批量写入 |

**预估吞吐**：
- Python MVP（fs/ext4，~5k 文件）：数分钟可接受
- C 生产版（全量内核，30k 文件）：目标 < 10 分钟

## 6. 技术依赖与构建流程

### MVP（Python）

```bash
# 1. 从 scip.proto 生成 Python 绑定
pip install protobuf grpcio-tools
protoc --python_out=src/kgraph/scip scip.proto
# → 生成 src/kgraph/scip/scip_pb2.py

# 2. Python 解析脚本
# src/kgraph/ingest.py — 读 index.scip → 写 SQLite
```

### 生产（C）

```bash
# 1. 安装 protobuf-c
# macOS: brew install protobuf-c
# Linux: apt install libprotobuf-c-dev

# 2. 从 scip.proto 生成 C 绑定
protoc --c_out=src/libkgraph/scip scip.proto
# → 生成 scip.pb-c.h, scip.pb-c.c

# 3. C ingest 库
# src/libkgraph/ingest.c — 流式读 index.scip → 批量写 SQLite
# 编译: gcc -O2 -lprotobuf-c -lsqlite3 ingest.c scip.pb-c.c -o libkgraph.so
```

## 7. 需要你 Review 的决策点

| # | 决策 | 我的倾向 | 替代 | 备注 |
|---|---|---|---|---|
| R1 | MVP 解析器语言 | **Python protobuf 绑定**（快跑通） | 直接写 C | Python 先验证数据模型，C 后续替换 |
| R2 | 流式 vs 整体加载 | **小子系统整体加载，全量流式** | 全量也整体加载（要 ~5GB RAM） | fs/ext4 的 index.scip 大约 50-100MB，整体加载没问题 |
| R3 | symbol 名称解析 | **Python 正则解析 SCIP 符号串** | 用 SCIP 自带的 Symbol 类（proto 里有 `Symbol` message） | SCIP proto 有 `Symbol` message 结构（scheme + package + descriptors），但 scip-clang 输出的是字符串形式的 symbol，需要自己解析 |
| R4 | enclosing 匹配算法 | **同一 Document 内定义 occurrence 的 range → symbol 索引** | 跨 Document 全局匹配 | 定义和引用通常在同一 Document 内；跨 Document 的 enclosing 极少（C 头文件 inline 函数可能跨） |
| R5 | ops_bind 派生触发 | **基于 global_var 名称模式匹配 + kind 判断** | 基于 SymbolInformation.Relationship 的 is_implementation | 两种互补：Relationship 更精确但可能不全；名称模式覆盖面更广但需要维护 pattern 列表 |
| R6 | deprecated range 字段兼容 | **同时支持 typed_range 和 deprecated range** | 只支持 typed_range | scip-clang 当前版本可能还用 deprecated range；需要兼容两种编码 |

Sources:
- [SCIP Protocol - github.com/sourcegraph/scip](https://github.com/sourcegraph/scip)
- [SCIP Documentation - docs.sourcegraph.com](https://docs.sourcegraph.com)