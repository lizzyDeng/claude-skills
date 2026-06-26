# fastship Phase 3 重构：AC 驱动的旅程验证

- 日期：2026-06-26
- 状态：设计已确认（待 spec 复核）
- 涉及：`skills/fastship/orchestrator.py`、`skills/fastship/e2e/`、`skills/fastship/hooks/ship_verify_gate.py`、新增驱动适配器 + 裁判 + 报告渲染器

---

## 1. 背景与问题（读代码后的事实）

今天 fastship Phase 3（3.0–3.6）的验证：

- **3.2 runner** = `e2e/e2e_runner.py` 用 `requests` 库纯 HTTP 打 API（零浏览器、零截图，仅额外处理 SSE 流）。
- **3.4 gate** = `e2e/e2e_gate.py` 只算三件事：`total_turns >= min_turns`（默认 `10`，`orchestrator.py` `E2E_MIN_TURNS = 10`）、空响应率 `< 0.5`、错误率 `< 0.5`。
- gate 状态字段（`hooks/ship_verify_gate.py` empty_state）：`test_passed` / `e2e_executed` / `e2e_result_hash` / `e2e_result_turns` / `e2e_gate_passed` / `loop_count` / `knowledge_acknowledged`。

### 根因：门禁只会数数

确定性 gate 读不懂"需求满没满足"，于是退而求其次去数一个能数的东西——轮数。`min_turns=10` 这堵墙就是这么来的：纯 UI feature 没有 HTTP turn，凑不够 10 轮，只能被迫标 N/A 手工塞截图。

更深的问题：Phase 1 的 plan 里**已经有 `ac_mapping`**（`PLAN_MAPPING_FIELD = "ac_mapping"`，每条 `{ac_id, tasks[], e2e[]}`，AC 本体是 `observable_ac: [{id, assertion}]`），但 **Phase 3 执行期没有一行代码去走这个映射、逐条验 AC 是否被真的观察到**。它只在 1.4 / Codex Review 读 plan 文本时被"声明式"检查一次，真正跑的时候这层语义就丢了。

> 这就是"e2e 完全是冒烟、没把需求串联起来"的精确出处：**验证单元是 turn，不是 AC。**

---

## 2. 目标 / 非目标

### 目标

1. **验证单元从 turn 换成 AC**：每条 P0/P1 AC 一路串到底——派生旅程 → 真实执行 → 逐条判定 → 结构 gate。
2. **按需求表面选验证层级**：UI 走浏览器旅程（语义定位 + 截图），API 走 HTTP 断言，data 走持久化查询。
3. **裁判替代数数**：用「对着 AC 逐条引用证据的对抗裁判」替代 `min_turns` / 错误率 gate。
4. **永远产出 HTML 测试报告**：含每条 AC 的测试路径 + 截图 + 裁判结论，作为人类确认界面。
5. **消灭 UI feature 的 N/A 死墙**：UI 验证成为一等公民。

### 非目标

- 不改 Phase 1（需求/AC 锁定）和 Phase 2（实现）。AC 的产出与结构沿用现状。
- 不替换项目单元/集成测试（3.1 保留）。
- 不追求"零信任"裁判——残余信任不可消除（见 §11）。

---

## 3. 核心重构：验证单元 = AC

Phase 1 已把每条需求锁成 `{id, assertion}`（`observable_ac`）。新 Phase 3 把**每一条 AC 一路串到底**，只有当每条 AC 的 assertion 在真实证据里被**观察到且判定满足**才过。

`ac_mapping` 不再只是 plan 文本里的声明——它在执行期被**逐条兑现**：计划 ∩ 证据 ∩ 裁判输出三者必须对每条 P0/P1 AC 都齐全，否则结构 gate FAIL。

---

## 4. 新 Phase 3 流水线（old → new）

