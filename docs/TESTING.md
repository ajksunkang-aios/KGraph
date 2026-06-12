# KGraph 测试设计文档

## 目录结构

```
tests/
├── conftest.py                          # 共享 fixture (sys.path、合成 SCIP、populated_store、mcp_server)
├── unit/                                # 单元测试 — 纯函数/单模块，零外部依赖
│   ├── test_symbol_name.py              # parse_scip_symbol 及子函数
│   ├── test_scip_parser_helpers.py      # _match_ops_pattern / range提取 / protobuf wire helpers
│   ├── test_models.py                   # 枚举值、映射表、dataclass 默认值
│   ├── test_source_reader.py            # read_source_range / read_source_with_lineno
│   └── test_mcp_helpers.py              # _format_symbol_list / _format_edge_list
├── integration/                         # 集成测试 — 合成 SCIP 数据，无需真实内核
│   ├── test_scip_pipeline.py            # index.scip → parser → store 全链路 (41 tests)
│   └── test_mcp_server.py               # MCP tools → kgraph.db (31 tests)
└── real/                                # 真实案例 — 需要真实内核 index.scip
    └── ingest_real.py                   # 手动脚本，非 pytest
```

## 运行方式

```bash
# 全部测试
.venv/bin/python -m pytest tests/ -v

# 仅单元测试
.venv/bin/python -m pytest tests/unit/ -v

# 仅集成测试
.venv/bin/python -m pytest tests/integration/ -v

# 真实案例（手动）
cd /path/to/linux && .venv/bin/python tests/real/ingest_real.py
```

---

## 单元测试设计

### 1. `test_symbol_name.py` — SCIP Symbol 字符串解析器

> 源文件：`src/parser/symbol_name.py`
> 特点：全部纯函数，最适合 `@pytest.mark.parametrize`

| 测试目标 | 测试点 | parametrize |
|----------|--------|:-----------:|
| `parse_scip_symbol` | 标准函数 `ext4_file_read_iter().` → short_name, kind=function | ✅ |
| | 结构体 `ext4_file_operations#` → kind=struct | ✅ |
| | 结构体字段 `ext4_file_operations#read_iter()` → kind=function, enclosing 正确 | ✅ |
| | Term 描述符 `ext4_file_operations#read_iter.` → kind=global_var | ✅ |
| | local symbol `local foo` → kind=variable | ✅ |
| | 空字符串 → `{}` | ✅ |
| | 只有 scheme+package 无描述符 → short_name="" | ✅ |
| | Macro `kmalloc!` → kind=macro | ✅ |
| | 带反引号的转义标识符 | ✅ |
| `_parse_descriptors` | 单描述符、多描述符嵌套、Method 带 disambiguator `(+1).` | ✅ |
| | TypeParameter `[T]`、Parameter `(name)` | ✅ |
| | 尾部无 suffix、malformed 开括号 | ✅ |
| `_extract_name` | 简单标识符、转义反引号、未闭合反引号、特殊字符 `+-$` | ✅ |
| `_is_identifier_char` | 字母/数字/下划线 → True，`/ # .` → False | ✅ |
| `_reconstruct_descriptors` | 空列表 → ""、含转义名称时加反引号 | ✅ |
| **round-trip** | `parse_scip_symbol` → `enclosing_symbol` 可再次被 `parse_scip_symbol` 解析 | ✅ |

### 2. `test_scip_parser_helpers.py` — Parser 辅助函数

> 源文件：`src/parser/scip_parser.py`（模块级函数）
> 特点：全部纯函数，无需 protobuf 对象

