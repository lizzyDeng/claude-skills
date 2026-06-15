# fastship 子进程挂死 / 看门狗闭环加固 — 设计

- 日期：2026-06-15
- 范围：`skills/fastship/`（orchestrator.py / SKILL.md / hooks）
- 触发事件：Phase-1 计划评审期间，驱动 agent 在 fastship 之外裸起背景 `codex`，子进程阻塞在
  "Reading additional input from stdin"，永不退出 → 无完成事件 → harness 永不唤醒驱动 →
  后台会话静坐约 2 小时，直到人工 kill 才触发一条 "failed exit 144"。

## 1. 问题定义（根因，非症状）

事故不是"一个 case"，是三层独立的洞，任一补上都能阻止 2 小时静坐：

- **洞0（根因 · 启动准入）— 子进程无界。** fastship 从不规定 codex 怎么起：1.5c
  (Codex Review) 只验产物 `.fastship-codex-review.md` 的 PASS/FAIL JSON gate +
  `done --codex-review` 绑定（SKILL.md L80 / orchestrator.py:2253 Step "1.5c"）。
  实际 `codex exec …` 由驱动 agent 即兴拼，背景起、stdin 未关、无 timeout。阻塞在 stdin
  的子进程上报 alive、永不退出 → 无完成事件 → harness 永不唤醒驱动。
- **洞1（看门狗盲区 A）— 嗅探 loop 可能根本没在跑。** SKILL.md L24 要求"驱动 agent 必须
  立即启动嗅探 loop"，但无任何东西强制它活着。loop 未起 / 中途死掉 → 零监控、零告警；
  存活信息只在人工 `fastship status` 时渲染。事故 2h 时长最符合"loop 当时没在跑"——因为
  1.5c **不在豁免列表**，只要 loop 在跑且引擎停在 1.5c，Phase-1 阈值 1800s 到点必先 resume
  再 notify（约 34 分钟内告警），不可能拖 2 小时。
- **洞2（看门狗盲区 B）— 豁免无限。** 1.5/1.6/3.5 永久豁免、永不判 stalled
  (orchestrator.py:3313 `SNIFF_EXEMPT_STEPS`)。**本设计不修洞2**，理由见 §5。

### 关键事实：安全启动方式早已存在且正确

gstack `codex` skill（`~/.claude/skills/gstack/codex/SKILL.md.tmpl`）全程用
`_gstack_codex_timeout_wrapper <秒> codex … < /dev/null 2>"$TMPERR"`——timeout 包裹 +
stdin 接 /dev/null + stderr 捕获 + hang 日志（L177-184 / L218 / L339），并已记录
"0.120.x stdin 回归"教训。**结论：洞0 不是"造安全启动"，是"逼驱动走已有安全路径、禁止
即兴裸起背景 codex"。**

## 2. 本次范围（用户已决策）

- ✅ 洞0：有界启动 codex —— **窄门禁**（只拦裸 `codex`）+ CLI 软处方
- ✅ 洞1：嗅探 loop 存活自检 —— 挂到驱动活动 + 自愈重启
- ❌ 洞2：豁免外层上限 —— 本次不做（§5 说明为何与 洞0 互斥地变得多余）

## 3. 设计

### 3.1 洞0 — 有界启动 codex（双层）

**A. 硬门禁（Claude Code hook 模式主路径）**

`hook_pre_bash_logic`（orchestrator.py:2826）已能硬 block（`return 1` + 打印提示，
现有 branch-mismatch 即此机制，L2840-2845）。新增一条检查：

- 触发条件：session 活跃（`_is_active(orch_state)`）且 Bash 命令字符串调用了 `codex`，
  但**缺有界保护**——判定为"裸 codex"当且仅当命令中：
  - 不含 stdin 重定向到 /dev/null（`< /dev/null` 或 `0</dev/null` 或 `</dev/null`），**或**
  - 不含 timeout 包裹（`timeout `/`gtimeout `/`_gstack_codex_timeout_wrapper `）。
- 命中即 `return 1`，打印：① 一行原因；② **改好的安全命令模板**让驱动照抄
  （`timeout <秒> codex … < /dev/null`，或直接指向 `/codex review` 安全路径）。
- 检测**只针对 codex**（用户决策：窄门禁，最低误拦），不拦其它背景子进程。
- 放行豁免：恢复类命令沿用现有 `is_branch_recovery_command` 同类思路——嗅探自身 / 已
  带 timeout+stdin 的命令直接放行。

**B. 软处方（Codex / 其它 Agent 的 CLI 模式，无 hook 兜底）**

CLI 模式 pre_bash 不触发。补：

- fastship 1.5c 的 `next` 指令输出 + SKILL.md「Codex Review」段，显式吐出 canonical
  有界命令，并明文禁止裸起背景 codex。
- 文案单源：门禁打印的安全命令模板与 SKILL 处方共用同一常量，避免两处漂移。

**效果**：无限挂死从"可能发生"变成"起不来 / 必有界"。挂死被 timeout 转成有界失败 →
完成事件 → 驱动唤醒 → 1.5c FAIL 按缺陷层回退（orchestrator.py:3765 已存在）接管。