| 步骤 | 今天 | 改成 |
|---|---|---|
| 3.0 冒烟 | 保留 | 保留（廉价 liveness 前置） |
| 3.1 项目测试 | 保留 | 保留 |
| **3.2** | HTTP runner 跑手写场景 | **验证计划生成**：从锁定 AC 派生可执行旅程（`verification-plan.json`） |
| **3.3** | LLM 写报告 | **验证执行**：可插拔驱动跑旅程，逐 AC 收证据 bundle（含截图） |
| **3.4** | 数 `min_turns≥10` + 错误率 | **AC 裁判 + 结构 gate + HTML 报告 + 智能人工门** |
| 3.5 loop record | 保留 | 保留 |
| 3.6 knowledge | 保留 | 保留 |

> 步骤 id 保持 `3.2/3.3/3.4` 不变（最小化 orchestrator/hook/状态行的连带改动），只换语义与 validator。

---

## 5. 组件设计

每个组件边界清晰、可独立测试。fastship 拥有「验证契约」（AC → 旅程 → 证据 → 裁判 → gate → 报告），驱动是可插拔后端。

### 5.1 验证计划生成器（3.2）

- **输入**：锁定 AC（`observable_ac: [{id, assertion}]`）+ 本次实现 diff（知道实现真实加了什么按钮 / 文案 / 路由 / 端点）+ Context Brief。
- **输出**：`verification-plan.json`。
- **每条 AC 派生**：`{ac_id, surface, steps[], expected_evidence[]}`。
- **surface 由 AC 自身 + 实现推断**：`ui` / `api` / `data`。
- **UI 步骤用语义定位**（确认决策）：复用 agent-browser 的 `find role/text/label/testid`、`get text/url`、`is visible`，**禁止脆 CSS 选择器**。旅程读起来像人类路径：

  ```
  AC-3: 用户送礼物后看到感谢文案
  surface: ui
  steps:
    - open  {base_url}/chat
    - find  role=button  name="送礼物"  → click
    - find  text="感谢"  → wait visible
    - screenshot  ac-3-after-gift.png
  expected_evidence:
    - screenshot: 礼物面板关闭、感谢气泡可见
    - dom: text "感谢" 存在
    - network: POST /api/gift 状态 2xx
  ```

- **派生自 AC，无法漂移出需求**：生成器不得新增 plan 里没有的 AC，也不得漏掉任一 P0/P1 AC（结构 gate 复核覆盖）。
- **文案/标签来源**：从实现 diff 取真实文案，不得凭空编（grounding 原则）。

### 5.2 驱动适配器（3.3）

- **接口**：`run(journey) -> evidence_bundle`。
- **内置适配器**：
  - `agent-browser`（UI 默认）：把语义步骤翻译成 agent-browser CLI 调用（`open/click/type/fill/select/find/screenshot/snapshot/get/is/network requests`）。
  - `http`（API）：约等于今天的 `e2e_runner.py`，发请求断言响应。
  - `data`（可选）：执行只读 SQL/SELECT 验持久化结果。
- **选型**：项目在 `.claude/fastship.project.json` 配 `verify.driver`（默认 `agent-browser`），可覆盖为项目自带 Playwright/runner。
- **agent-browser 已验能力**（`agent-browser --help`）覆盖全部核心证据类型：截图、a11y snapshot、`get text/url/attr`、`is visible`、`network requests --filter`、`eval`。

### 5.3 证据 bundle（3.3 产物）

每条 AC 一个 bundle：

```json
{
  "ac_id": "AC-3",
  "surface": "ui",
  "steps_executed": [ {"action": "...", "ok": true, "elapsed_ms": 120} ],
  "artifacts": {
    "screenshots": [".claude/fastship-verify/ac-3-after-gift.png"],
    "a11y_snapshot": ".claude/fastship-verify/ac-3-snapshot.txt",
    "network": [{"method":"POST","url":"/api/gift","status":201}],
    "api_responses": [],
    "dom_facts": [{"selector_intent":"text=感谢","present":true}]
  },
  "raw_log": ".claude/fastship-verify/ac-3.log"
}
```

- 截图为**硬要求**（确认决策）：每条 UI AC 至少一张关键状态截图。
- 所有 artifact 写入 `verify.result_dir`（默认 `.claude/fastship-verify/`），并 **hash 绑账本**（沿用今天 `e2e_result_hash` 机制，扩展为 per-AC artifact 清单 + sha256）。

