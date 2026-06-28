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
2. **按需求表面选验证层级**：UI 走浏览器旅程（语义定位 + 截图），API 走 HTTP 断言，data 走持久化查询。**一条 AC 的旅程可跨多个表面**（见 §6 cross-端）。
3. **裁判替代数数**：用「对着 AC 逐条引用证据的对抗裁判」替代 `min_turns` / 错误率 gate。
4. **永远产出 HTML 测试报告**：含每条 AC 的测试路径 + 截图 + 裁判结论，作为人类确认界面。
5. **消灭 UI feature 的 N/A 死墙**：UI 验证成为一等公民。
6. **cross-端覆盖靠结构强制，不靠旅程写得全**：required-surface 覆盖 + differential 差分做成确定性硬门（§6）。

### 非目标

- 不改 Phase 2（实现）。AC 的实现产出沿用现状。
- **Phase 1 仅加一条小约束**（§6.4）：被 config/toggle 门控的 AC 必须写成差分（ON/OFF 两态）并声明涉及表面。除此之外 Phase 1 需求/AC 锁定流程不动。
- 不追求"零信任"裁判——残余信任不可消除（见 §12）。

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
| **3.2** | HTTP runner 跑手写场景 | **验证意图生成**：从锁定 AC 派生「可导航目标 + 语义提示」（`verification-plan.json`） |
| **3.3** | LLM 写报告 | **验证执行（看一眼再点）**：浏览器 agent 读真实 a11y 树导航，记录**真实走过的路径** + 逐 AC 收证据 bundle（含截图） |
| **3.4** | 数 `min_turns≥10` + 错误率 | **AC 裁判 + 结构 gate + HTML 报告 + 智能人工门** |
| 3.5 loop record | 保留 | 保留 |
| 3.6 knowledge | 保留 | 保留 |

> 步骤 id 保持 `3.2/3.3/3.4` 不变（最小化 orchestrator/hook/状态行的连带改动），只换语义与 validator。

### 路径生成方式（已锁定）：看一眼再点（explore-and-record）

不预写死脚本。3.2 只产「验证意图」（entry + goal + success_evidence + 从 diff 取的语义 hints）；3.3 由浏览器 agent **读真实 a11y 树（`snapshot`）→ 看到真实元素再决定点哪 → 边走边截图**，输出**真实走过的路径**。这是人类 QA 的做法，对动态/多语言 UI 健壮（按钮真叫"赠送"还是"送出"，agent 当场在树里看到，照点不误）。

非确定性导航是可接受的：验证的**信任来自独立对抗裁判 + 结构 gate 对证据的复核**，不来自导航被脚本写死（与 Phase 2 实现走 agent 同理）。

---

## 5. 组件设计

每个组件边界清晰、可独立测试。fastship 拥有「验证契约」（AC → 旅程 → 证据 → 裁判 → gate → 报告），驱动是可插拔后端。

### 5.1 验证意图生成器（3.2）

- **输入**：锁定 AC（`observable_ac`）+ 本次实现 diff + Context Brief。
- **输出**：`verification-plan.json`，每条 AC 一个**验证意图**：

  ```json
  {
    "ac_id": "AC-2",
    "required_surfaces": ["ui"],
    "entry": "{user_base_url}/chat",
    "goal": "送出一个礼物给当前女友",
    "success_evidence": [
      "聊天出现感谢气泡（文案含'谢'）",
      "礼物面板不再可见",
      "network: POST /api/gift → 2xx"
    ],
    "hints_from_diff": { "gift_entry": "role=button name~=礼物", "send": "role=button name~=送出|赠送" },
    "differential": null
  }
  ```

- `required_surfaces`、`differential` 由 **AC + diff 派生**（cross-端 见 §6）。
- **语义提示而非死选择器**（确认决策）：hints 用 `role/text/label/testid` 意图，禁止脆 CSS。文案从实现 diff 取真实值，不得凭空编。
- **派生自 AC，无法漂移出需求**：生成器不得新增 plan 里没有的 AC，也不得漏掉任一 P0/P1 AC（结构 gate 复核覆盖）。

### 5.2 驱动适配器（3.3）

- **接口**：`run(intent) -> evidence_bundle`，内部走「看一眼再点」循环（`snapshot → 决策 → act → capture`）。
- **内置适配器**：
  - `agent-browser`（UI 默认）：语义步骤映射到 agent-browser CLI（`open/snapshot/find/click/type/fill/select/screenshot/get/is/network requests/eval`）。
  - `http`（API）：约等于今天的 `e2e_runner.py`，发请求断言响应。
  - `data`（可选）：执行只读 SQL/SELECT 验持久化结果。
