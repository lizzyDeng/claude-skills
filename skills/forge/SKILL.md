---
name: forge
description: "Project-level harness. Wraps /fastship with roadmap management, benefit harvesting, and cross-feature feedback loops. Use for any project that needs goal tracking + outcome verification."
---

# /forge — 项目级 Harness

以终极目标（North Star）为驱动，管理 roadmap → 拆分 feature → 用 /fastship 交付 → 收益回收 → 反哺下一轮迭代。

核心原则：**产物约束优于流程约束。** 每个状态转换都由可验证的 artifact 把关；报告/JSON 文件不能自证，必须绑定当前 step provenance、hash 或外部 evidence。

## 状态机

每个 feature 遵循严格的状态流转，gate 强制执行：

```
draft ──→ planned ──→ in_progress ──→ shipped ──→ measuring ──→ concluded
  G1        G2           G3             G4          G5            G6
```

---

## 状态栏

每条回复前缀：

```
🔥 /forge | {project_name} | {active_feature} | {status}
```

---

## 命令

### `/forge init`

**前置**：当前项目没有 `project-roadmap/roadmap.json`

**流程**：

1. 引导用户定义 North Star（一句话描述项目终极目标）
2. 引导定义 1-3 个子目标（objective），每个包含：
   - `id`（如 `obj-1`）
   - `name`（子目标名）
   - `description`（详细描述）
   - `target_metric`（可量化的目标指标表达式）
3. 创建 `project-roadmap/` 目录
4. 写入 `project-roadmap/roadmap.json`
5. 生成 `project-roadmap/roadmap.md`（调用 `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py generate-view`）
6. 输出确认信息

### `/forge add <feature-name>`

**前置**：roadmap.json 存在

**流程**：

1. 引导用户确认 feature 关联的 objective（从 roadmap 中选择）
2. 生成 feature slug（kebab-case，如 `optimize-onboarding`）
3. 创建目录 `project-roadmap/features/<slug>/`
4. **引导式创建 metric.json**——逐一询问：
   - `metric_name`：这个 feature 成功的衡量指标是什么？
   - `event_name`：对应的埋点事件名？
   - `baseline`：当前基准值？（数字）
   - `target`：目标值？（数字）
   - `harvest_days`：上线后多少天回收数据？（默认 7）
   - `data_query_hint`：获取数据的方式提示？（SQL / 仪表盘 URL / CLI 命令，可选）
5. 写入 `project-roadmap/features/<slug>/metric.json`
6. **Gate 1 校验**：`python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py check-g1 <slug>`
   - 如果 metric.json 校验失败 → 提示修正，不加入 roadmap
7. 将 feature 加入 `roadmap.json`（status=draft）
8. 重新生成 roadmap.md

### `/forge plan <feature>`

**前置**：feature 状态为 `draft`（G1 已通过）

**流程**：

1. 激活 feature：`python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py activate <slug>`
2. 读取 `metric.json` + 从 roadmap 获取所属 objective 信息
3. 向用户展示上下文摘要：
   ```
   📋 Feature: <name>
   🎯 Objective: <objective_name> — <target_metric>
   📊 Metric: <metric_name> (baseline: <baseline> → target: <target>)
   📡 Event: <event_name>
   ```
4. **注入 fastship**：调用 `/fastship`，在 Phase 1 上下文中追加：
   - 使用 feature slug 作为 fastship session：`fastship start --session <slug> "<需求>"`
   - "本 feature 的交付 AC 必须包含：埋点事件 `<event_name>` 的实现"
   - "本 feature 服务于子目标：<objective_name> — <objective_description>"
5. fastship Phase 1 完成后（plan 落库 + grill 通过 + Codex Review structured gate + 用户 sign-off + trusted artifact hash）：
   - `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py transition <slug> planned`

### `/forge dev <feature>`

**前置**：feature 状态为 `planned`（G2 已通过）

🧠 **Context 预检**（软建议）：`activate` 命令会检查最近 2 分钟内是否执行过 `/compact`。未 compact → 打印建议但继续激活；建议在大需求前主动 `/compact` 保持 context 干净。

**流程**：

1. 激活 feature：`python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py activate <slug>`
2. `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py transition <slug> in_progress`
3. 调用 `/fastship` Phase 2+3（执行 + 验证）
4. fastship 流程自行运转，forge 不干预