| 测试目标 | 测试点 | parametrize |
|----------|--------|:-----------:|
| `_match_ops_pattern` | `_operations` / `_ops` / `_handler` / `_table` → True | ✅ |
| | `_callbacks` / `_hooks` / `_methods` / `_funcs` / `_fops` → True | ✅ |
| | 普通函数名 → False、空字符串 → False、大小写不敏感 | ✅ |
| | 部分匹配在中间而非末尾 → False（如 `operations_foo`） | ✅ |
| `_language_enum_to_str` | `C` / `CPP` / `C_CPP` / `OBJECTIVE_C` → `"C"` | ✅ |
| | 未知语言原样返回、空字符串 → `""` | ✅ |
| `_get_symbol_kind` | 在 symbol_map 中 → 返回 map 的 kind | ✅ |
| | 不在 map → 回退到 parse_scip_symbol 解析 | ✅ |
| | 都没有 → DEFAULT_SYMBOL_KIND | ✅ |
| `_get_symbol_name` | 在 map 中 → 返回 map 的 name | ✅ |
| | 不在 map → 回退到 parse_scip_symbol 的 short_name | ✅ |
| | 都没有 → 返回原始 scip_symbol 字符串 | ✅ |
| `_json_metadata` | 正常 dict → JSON 字符串、空 dict → `"{}"` | ✅ |
| `_read_varint32` | 单字节 (<128)、多字节、零值、截断 buf → ValueError | ✅ |
| `_read_tag` | field_number + wire_type 正确解析 | ✅ |
| `_skip_field` | wire_type 0/1/2/5 → 正确跳过，未知 wire_type → ValueError | ✅ |

### 3. `test_models.py` — 数据模型验证

> 源文件：`src/parser/models.py`
> 特点：常量/枚举验证，无依赖

| 测试目标 | 测试点 |
|----------|--------|
| `SymbolKind` | 每个常量是正确的字符串值（`FUNCTION == "function"`） |
| `EdgeType` | 每个常量是正确的字符串值，`OPS_BIND == "ops_bind"` |
| `SymbolRole` | 位掩码值正确：`DEFINITION=0x1`、`READ_ACCESS=0x8` |
| | 组合：`DEFINITION | READ_ACCESS == 0x9`、`bool(roles & DEFINITION)` |
| `SCIP_KIND_TO_SYMBOL_KIND` | 已知 SCIP kind int (17→function, 49→struct, 25→macro) 映射正确 |
| | C 特殊映射：7→struct（不是 class） |
| | 所有 value 都是合法的 `SymbolKind` 值 |
| Dataclass 默认值 | `FileRecord()` 默认 `language="C"` |
| | `EdgeRecord()` 默认 `weight=1, confidence=1.0` |
| | `IngestBatch()` 默认空列表 |
| | `SymbolRecord` 必填字段 `scip_symbol/name/kind` |

### 4. `test_source_reader.py` — 源码读取器

> 源文件：`mcp/source_reader.py`
> 依赖：tmp_path 创建假文件

| 测试目标 | 测试点 |
|----------|--------|
| `read_source_range` | 正常范围读取（0-based 行号） |
| | `start_line < 0` → None |
| | 文件不存在 → None |
| | `start_line` 超出文件行数 → None |
| | `context > 0` 扩展范围，clamped 到 `[0, len)` |
| | 单行范围 (`start == end`) |
| `read_source_with_lineno` | 行号 1-based、6 列右对齐、tab 分隔 |
| | `read_source_range` 返回 None 时传播 None |
| | context 调整起始行号正确 |

### 5. `test_mcp_helpers.py` — MCP 格式化函数

> 源文件：`mcp/server.py`（`_format_symbol_list` / `_format_edge_list` / `_resolve_one`）
> 特点：`_format_*` 纯函数；`_resolve_one` 需要 mock store

