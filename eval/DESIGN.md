# KGraph Eval — Design (极简版)

> **一个 CLI，一个报告。**
> 用户已用 `kgraph init` / `kgraph sync` 构建并维护了 `<linux>/.kgraph/kgraph.db`。
> eval 的唯一职责：**消费这个已存在的 db，跑 KBench A/B，产出报告。**
> 不重新构建 db，不重复 KGraph 自己的 CLI。

---

## 1. 定位与边界

| 是 | 不是 |
|---|---|
| 消费已构建的 `<linux>/.kgraph/kgraph.db` 跑评估 | ❌ 不再包一层 build_sut（用户用 `kgraph init` 构建） |
| 一个 CLI 命令跑完全流程并出报告 | ❌ 不拆 build-sut/run/report/list 多子命令 |
| 薄调 KBench（`runner.run` + `render`），不重写 task/scorer/agent loop | ❌ 不产 GT（非循环红线，GT 来自 KBench） |
| 首期仅 retrieval v1 闭环（KBench 已成熟 8 任务，pin v7.1-rc7） | ❌ 首期不做 bugfix/backport（KBench harness 未就绪） |

**核心问题**：接入 KGraph 后，agent 在内核检索任务上是否更准且更省？
- direct-retrieval（grep 本就能解）→ 差距在**成本**
- compiler-aware（ops_bind / 间接调用 / 宏展开）→ 差距在**准确率**

---

## 2. 使用方式（就一条命令）

```bash
# 前提：用户已在内核树里构建过 db
cd /path/to/linux && kgraph init .    # 或 kgraph sync 增量更新

# 跑 eval（在 KGraph 仓里）
python -m kgraph_eval --kernel /path/to/linux

# → 自动发现 /path/to/linux/.kgraph/kgraph.db
# → 拉 KBench(钉 commit) → 跑 A-baseline vs B-kgraph (reps=3)
# → 报告落到 eval/reports/<run_id>.{md,json}，摘要打印到 stdout
```

**选项**（都有默认值，零配置可跑）：
| flag | 默认 | 说明 |
|---|---|---|
| `--kernel` | （必填） | 内核源树根；db 默认取 `<kernel>/.kgraph/kgraph.db` |
| `--db` | `<kernel>/.kgraph/kgraph.db` | 显式指定 db（绕过默认发现） |
| `--models` | 环境默认模型 | 逗号分隔；**传多个才出模型矩阵** |
| `--arms` | `A-baseline,B-kgraph` | 默认两臂跑 A/B |
| `--reps` | `3` | 每任务×臂重复数（中位数+IQR） |
| `--kgraph-repo` | 当前 KGraph 仓 | B-arm 需 import 它的 `mcp/server.py` |
| `--relax-pin` | off | 跳过 kernel-commit vs KBench-pin 一致性校验（本地探索用；报告标注 non-pinned） |
| `--max-turns` | `12` | agent 循环上限 |

---

## 3. 与 KBench 的契约（唯一集成点）

KBench 已内置对 KGraph 的 B-arm 支持（`kbench/harness/arms.py:KGraphTools`
直接 `import` KGraph 的 `mcp/server.py`，把 13 个工具当函数调用，靠
`KGRAPH_DB` / `KGRAPH_ROOT` 环境变量耦合）。KGraph 侧只要保证 `mcp/server.py`
工具函数签名稳定 + env 解析稳定，B-arm 即可用。

KGraph/eval 调 KBench 的接口：
```python
# 库函数路线（绕过 cli.py 硬编码的 results_dir）：
from kbench.harness import runner
run_id = runner.run(tasks, arms, reps, model,
                    repo_root, kgraph_repo, db_path,
                    results_dir=<eval/results>,   # ← 直写本仓，免跨仓拷贝
                    max_turns)
from kbench.report import render
md, js = render.render(run_id, <eval/results>, <eval/reports>)
```
> KBench `cli.py` 把 `results_dir` 钉在 KBench 仓内，但 `runner.run()` 接受参数，
> 故直接调 runner，让产物落进 `eval/results/`。

---

## 4. 流程（一个函数串到底）

```
cli.main(kernel)
  │
  ├─ 1. 发现 db: <kernel>/.kgraph/kgraph.db  (不存在 → 报错提示 `kgraph init`)
  ├─ 2. pin 校验: kernel HEAD == KBench retrieval manifest.commit (v7.1-rc7)
  │      不一致 → 警告；--relax-pin 跳过(报告标 non-pinned)
  ├─ 3. clone KBench @ pin-commit (缓存于 eval/datasets/, depth 1)
  ├─ 4. for model in --models:
  │      runner.run(tasks=KBench tasks/retrieval, arms, reps, model,
  │                 repo_root=kernel, kgraph_repo, db_path,
  │                 results_dir=eval/results, max_turns)
  │      → results/<run_id>/retrieval/*.json
  ├─ 5. render: KBench render.render() (A/B+tier+per-task) + KGraph 增量(Δ增益/模型矩阵)
  │      → eval/reports/<eval_run_id>.{md,json}
  └─ 6. 打印摘要表到 stdout
```

---

## 5. 目录结构（最小粒度，3 个文件）