### `/forge ship <feature>`

**前置**：feature 状态为 `in_progress`，fastship 已完成（test_passed + e2e_executed + e2e_gate_passed + e2e_result_hash + loop pass + knowledge_acknowledged + orchestrator step=done）

**流程**：

1. `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py transition <slug> shipped`
   - Gate 4 自动检查当前 worktree 中该 feature slug 对应的 fastship session state + orchestrator state
   - 不读取旧 `.claude/.ship-verify-state.json`，禁止 legacy fallback
   - 必须验证 E2E 报告 trusted artifact，并确认报告引用 `e2e_result_hash`
   - Gate 5 自动从 shipped 转入 measuring
   - **自动 worktree 清理**：transition 成功后跑一次全量 managed-orphan sweep（仅清理「干净 + 已真合并进 trunk」的 worktree）。你正在 ship 的那个 worktree 因 git 限制无法自删（保留为 `kept-current`），会在下次交付或 `/forge sweep-worktrees` 时回收。清理失败绝不阻断 transition。
2. 如果通过 → 输出：
   ```
   ✅ <feature_name> 已上线！
   📊 收益回收日期：<harvest_due>
   📡 届时运行 /forge harvest <slug> 来验证效果
   ```
3. 如果被 Gate 4 拦截 → 输出缺失项，引导用户回到 fastship 完成验证

### `/forge harvest <feature>`

**前置**：feature 状态为 `measuring`

**流程**：

1. 读取 `metric.json`，展示：
   ```
   📊 Metric: <metric_name>
   📈 Baseline: <baseline> → Target: <target>
   💡 数据获取方式: <data_query_hint>
   ```
2. 请用户提供 `actual` 值（实际数据）
3. 计算结果并展示：
   ```
   Baseline: <baseline>
   Target:   <target>
   Actual:   <actual>
   Delta:    <actual - baseline> (<percentage>%)
   ```
4. 引导用户判定 verdict：
   - `achieved`：actual >= target
   - `partial`：actual > baseline 但 < target
   - `missed`：actual <= baseline
5. 引导用户填写 notes（简要分析原因）
6. 引导用户选择 next_action：
   - `done` → feature 完成，子目标进度更新
   - `iterate` → 基于当前结果开始新一轮迭代
   - `pivot` → 子目标需要重新评估
7. 写入 evidence 原始文件（如 `project-roadmap/features/<slug>/evidence.json`），计算 SHA-256
8. 写入 `project-roadmap/features/<slug>/harvest.json`，其中必须包含：
   ```json
   {
     "evidence": {
       "source": "warehouse query / dashboard export / CLI output",
       "collected_at": "2026-05-28T10:00:00",
       "raw_path": "evidence.json",
       "raw_sha256": "<evidence file sha256>"
     }
   }
   ```
9. `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py transition <slug> concluded`
10. **如果 next_action == "iterate"**：
   - 自动创建新 feature（slug: `<original>-v2`）
   - 继承 metric.json（baseline 更新为本次 actual，target 不变）
   - 设置 `previous_feature` 指向当前 feature
   - 设置当前 feature 的 `next_feature` 指向新 feature
   - 提示用户运行 `/forge plan <new-slug>` 继续
11. 重新生成 roadmap.md

### `/forge status`

**前置**：无

**流程**：

1. `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py status`
2. 输出全局 roadmap 状态 + 到期提醒 + 可清理的孤儿 worktree 计数（如有 → 提示 `/forge sweep-worktrees`）

### `/forge sweep-worktrees [--dry-run]`

**前置**：无

清理所有交付完成的孤儿 worktree，杜绝它们占用磁盘/内存。

**流程**：

1. `python3 ${CLAUDE_PLUGIN_ROOT}/skills/forge/hooks/forge_gate.py sweep-worktrees [--dry-run]`
2. 扫描 `<main-worktree>/.claude/worktrees/` 下的所有 worktree，逐个判定并输出 removed/kept 摘要
3. 额外跑 `git worktree prune` 清理「工作目录已被手动删除」的失联 admin 记录（绝不丢提交）