| 测试目标 | 测试点 | parametrize |
|----------|--------|:-----------:|
| `_format_symbol_list` | 正常结果格式化、`def_start_line=-1` 显示 `(external)` | ✅ |
| | `include_scip=True` 添加 `id:` 行、有/无 signature | ✅ |
| | 空列表 → `"Found 0 symbol(s):"` | ✅ |
| `_format_edge_list` | depth>1 缩进、`ops_bind` 标记 `[ops_bind]` | ✅ |
| | direction 字符串在标题中、`line=None` 显示 `:?` | ✅ |
| `_resolve_one` | 有非 external 定义 → 返回其 scip_symbol | ✅ |
| | 全部 external → 返回第一个的 scip_symbol | ✅ |
| | 无候选 → None | ✅ |
| | `prefer_kind` 过滤无结果时 retry 无 kind 过滤 | ✅ |

---

## 集成测试设计（已完成）

### `test_scip_pipeline.py` — SCIP → Store 全链路 (41 tests)

**Parser 输出验证 (TestParserOutput)**：
- 批次数量和类型（metadata / document / external_symbols）
- Symbol 名称、kind 映射正确性
- Occurrence 数量
- calls / ops_bind / implements / type_of 边的派生
- ops_bind 的 confidence=0.5 和 metadata JSON
- External symbols 标记

**Store 查询验证 (TestStoreQueries)**：
- `search_symbols`：精确/模糊/带 kind 过滤/无结果
- `get_symbol`：精确名/kind 过滤/不存在
- `find_callers` / `find_callees`：直接调用/多层遍历/ops_bind 边
- `find_ops_impls`：field_name 匹配/struct_type 过滤
- `find_references`：definition + reference / enclosing 信息
- `find_type_definition`：type_of 边遍历
- `get_struct_layout`：contains 边 → struct 字段
- `get_neighborhood`：depth=1/2、summary 模式
- `call_path`：有路径/无路径
- `get_metadata`：project_root / tool_name / total_symbols
- `get_definition_location`：正常/external

### `test_mcp_server.py` — MCP Tools 集成 (31 tests)

- 全部 12 个 MCP tool 函数的正常路径验证
- not-found 路径验证（`"No symbol named"` / `"No symbols found"`）
- `get_function_body`：从 tmpdir 读取假源文件
- `get_struct_layout`：contains 边显示字段
- `call_path`：源/目标不存在的情况

---

## 合成 Benchmark 数据

`conftest.py` 中的 `build_synthetic_scip_index()` 构建了一个模拟 ext4 VFS 场景：

```
3 个 Document:
  fs/ext4/file.c     — ext4_file_operations (struct, ops table)
                        ext4_file_read_iter / ext4_file_write_iter / ext4_file_open (function)
                        read_iter / write_iter / open (field)
  fs/read_write.c    — vfs_read / vfs_write (function, direct calls)
  include/linux/fs.h — file_operations (struct), loff_t (typedef)

2 个 External symbol:
  sys_read / __fdget_pos

边类型覆盖:
  calls       — vfs_read → ext4_file_read_iter
  ops_bind    — ext4_file_operations → ext4_file_read_iter (×3)
  implements  — ext4_file_operations → file_operations
  type_of     — read_iter → ext4_file_read_iter
  contains    — ext4_file_operations → {read_iter, write_iter, open} (parser Step 6)
```

---

## Fixture 依赖关系

```
scip_index_bytes (session)
  └─ scip_file (function)
       └─ populated_store (function) ─── SQLiteStore 实例，已 finalize
            │
            ├─ project_root (function) ─── 假内核源文件树
            │    └─ mcp_server (function) ─── 加载 mcp/server.py，注入 test DB + source root
            │
            └─ SQLiteStore 单元测试: 使用 tmp_path 独立创建 :memory: DB
```

---

## 已发现的 Pipeline 缺陷

| 缺陷 | 影响 | 状态 |
|------|------|------|
| **跨文档边丢失**：edges 在同一 batch 中写入，引用其他 Document 的 symbol 时目标尚未入库，边被静默丢弃 | type_of / implements 等跨文档关系丢失 | 待修复 |
| **contains 边缺失**：parser 已解析 `enclosing_symbol` 但未派生 `contains` 边 | `get_struct_layout` 无字段 | ✅ 已修复 (Step 6) |