- **一条旅程可在多个适配器/多个 app 间切换**（cross-端，§6）：步骤携带 `target`（哪个 app/url/driver）。
- **选型**：项目在 `.claude/fastship.project.json` 配 `verify.driver`（默认 `agent-browser`），可覆盖为项目自带 Playwright/runner。

### 5.3 证据 bundle（3.3 产物）

每条 AC 一个 bundle，记录**真实走过的路径** + 各表面 artifact：

```json
{
  "ac_id": "AC-2",
  "surfaces_touched": ["ui"],
  "states": { "default": { } },
  "realized_journey": [ {"target":"user","action":"click 礼物入口","ok":true,"elapsed_ms":120} ],
  "artifacts": {
    "screenshots": [".claude/fastship-verify/ac-2-after.png"],
    "a11y_snapshot": ".claude/fastship-verify/ac-2-snapshot.txt",
    "network": [{"target":"user","method":"POST","url":"/api/gift","status":201}],
    "api_responses": [],
    "dom_facts": [{"selector_intent":"text=感谢","present":true}]
  },
  "raw_log": ".claude/fastship-verify/ac-2.log"
}
```

- 截图为**硬要求**（确认决策）：每条 UI AC 至少一张关键状态截图；差分 AC 每态各一张（§6）。
- 所有 artifact 写入 `verify.result_dir`（默认 `.claude/fastship-verify/`），**hash 绑账本**（沿用 `e2e_result_hash` 机制，扩展为 per-AC artifact 清单 + sha256）。

### 5.4 AC 裁判（3.4 第一段）

- **独立对抗 agent**（≠ 实现者），**真的看截图**（vision）。
- **每条 AC 输出**：

  ```json
  {
    "ac_id": "AC-2",
    "verdict": "pass | fail | uncertain",
    "evidence_refs": [
      {"artifact": ".../ac-2-after.png", "fact": "礼物面板已关闭，气泡显示'感谢你的礼物'"},
      {"artifact": "network", "fact": "POST /api/gift → 201"}
    ],
    "reason": "assertion 要求送礼后出现感谢文案，截图+network 双证据满足"
  }
  ```

- **反橡皮图章硬约束**：
  - 每条 verdict **必须引用真实存在的 artifact**（裁判说 pass 但 evidence_ref 指向空/不存在/错 AC 的文件 → 结构 gate FAIL）。
  - 逐 AC 粒度，禁止一句话总判。
  - 差分 AC 须分别对 ON/OFF 两态出证（§6）。
  - 裁判输出绑当前 step provenance/hash，记录后被改即 FAIL。

### 5.5 结构 gate（3.4 第二段，确定性）

替代 `e2e_gate.py` 的数 turn。**纯确定性**，验：

1. **AC 覆盖**：每条 P0/P1 AC 同时出现在「验证计划 ∩ 证据 bundle ∩ 裁判输出」——任一缺失 = FAIL（封掉 `ac_mapping` 被声明却没跑的漏洞）。
2. **surface 覆盖**（cross-端，§6）：每条 AC 的 `required_surfaces`（从 diff 派生）必须都在 `surfaces_touched` 里有 artifact 佐证——旅程没碰某个必需表面 = FAIL。
3. **differential 覆盖**（cross-端，§6）：被标 differential 的 AC，证据 bundle 必须含两态对照（ON/OFF 各有 artifact + 裁判分别出证）——只证共存不证因果 = FAIL。
4. **证据真实**：每条 AC 的 artifact 文件存在 + sha256 匹配账本 + surface 类型与计划声明一致。
5. **裁判引用有效**：裁判每条 `evidence_refs[].artifact` 指向真实存在且非空的 artifact。
6. **派生终判**（自结构派生，自报无效）：
   - 全 AC `verdict=pass` + ①–⑤ 通过 → **GATE PASS**
   - 任一 `verdict=fail`，或①–⑤任一不过 → **GATE FAIL**（进 loop record）
   - 任一 `verdict=uncertain`，或 P0 AC 缺证据，或裁判低置信 → **SURFACE 给人**（智能门，§8）

`min_turns` / 错误率不再是主判据。错误率可作为廉价次级 liveness 信号保留在报告里，不参与终判。

---

## 6. cross-端验证与覆盖强制

aifriends 大量 feature 是跨端的（admin 配置 + sys_config 开关 + 用户端生效，如 gift_entry / daily_report / feedback_notification）。单表面验证会假过：用户端碰巧是开的就报 PASS。

### 6.1 cross-端 AC 写成差分（differential）

> `AC: admin 将 gift_entry 置 ON 保存后，用户端 /chat 出现礼物入口；置 OFF 后消失。`

**"开启后出现"单独不算验证到位**——它只证明 feature 和开关共存，没证明开关控制 feature。全链路证据是 **ON→有 且 OFF→无 的对照**，证明 admin→backend→user 因果链通了。这与现有红线（`status 200`/`button visible` 只能当 smoke）同一精神：弱断言不算核心覆盖。

