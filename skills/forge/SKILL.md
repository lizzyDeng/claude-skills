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
5. 生成 `project-roadmap/roadmap.md`（调用 `python3 .claude/hooks/forge_gate.py generate-view`）
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
6. **Gate 1 校验**：`python3 .claude/hooks/forge_gate.py check-g1 <slug>`
   - 如果 metric.json 校验失败 → 提示修正，不加入 roadmap
7. 将 feature 加入 `roadmap.json`（status=draft）
8. 重新生成 roadmap.md

### `/forge plan <feature>`

**前置**：feature 状态为 `draft`（G1 已通过）

**流程**：

1. 激活 feature：`python3 .claude/hooks/forge_gate.py activate <slug>`
2. 读取 `metric.json` + 从 roadmap 获取所属 objective 信息
3. 向用户展示上下文摘要：
   ```
   📋 Feature: <name>
   🎯 Objective: <objective_name> — <target_metric>
   📊 Metric: <metric_name> (baseline: <baseline> → target: <target>)
   📡 Event: <event_name>
   ```
4. **注入 fastship**：调用 `/fastship`，在 Phase 1 上下文中追加：
   - "本 feature 的交付 AC 必须包含：埋点事件 `<event_name>` 的实现"
   - "本 feature 服务于子目标：<objective_name> — <objective_description>"
5. fastship Phase 1 完成后（plan 落库 + grill 通过 + Codex Review structured gate + 用户 sign-off + trusted artifact hash）：
   - `python3 .claude/hooks/forge_gate.py transition <slug> planned`

### `/forge dev <feature>`

**前置**：feature 状态为 `planned`（G2 已通过）

🧠 **Context 预检**（机械强制）：`activate` 命令会检查最近 2 分钟内是否执行过 `/compact`。未 compact → BLOCK。先 `/compact` 再 activate。

**流程**：

1. 激活 feature：`python3 .claude/hooks/forge_gate.py activate <slug>`
2. `python3 .claude/hooks/forge_gate.py transition <slug> in_progress`
3. 调用 `/fastship` Phase 2+3（执行 + 验证）
4. fastship 流程自行运转，forge 不干预

### `/forge ship <feature>`

**前置**：feature 状态为 `in_progress`，fastship 已完成（test_passed + e2e_executed + e2e_gate_passed + e2e_result_hash + loop pass + knowledge_acknowledged + orchestrator step=done）

**流程**：

1. `python3 .claude/hooks/forge_gate.py transition <slug> shipped`
   - Gate 4 自动检查当前 worktree 的 fastship gate state + orchestrator state
   - 不读取旧 `.claude/.ship-verify-state.json`，禁止 legacy fallback
   - 必须验证 E2E 报告 trusted artifact，并确认报告引用 `e2e_result_hash`
   - Gate 5 自动从 shipped 转入 measuring
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
9. `python3 .claude/hooks/forge_gate.py transition <slug> concluded`
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

1. `python3 .claude/hooks/forge_gate.py status`
2. 输出全局 roadmap 状态 + 到期提醒

---

## 🚫 Red Flags（禁止行为）

1. 跳过 `/forge add` 直接进入 `/forge plan` — metric 未定义就开发 = 无法回收
2. 手动编辑 `roadmap.json` 或 `.forge-state.json` — hook 会拦截
3. 在 `/forge harvest` 中捏造 actual 数据或 evidence hash — 违背 forge 的核心价值，Gate 6 必须阻断
4. 跳过 `/forge ship` 直接标记 concluded — Gate 4/5/6 联合拦截
5. 不做 harvest 就开始下一个 feature — measuring 状态的 feature 会在每次调用时提醒
6. 修改 fastship 的 ship_verify_gate.py 来适配 forge — forge 单向依赖 fastship，不改 fastship
7. 依赖旧 fastship state 或单个布尔字段推进 forge 状态 — G2/G4 必须读当前 worktree 的 gate.json + orchestrator.json，并校验 trusted artifact hash