```
eval/
  DESIGN.md                 # 本文件
  README.md                 # 怎么跑、怎么看结果
  pyproject.toml            # 独立子项目；deps: anthropic + KBench 运行时(protobuf/mcp)
  .env.example              # ANTHROPIC_BASE_URL / AUTH_TOKEN / MODEL 模板

  kgraph_eval/
    __init__.py
    cli.py                  # 入口 + 全流程编排（步骤1-6）
    kbench.py               # KBench 适配：clone(钉commit) + preflight + 调 runner.run
    report.py               # 增量渲染：Δ增益表 + 模型矩阵（复用 KBench render 基础输出）

  results/                  # KBench runner.run 直写（.gitignore）
  reports/                  # 最终报告（.gitignore；关键摘要可选 commit）
  datasets/                 # KBench clone 缓存（.gitignore）
```

**为什么独立 pyproject 而非塞进 KGraph 核心 requirements**：
eval 依赖 `anthropic` SDK（驱动 agent loop），是重运行时依赖；KGraph 核心
（scip-clang + sqlite）不该背它。独立子项目 → `pip install -e ./eval` 按需装。

**与现有仓的关系**：复用 `src/cli/init_cmd.py`（db 构建本就是用户的事，eval 不调它，
只在 db 缺失时提示用户去调）；**不动** `mcp/server.py`（KBench 直接 import，签名稳定是前提）。

---

## 6. 报告产物（用户选定：A/B + 模型矩阵）

### 6.1 A/B 对比报告
KBench `render.render()` 已产出 `arm × tier × per-task` 的 F1+成本表，直接用。
KGraph 侧增量补一个 **增益表**：每 `(tier)` 一行，`ΔF1 / Δtokens(省%) / Δcalls(省%)`
（B 相对 A）。直接凸显护城河价值。

### 6.2 模型矩阵交叉表
仅当 `--models` 传入 ≥2 个模型时渲染：`模型(rows) × arm(cols)`，每格 `F1 / tokens`。
证明 KGraph 增益跨模型泛化（Claude/GLM/DeepSeek 都受益），而非单模型过拟合。
单模型时跳过此节，报告更轻。

---

## 7. CI 集成

新增 `.github/workflows/eval-retrieval.yml`（首期 manual dispatch）。
**eval 本身不管 build**，db 构建步骤在 workflow 里裸写（复用 build-probe 链路）：

```yaml
on: { workflow_dispatch: }
jobs:
  eval:
    runs-on: ubuntu-latest        # scip-clang 是 Linux x86-64 only
    timeout-minutes: 120
    steps:
      - checkout KGraph
      - setup Python 3.10
      - pip install -r requirements-dev.txt && pip install -e ./eval
      - clone torvalds/linux (shallow)
      - make CC=clang LLVM=1 defconfig && gen_compile_commands.py   # build
      - python src/cli/init_cmd.py ./linux --force                   # index → db (复用现有)
      - python -m kgraph_eval --kernel ./linux --models glm-5,claude-sonnet
      - upload-artifact: eval/results eval/reports
```

> db 构建（`kgraph init`）是 CI yaml 的职责，**不是 eval CLI 的职责**——
> 这正是"build_sut 意义不大"的落地：真实用户自己 init，CI 也直接 init，eval 只消费。

**缓存**：actions/cache 按 kernel commit 缓存 `compile_commands.json` + `kgraph.db`，
仅 KGraph 代码改动时重建。**成本红线**：免费 runner 16GB 可能 OOM，加 `--tus N` 子集回退
（runner 按 TU 维度可裁剪），跳过数 `log()` 出来。

---

## 8. 严谨性（继承 KBench DESIGN §12 红线）

1. **N≥3 reps，报中位数 + IQR**（不妥协）。
2. **臂公平**：复用 KBench 工具中立 system prompt（`agent.py:SYSTEM_PROMPT`），
   不改 prompt 偏袒 B-arm。
3. **GT 非循环**：GT 来自 KBench（编译器解析集合，独立核验），KGraph/eval 只消费。
   pin 校验保证 db 构建自与 task 相同 commit——非循环在 KGraph 侧的落地。
4. **可复现**：每份报告记录 `kgraph_commit` + `kbench_pin` + `kernel_commit` + 模型版本。
5. **诚实分层**：direct / compiler-aware 分层报告（KBench `tier` 字段直接用）。

---

## 9. 落地分期

**Phase 1（本轮，retrieval v1 闭环）**
1. `eval/` 目录 + `pyproject.toml` + `.env.example` + 更新根 `.gitignore`
2. `kgraph_eval/kbench.py`：clone KBench(钉 commit) + preflight + 调 `runner.run(results_dir=eval/results)`
3. `kgraph_eval/report.py`：KBench render 基础输出 + Δ增益表 + 模型矩阵
4. `kgraph_eval/cli.py`：`python -m kgraph_eval --kernel ...` 一条命令串全流程
5. `README.md`
6. `.github/workflows/eval-retrieval.yml`（manual dispatch）

**Phase 2（KBench 垂域成熟后接入）**
- bugfix / backport：复用本编排层，只换 `runner.run` 的 tasks + scorer。

**Phase 3（KGraph 专属任务集贡献回 KBench）**
- config-gated 可达性、多跳 ops 链路等护城河任务，在 KBench 仓策展，eval 只是首批消费者。

---

## 10. 不做（防越界）

- 不在 eval 重写 agent loop / scorer / 聚合核心（KBench 的活）
- 不包 build_sut（用户用 `kgraph init`；CI 裸写 init 步骤）
- 不 git-submodule KBench（KBench DESIGN §10 明令）
- 不产 GT（非循环红线）
- 首期不做 bugfix/backport（KBench harness 未就绪）