### 6.2 旅程是跨表面序列（看一眼再点照常）

```
# 表面1 · admin (:15174) — 设置「因」
open  {admin_base_url}/config ; snapshot → gift_entry 开关 → 置 ON → 保存
screenshot adm-on.png ; network /api/admin/sys_config  # 落库 2xx（backend 证据）
# 表面2 · user app (:15173) — 验「果」(ON 态)
open  {user_base_url}/chat ; assert 礼物入口 visible ; screenshot user-on.png
# 差分 · 回 admin 置 OFF
open  {admin_base_url}/config → gift_entry → OFF → 保存
# user app 验 OFF 态
open  {user_base_url}/chat (reload) → assert 礼物入口 not visible ; screenshot user-off.png
# teardown: 恢复原值
```

一条 AC、三个表面（admin/backend/user）、两个状态。证据 bundle：4 截图 + admin 保存 network + ON/OFF 两态 DOM 事实。

### 6.3 覆盖怎么"确保"（结构强制，不寄希望于旅程写得全）

1. **required-surface 派生 + 强制**：diff 改了 `apps/admin-web` + `apps/web-app` + sys_config key → 派生 `required_surfaces = {admin, user}`。结构 gate（§5.5②）要求证据 bundle 含开过各表面的 artifact，缺一 FAIL。文件路径→app 映射是确定的，可靠。
2. **differential 强制**：feature 读 sys_config flag → 要求 ON/OFF 两态证据齐全（§5.5③），否则判 weak → FAIL。

### 6.4 Phase 1 小约束（唯一改动 Phase 1 处）

被 config/toggle 门控的 AC，在 1.4 `ac_mapping` 校验时**必须**：声明 `differential: {flag, on_state, off_state}` + 列出 `required_surfaces`。

理由：自动从 diff 判"这是 config-gated feature 且需差分"会漏（启发式）；根上的兜底是需求锁定时就把 cross-端 AC 写成差分。AC 写错（漏 OFF 态）→ 验证必漏。这是 Phase 1↔Phase 3 的覆盖契约，落在 `ac_mapping` 校验里（dangling/漏态/漏表面 = 当场 FAIL，不等 codex）。

### 6.5 gate-0「禁 DB 写」冲突的化解

现红线 Gate 0 拦 E2E 阶段 DB 写（INSERT/UPDATE/DELETE）。cross-端验证要写 config——但它是**驱动真实 admin UI 做的**（feature 自己的预期写入路径），不是测试跑裸 SQL。Gate 0 拦的是 Bash 裸 SQL，不拦 app 驱动的写入，故不冲突，且更对（顺带验了运营真实操作流）。代价：状态被改，旅程末尾须 teardown 恢复原值（或跑隔离 dev DB，如 aifriends 的 15432）。

---

## 7. HTML 测试报告（3.4 产物，永远生成）

复用 fastship 既有 `plan_html.py` / `render-plan` 的离线自包含单文件套路，新增 `verify_html.py`：

- **输入**：`verification-plan.json` + 证据 bundle + 裁判输出。
- **输出**：`*.verify-report.html`（离线自包含单文件，gitignore，不进可信账本）。
- **内容**：
  - 顶部总判：PASS / FAIL / 待确认 + 各 surface 的 AC 通过数。
  - 每条 AC 一个卡片：assertion 原文 → **人类可读测试路径**（真实走过的 realized_journey）→ **截图**（差分 AC 并排 ON/OFF 两态）→ network/DOM 事实 → **裁判 verdict + reason + 引用证据**。
  - 失败/uncertain 的 AC 高亮置顶。
- **生成后自动在浏览器打开**（非 CI/headless），开关 `FASTSHIP_VERIFY_HTML_OPEN`（`auto`/`always`/`never`）。
- 生成失败**不阻断**流程。

---

## 8. 智能人工门（确认决策）

报告**永远生成**，智能门只决定「自动过 + 报告留你过目」还是「阻断等你确认」：

- **自动过**（全 AC pass + 证据强 + surface/differential 覆盖齐）：3.4 PASS，`/goal` 继续不断。报告链接醒目打印「FYI，这是我验了什么」。
- **阻断**（任一 uncertain / P0 缺证 / 裁判低置信 / 覆盖不齐）：停在 3.4，把 HTML 报告推给用户，等用户看截图后显式确认（`done --verify-confirmed`）或判 FAIL 回退。
- 与 §5.5 派生终判一致。豁免权归用户，不归 Claude。

---

## 9. 删除 / 迁移