### 3.2 洞1 — 嗅探 loop 存活自检（挂到驱动活动 + 自愈）

存活判定逻辑**已存在**：`_sniff_status_lines`（orchestrator.py:3829）已区分三态——
未启动 / watchdog stale（心跳 age > 2×interval）/ 正常。问题纯粹是只在 `fastship status`
渲染，要人主动看。

- **挂到驱动活动**：把 `_sniff_status_lines`（或其等价精简版）的"未启动 / stale"告警，
  自动附到驱动每次与 orchestrator 交互的输出末尾——`cmd_next` / `cmd_done` 推进结果，
  以及 hook 自动推进（post_bash / post_edit）的输出。
- **自愈重启**：检测到 loop 死 / 未起时，不止 warn —— **原样回吐那条 `/loop` 嗅探命令**
  （复用 `_print_sniff_hint` 的命令构造，单源），驱动零思考即可重启。
- 不改嗅探判定口径、不读 mtime（沿用 KNOWLEDGE 教训）、不新增 daemon。纯 stdlib、
  对 orchestrator.json 只读。
- 终态步骤（done/stopped）不告警（沿用 `_sniff_status_lines` 现有分支）。

### 3.3 咬合点（为何 洞0+洞1、且砍 洞2 自洽）

- 洞0 保证**驱动永不无限静坐**：有界子进程必产生完成/timeout 事件 → harness 必唤醒驱动
  → 驱动必再碰 orchestrator（next/done/hook 推进）。
- 洞1 的检查就跑在这个"必然发生的活动"上 → 原本无解的"loop 死 + 驱动闲 = 永久黑屏"
  变成**自愈**：驱动被唤醒 → 输出里看到"loop 已死" → 照抄重启。
- 洞2 当初只为兜"豁免步骤里被等的活儿悄悄死了"；洞0 已让那活儿有界 → 该危险消失 →
  洞2 在本组合下多余。

## 4. 受影响文件

- `skills/fastship/orchestrator.py`
  - `hook_pre_bash_logic`（2826）：新增窄 codex 门禁分支。
  - 新增 codex 命中判定 helper（纯字符串/stdlib，可单测）+ 安全命令模板常量。
  - `cmd_next` / `cmd_done` 输出尾部：附加 loop 存活告警。
  - hook post_bash / post_edit 自动推进输出：同上附加。
  - 复用 `_sniff_status_lines`、`_print_sniff_hint`、`_sniff_interval_s`。
- `skills/fastship/SKILL.md`
  - 「Codex Review」段：canonical 有界命令处方 + 禁止裸起背景 codex。
  - 「嗅探 loop」段：补一句"驱动每次推进会自检 loop 存活，死则提示重启"。

## 5. 明确不做（YAGNI / 范围边界）

- 洞2 豁免外层上限：见 §3.3，被 洞0 取代。
- 不拦 codex 以外的背景子进程（用户决策：窄门禁）。
- 不引入 daemon / 进程表扫描 / mtime 轮询（违背嗅探"绝不读 mtime"设计）。
- 不改 codex 评审的内容契约 / JSON gate / 缺陷层回退逻辑。

## 6. 验证（AC + 测试）

每条 AC 配 ≥1 单测（纯 stdlib，沿用现有 Test* 风格；嗅探有 `TestSniffLivenessParity`
等可挂靠）：

- **AC-0-1**：active session 下，`codex exec …`（无 timeout、无 `< /dev/null`）经 pre_bash →
  `return 1` + 打印安全命令模板。
- **AC-0-2**：`timeout 330 codex … < /dev/null` 经 pre_bash → 放行（`return 0`）。
- **AC-0-3**：非 codex 背景命令（如 `sleep 999 &`）→ 放行（窄门禁不误拦）。
- **AC-0-4**：无活跃 session / 已在 done/stopped → 不拦（沿用现有早退分支）。
- **AC-0-5**：SKILL.md「Codex Review」段含有界命令处方与禁止裸起的明文。
- **AC-1-1**：sniff-state 无 `last_check_at` 且 step 活跃 → `cmd_next`/`cmd_done` 输出含
  "嗅探未启动 + /loop 重启命令"。
- **AC-1-2**：`last_check_at` age > 2×interval → 输出含 "watchdog stale + 重启命令"。
- **AC-1-3**：心跳新鲜 → 不刷告警（不污染正常输出）。
- **AC-1-4**：step ∈ done/stopped → 不告警。
- **回归**：现有 hook_pre_bash branch-mismatch / gate 委派 / ambiguous 分支不变；
  `_sniff_status_lines`、cmd_sniff 升级链行为不变。

## 7. 实现路径（待 §spec 评审时与用户确认）

本仓库（claude-skills）是 fastship 源仓库，fastship 自跑只能 CLI 模式
（无 hook 安装）。实现路由二选一，评审时定：
- (a) 直接 TDD 改 + `python3 orchestrator.py`/pytest 跑全测（最短）；
- (b) fastship CLI 自跑（dogfood，较重）。