### 5.4 AC 裁判（3.4 第一段）

- **独立对抗 agent**（≠ 实现者），**真的看截图**（vision）。
- **每条 AC 输出**：

  ```json
  {
    "ac_id": "AC-3",
    "verdict": "pass | fail | uncertain",
    "evidence_refs": [
      {"artifact": ".../ac-3-after-gift.png", "fact": "礼物面板已关闭，气泡显示'感谢你的礼物'"},
      {"artifact": "network", "fact": "POST /api/gift → 201"}
    ],
    "reason": "assertion 要求送礼后出现感谢文案，截图+network 双证据满足"
  }
  ```

- **反橡皮图章硬约束**：
  - 每条 verdict **必须引用真实存在的 artifact**（裁判说 pass 但 evidence_ref 指向空/不存在/错 AC 的文件 → 结构 gate FAIL）。
  - 逐 AC 粒度，禁止一句话总判（不能 blanket pass）。
  - 裁判输出绑当前 step provenance/hash，记录后被改即 FAIL（沿用 codex-review / code-review 的账本机制）。

### 5.5 结构 gate（3.4 第二段，确定性）

替代 `e2e_gate.py` 的数 turn。**纯确定性**，验四件事：

1. **覆盖**：每条 P0/P1 AC 同时出现在「验证计划 ∩ 证据 bundle ∩ 裁判输出」——任一缺失 = FAIL（封掉 `ac_mapping` 被声明却没跑的漏洞）。
2. **证据真实**：每条 AC 的 artifact 文件存在 + sha256 匹配账本 + surface 类型与计划声明一致。
3. **裁判引用有效**：裁判每条 `evidence_refs[].artifact` 指向真实存在且非空的 artifact。
4. **派生终判**（自结构派生，自报无效）：
   - 全 AC `verdict=pass` + ①②③ 通过 → **GATE PASS**
   - 任一 `verdict=fail` → **GATE FAIL**（回 3.2/3.3 修，进 loop record）
   - 任一 `verdict=uncertain`，或 P0 AC 缺证据，或裁判低置信 → **SURFACE 给人**（智能门，见 §7）

`min_turns` / 错误率不再是主判据。错误率可作为廉价次级 liveness 信号保留在报告里，不参与 gate 终判。

---

## 6. HTML 测试报告（3.4 产物，永远生成）

复用 fastship 既有 `plan_html.py` / `render-plan` 的离线自包含单文件套路，新增 `verify_html.py`：

- **输入**：`verification-plan.json` + 证据 bundle + 裁判输出。
- **输出**：`*.verify-report.html`（离线自包含单文件，gitignore，不进可信账本）。
- **内容**：
  - 顶部总判：PASS / FAIL / 待确认 + 每类 surface 的 AC 通过数。
  - 每条 AC 一个卡片：assertion 原文 → **人类可读测试路径**（steps）→ **截图**（内嵌缩略图，可点开大图）→ network/DOM 事实 → **裁判 verdict + reason + 引用证据**。
  - 失败/uncertain 的 AC 高亮置顶。
- **生成后自动在浏览器打开**（非 CI/headless），沿用 `FASTSHIP_PLAN_HTML_OPEN` 同款开关 `FASTSHIP_VERIFY_HTML_OPEN`（`auto`/`always`/`never`）。
- 生成失败**不阻断**流程。

---

## 7. 智能人工门（确认决策）

报告**永远生成**，智能门只决定「自动过 + 报告留你过目」还是「阻断等你确认」：

- **自动过**（全 AC pass + 证据强）：3.4 PASS，`/goal` 继续不断。报告链接醒目打印给用户「FYI，这是我验了什么」。
- **阻断**（任一 uncertain / P0 缺证 / 裁判低置信）：停在 3.4，把 HTML 报告推给用户，等用户看截图后显式确认（`done --verify-confirmed`）或判 FAIL 回退。
- 与 §5.5 派生终判一致。豁免权归用户，不归 Claude（沿用现有红线）。