- **删除**：`e2e_gate.py` 的 `min_turns` 墙 + 错误率主判据逻辑 → 换成 §5.5 结构 gate。`E2E_MIN_TURNS` 移除或降级为可选 liveness 阈值。
- **迁移**：
  - 既有项目 HTTP `runner_command` → 由 `http` 适配器包装，旧 `e2e_result.json` 作为 `http` surface 证据来源。
  - `.claude/fastship.project.json` 新增 `verify` 段（§10）；旧 `e2e` 段保留供 `http` 适配器读取。
- **N/A**：UI/cross-端 feature 不再需要 N/A。N/A 仅剩"完全无可观察表面"的极少数情况，且本身是需求异味，需用户显式豁免。

---

## 10. 项目配置变化（`.claude/fastship.project.json`）

```json
{
  "verify": {
    "driver": "agent-browser",
    "result_dir": ".claude/fastship-verify/",
    "surfaces": {
      "user":  { "base_url": "http://localhost:15173", "app_paths": ["apps/web-app"] },
      "admin": { "base_url": "http://localhost:15174", "app_paths": ["apps/admin-web"] },
      "api":   { "base_url": "http://localhost:3100", "health": "/health" }
    },
    "setup_commands": ["./dev_local.sh", "cd apps/web-app && npm run dev", "cd apps/admin-web && npm run dev"],
    "drivers": { "agent-browser": { "bin": "agent-browser", "viewport": [1280, 800] } }
  }
}
```

- `surfaces.*.app_paths` 是 **required-surface 派生的依据**：diff 命中哪个 app_paths → 该 surface 成为 required。
- runner / hook hash 记录 / gate / 报告渲染读同一份配置（配置单源红线）。

---

## 11. orchestrator / hook 连带改动

- **orchestrator.py**：3.2/3.3/3.4 的 `next` 指令文本与 validator 重写（`validate_e2e_run` / `validate_e2e_report` / `validate_e2e_gate` → AC 意图/执行/裁判+结构 gate）。step id 不变。1.4 `ac_mapping` 校验加 §6.4 差分约束。
- **ship_verify_gate.py**：
  - `e2e_result_hash` 扩展为 per-AC artifact 清单 hash。
  - 新增状态字段：`verify_plan_hash` / `verify_evidence_hash` / `judge_output_hash` / `ac_coverage_ok` / `surface_coverage_ok` / `differential_ok` / `verify_confirmed`。
  - Gate 0（E2E 阶段禁裸 SQL DB 写）保留；app 驱动的 config 写入不拦（§6.5）。
- **状态行** `[FASTSHIP_GOAL]`：`e2e_executed` / `e2e_gate_passed` 语义改为 AC 验证维度。

---

## 12. 诚实的边界 / 风险

- **残余信任不可消除**：结构 gate 能确定性验证「文件在、hash 对、覆盖全、裁判引用真实」，但**无法验证裁判的"看图推理"本身对不对**。这与今天信任 codex-review 同一种信任。智能人工门是高风险兜底。
- **surface 覆盖最硬，differential 判定半启发**：required-surface 从文件路径派生可靠；"是否 config-gated 需差分"一半靠 diff 启发、一半靠 §6.4 的 Phase 1 AC 写对。AC 写漏 OFF 态 → 验证必漏，故 §6.4 是真正兜底。
- **语义定位脆弱面**：依赖实现真实文案；多语言/动态文案需从 diff 取准。比 CSS 健壮但非零风险。
- **成本**：每条 AC 一次 vision 判定 + 浏览器旅程（差分 AC 翻倍），比纯 HTTP 慢、耗 token。报告里标注耗时。
- **多 app 启动复杂度**：cross-端需 admin + user + api 同时在跑；`verify.setup_commands` 要全拉起；状态需 teardown。

---

## 13. 验收标准（这次重构怎么算成功）

1. **纯 UI feature**（礼物面板交互）不标 N/A 走完 3.2–3.4，产出含截图的 HTML 报告，结构 gate 基于 AC 覆盖判定。
2. **后端 API feature** 走 `http` 适配器，逐 AC 验响应/持久化，不触发浏览器。
3. **cross-端 toggle feature**（admin 配 + 用户端生效）：旅程跨 admin+user 两表面、ON/OFF 两态出证，结构 gate 验 surface + differential 覆盖。
4. **故意漏验一条 P0 AC** → 结构 gate FAIL（AC 覆盖检查）。
5. **cross-端 feature 的旅程只点了用户端、没碰 admin** → 结构 gate FAIL（surface 覆盖检查）。
6. **toggle feature 只验了 ON 态没验 OFF 态** → 结构 gate FAIL（differential 检查）。
7. **裁判伪造 pass 但 evidence_ref 指向不存在文件** → 结构 gate FAIL（反橡皮图章）。
8. `min_turns` 不再出现在任何判定路径。
</content>