**安全契约（绝不丢失代码）**：只删除「工作区干净（`git status` 为空）+ 分支已真合并进 trunk（origin/main…，用 `git merge-base --is-ancestor` 判定）」的 worktree。脏的 / 未合并的 / 当前所在的 / 主工作区 / 不在 `.claude/worktrees/` 下的一律保留。squash-merge 用 ancestor 判定检测不到，保守保留，请手动处理。删除 worktree 不带 `--force`（git 拒删脏工作区）；删分支用 `git branch -d`（git 拒删未合并分支）—— 三层独立兜底。

> 从主工作区运行最彻底：git 不允许删除你当前所在的 worktree。

---

### `/forge dashboard`

可视化进度页面（只读 Web UI），看 forge 目标 + 每个 feature 的 fastship 执行进度。

**前置**：无

**流程**：

1. 启动本地 dashboard（零依赖 stdlib，端口 7575，每 5s 自动刷新）：

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/skills/forge/forge-dashboard            # serve on http://127.0.0.1:7575
   ${CLAUDE_PLUGIN_ROOT}/skills/forge/forge-dashboard --port N   # 自定义端口
   ${CLAUDE_PLUGIN_ROOT}/skills/forge/forge-dashboard --once     # 打印 JSON 快照后退出（CI/脚本用）
   ```

   > 路径用 `${CLAUDE_PLUGIN_ROOT}`（插件安装后由 Claude Code 注入）。源/dev 调试（非插件模式）时直接运行源树 `skills/forge/forge-dashboard`。

2. 页面层级：**North Star → objective 卡片**（总体进度条 + status chips + **剩余 TODO 列表**）**→ feature 行**（status 徽章 + 进度条 + fastship 18 步执行条 + 指标 baseline→target→actual）。

**它读什么**（全只读）：`project-roadmap/roadmap.json` + 每个 feature 的 `metric.json`/`harvest.json` + `.claude/forge-state/` gate 缓存；并扫描当前 repo 全部 worktree 的 fastship session（`<git-common-dir>/fastship` + `worktrees/*/fastship`），按 slug 把 session 关联到 feature。一个大目标拆成多个 feature（如 obj-4 的 F1-F7）时，objective 卡片给出**总体进展 + TODO**。

### `/forge track` / `/forge analyze`（持续指标追踪）

把一次性 harvest 升级为**连续、方向感知、证据可复验**的追踪。

- `/forge track <feature>`（或 `--objective <id>`）：经 `.claude/metrics.project.json` 声明的 resolver 取「数字 + 证据」，用 metric.json/objective 的 curate 定义 enrich（baseline/target/`direction`），evidence sha256 校验后 append 进 `metric-history.jsonl`；方向感知 regression（`up` 跌破 / `down` 升破）。
- `/forge analyze <feature>`：先复验 history evidence（被改即拒），产 `analysis.json`（trend/slope/方向感知 projection/**provenance footer**）；深度对抗归因走 **dynamic workflow** `skills/forge/workflows/analyze.workflow.js`。
- **🔴 forge 自身零 SQL、零 shell 注入**：只 `shlex.split` + argv 调 resolver（取数在消费方实现），占位符白名单校验、`..`/`/` 拒。resolver 只产 `{metric_id,value,as_of,evidence}`，定义人工 curate（"curate, don't auto-generate"）。详见 `.claude/commands/forge.md`。

---

## 🚫 Red Flags（禁止行为）

1. 跳过 `/forge add` 直接进入 `/forge plan` — metric 未定义就开发 = 无法回收
2. 手动编辑 `roadmap.json` 或 `.forge-state.json` — hook 会拦截
3. 在 `/forge harvest` 中捏造 actual 数据或 evidence hash — 违背 forge 的核心价值，Gate 6 必须阻断
4. 跳过 `/forge ship` 直接标记 concluded — Gate 4/5/6 联合拦截
5. 不做 harvest 就开始下一个 feature — measuring 状态的 feature 会在每次调用时提醒
6. 修改 fastship 的 ship_verify_gate.py 来适配 forge — forge 单向依赖 fastship，不改 fastship
7. 依赖旧 fastship state 或单个布尔字段推进 forge 状态 — G2/G4 必须读当前 worktree 的 `fastship/sessions/<slug>/gate.json` + `orchestrator.json`，并校验 trusted artifact hash
8. 用 Forge 激活 feature 时清空其他 feature 的 fastship state — 切换只允许更新当前 session 指针，不能 reset 另一个需求的状态