---

## 8. 删除 / 迁移

- **删除**：`e2e_gate.py` 的 `min_turns` 墙 + 错误率主判据逻辑 → 换成 §5.5 结构 gate。`E2E_MIN_TURNS` 常量移除或降级为可选 liveness 阈值。
- **迁移**：
  - 既有项目的 HTTP `runner_command` → 由 `http` 适配器包装，旧 `e2e_result.json` 格式作为 `http` surface 的证据来源。
  - `.claude/fastship.project.json` 新增 `verify` 段（见 §9）；旧 `e2e` 段保留供 `http` 适配器读取。
- **N/A**：UI feature 不再需要 N/A。N/A 仅剩"完全无可观察表面"的极少数情况，且本身是需求异味，需用户显式豁免。

---

## 9. 项目配置变化（`.claude/fastship.project.json`）

```json
{
  "verify": {
    "driver": "agent-browser",
    "base_url": "http://localhost:15173",
    "setup_commands": ["./dev_local.sh", "cd apps/web-app && npm run dev"],
    "result_dir": ".claude/fastship-verify/",
    "drivers": {
      "agent-browser": { "bin": "agent-browser", "viewport": [1280, 800] },
      "http": { "base_url": "http://localhost:3100", "health": "/health" }
    }
  }
}
```

- `verify.driver` 缺省 `agent-browser`；项目可覆盖为自带 Playwright（指向项目脚本）。
- runner / hook hash 记录 / gate / 报告渲染读同一份配置（沿用现有"配置单源"红线）。

---

## 10. orchestrator / hook 连带改动

- **orchestrator.py**：3.2/3.3/3.4 的 `next` 指令文本与 validator 重写（`validate_e2e_run` / `validate_e2e_report` / `validate_e2e_gate` → AC 计划/执行/裁判+结构 gate）。step id 不变。
- **ship_verify_gate.py**：
  - `e2e_result_hash` 扩展为 per-AC artifact 清单 hash。
  - 新增状态字段：`verify_plan_hash` / `verify_evidence_hash` / `judge_output_hash` / `ac_coverage_ok` / `verify_confirmed`（智能门人工确认）。
  - Gate 0（E2E 阶段禁 DB 写）保留。
- **状态行** `[FASTSHIP_GOAL]`：`e2e_executed` / `e2e_gate_passed` 语义改为 AC 验证维度，供 `/goal` 评估器解析。

---

## 11. 诚实的边界 / 风险

- **残余信任不可消除**：结构 gate 能确定性验证「文件在、hash 对、覆盖全、裁判引用真实」，但**无法验证裁判的"看图推理"本身对不对**（截图是否真的展示了裁判声称的东西）。这与今天信任 codex-review 是同一种信任。智能人工门是高风险场景兜底。
- **语义定位的脆弱面**：`find role/text/label` 依赖实现真实文案；多语言/动态文案下需从实现 diff 取准。比 CSS 健壮但非零风险。
- **成本**：每条 AC 一次 vision 判定 + 浏览器旅程，比纯 HTTP 慢、耗 token。可接受，但需在报告里标注耗时。
- **应用启动复杂度**：UI 验证需前端 dev server + 后端同时在跑，`verify.setup_commands` 要把两端都拉起来。

---

## 12. 验收标准（这次重构怎么算成功）

1. 一个**纯 UI feature**（如礼物面板交互）能在不标 N/A 的情况下走完 3.2–3.4，产出含截图的 HTML 报告，结构 gate 基于 AC 覆盖判定。
2. 一个**后端 API feature** 走 `http` 适配器，逐 AC 验响应/持久化，不触发浏览器。
3. **故意漏验一条 P0 AC** → 结构 gate FAIL（覆盖检查生效）。
4. **裁判伪造 pass 但 evidence_ref 指向不存在文件** → 结构 gate FAIL（反橡皮图章生效）。
5. `min_turns` 不再出现在任何判定路径。
</content>
</invoke>
