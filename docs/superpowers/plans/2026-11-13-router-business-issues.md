# Router v2（业务问题票 + 分析 agent）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 router 的工作单位从「机制票」换成「业务问题票」：分析 agent（Fable）负责发现问题、做每日验证；用户判真后一次授权整张票；票内的尝试由 router 推进。

**Architecture:**
- **数据：** `store.py` 的票加 `kind`。`business` 票带人群、墙、指标、尝试记录和影响分；没有 `kind` 的老票按 `legacy` 处理，行为和 v1 逐字节一致。
- **分析 agent 的运行：** 新模块 `analyst.py` 用 `claude -p --model claude-fable-5-1` 在后台跑。tick 每天定时触发一次，新周报到达时也触发一次。产出的 JSON 由 `router.py` 读入：daily 记成判定，weekly 进决定单的「判真」项。
- **排序：** 派发和决定单都改按影响分排。维护票单独占名额，有上限。

**Tech Stack:** Python 3.11 标准库，pytest，现有的 `FakeCtx`、`fake_claude.py` 测试设施。

**Spec:** `docs/superpowers/specs/2026-11-13-router-business-issues-design.md`。执行者要同时读 spec 和 v1 的 `docs/superpowers/specs/2026-11-02-router-design.md`。

## Global Constraints

- **公开仓：** 代码、测试、夹具、文档里一律不写真实 uid、用户原话、业务数值；push 前用 `github.leak_hits` 扫 `git log -p origin/main..HEAD`，命中数必须为 0。
- **测试：** `python3 -m pytest tests/router -q` 全绿（基线 371 passed）。新行为一律先写失败测试。
- **兼容：** 没有 `kind` 的老票走 v1 全部路径（方向卡交用户、staging 走查、T+1/T+3/T+7 验收），不许改变它们的行为。
- **不做 A/B：** 新代码路径里不出现对照组、显著性、z 值、置信区间。
- **只读 prod：** 分析 agent 和脚本都不写库、不动开关。撤回由 router session 照记录的回滚办法执行。
- **分析 agent 不开票：** 它的产出只能经 `propose` 进决定单，用户回「真」之后才建票。
- **仍要用户批的三类（spec §5.5）：** 花钱、撤不回的改动、发应用商店包。worker 用 `emit decision --tags spend|irreversible|store_release` 停下。
- **分析 agent 的模型：** 缺省 `claude-fable-5-1`，可以用 config `analyst.model` 改。走订阅，不算花钱。

## Review Focus

1. state.json 里已有的老票没有 `kind` 字段：所有命令都按 v1 处理，不报 KeyError，不进业务流程。（Task 1、6 的测试）
2. 分析 agent 崩溃、没写出文件、写出的 JSON 坏了：状态一点不变；errors 报一次；当天不重跑，次日照常跑。不许每轮反复重拉进程。（Task 5）
3. 用户回答「判真」项时的各种写法，要么解析对，要么拒收并提示正确格式：「真」「真，先放着」「不是：理由」「不是」（没写理由）「好的」（无关文本）。（Task 3）
4. 提议里的标题、人群、墙、证据带了 uid 或原话：在建 issue 之前就被防泄漏闸拒掉。（Task 3）
5. 同一张票还有尝试没判完，又要上线新的尝试：`released` 拒收。待样本超过 7 天还没判：不许再记待样本，必须给出终判。（Task 1、Task 4）

---

### Task 1: 业务票数据模型（store.py）

**Files:**
- Modify: `skills/router/scripts/store.py`（`_ticket_defaults` :92 附近；新增函数放在 `new_ticket` :170 之后）
- Test: `tests/router/test_business.py`（新建）

**Interfaces:**
- Produces:
  - `KINDS = ("legacy", "business", "maint")`。`ticket_kind(t: dict) -> str`：缺字段时返回 `"legacy"`。
  - `new_business_ticket(state, *, title, segment, want, walls: list[str], metric: dict, evidence: list[str], impact: dict, origin: str, absorbs: list[str] = ()) -> str`：新建的票带 `kind="business"`、`authorized=True`、`walls=[{"id":"W1","text":…,"status":"open","source":…,"at":…}]`、`attempts=[]`、`tasks=[]`、`no_use_streak=0`、`parent=None`。
  - `impact_score(t) -> float`：值 = `size × gap × {"low":0.5,"mid":0.75,"high":1.0}[confidence]`。`size` 和 `gap` 取 0–1。legacy 和 maint 票返回 0.0。
  - `attach_wall(state, tid, text, source, at) -> str`：返回新墙的 id，编号续着往下排（W2、W3…）。
  - `add_attempt(state, tid, direction, at, rollback: str) -> int`：返回尝试序号 n（从 1 开始）。
  - `VERDICTS = ("no_entry","pending_sample","useful","wall_broken_not_retained","useless","harmful")`
  - `record_verdict(state, tid, n, verdict, at, *, touched: int, hit_wall: int, continued: int, note: str, new_wall: str | None = None) -> dict`：返回 `{"actions": [...]}`，取值有 `next_wall`、`change_direction`、`return_to_user`、`rollback`、`check_entry`、`wait`。

- [ ] **Step 1: 写失败测试**（`tests/router/test_business.py`）
  - `test_legacy_ticket_kind_defaults`：用 `make_state` 建一张不带 kind 的票，`ticket_kind(t) == "legacy"`。
  - `test_new_business_ticket_fields`：`authorized is True`，`walls[0]["id"] == "W1"`，`attempts == []`。
  - `test_impact_score`：size=0.3、gap=0.7、confidence="high" ⇒ 0.21（`pytest.approx`）。legacy 票 ⇒ 0.0。
  - `test_verdict_useful_breaks_wall`：`useful` ⇒ 当前第一道 open 的墙变成 broken；actions 含 `next_wall`；streak 归零。
  - `test_verdict_not_retained_requires_new_wall`：`wall_broken_not_retained` 不带 `new_wall` ⇒ `ValueError`。带上 ⇒ 多一道 W2，actions 含 `next_wall`。
  - `test_verdict_useless_streak_returns_at_3`：连续 3 次 `useless` ⇒ 第三次的 actions 含 `return_to_user`；前两次只含 `change_direction`。
  - `test_verdict_harmful_requests_rollback`：actions 含 `rollback`。
  - `test_pending_sample_capped_7_days`：第一次 `pending_sample` 的时刻是 t0；t0+7 天之后再记 `pending_sample` ⇒ `ValueError("待样本已满 7 天")`。
- [ ] **Step 2:** 运行 `python3 -m pytest tests/router/test_business.py -q`。预期：FAIL（导入失败）。
- [ ] **Step 3:** 在 `store.py` 里实现上面的 Produces。`_ticket_defaults` 不加 `kind`，靠 `ticket_kind` 兜底，这样老 state 不用迁移。
- [ ] **Step 4:** 运行 `python3 -m pytest tests/router -q`。预期：全绿，371 + 新增。
- [ ] **Step 5:** 提交：`feat(router): 业务票数据模型（墙/尝试/判定/影响分）`。

### Task 2: 排序与维护名额（scheduler.py、store.py）

**Files:**
- Modify: `skills/router/scripts/scheduler.py`（`pick_dispatch` :28 的排序键 :35）
- Modify: `skills/router/scripts/store.py`（`queue_view` :335 的排序 :345；`QUEUE_KINDS` :41）
- Test: `tests/router/test_scheduler.py`、`tests/router/test_store.py`

**Interfaces:**
- Consumes: Task 1 的 `ticket_kind`、`impact_score`。
- Produces:
  - `QUEUE_KINDS` 加四种：`verify_problem`（判真）、`close`（提议结束）、`harm`（有害，已撤或待撤）、`return`（连续没用，回到用户）。
  - `pick_dispatch(state, slots, heavy_blocked=False, maint_slots=1)`：
    - 排序键 = `(kind_rank, -impact_score, priority, 票号)`，`kind_rank`：business=0、legacy=1、maint=2；
    - 同时在跑的 maint 票不超过 `maint_slots`，带 `unblocks` 字段的 maint 票不受这个限制。
  - `queue_view(state)`：
    - 排序键 = `(-impact, -blocks, Q 序号)`；
    - `impact` 取这一项所属票的 `impact_score`；`verify_problem` 项取提议里的 impact。

- [ ] **Step 1: 写失败测试**
  - `test_business_dispatched_before_legacy_by_impact`：两张业务票（影响 0.1、0.4）、一张 legacy 票（priority 1）⇒ 派发顺序为 0.4、0.1、legacy。
  - `test_maint_cap`：3 张 maint 票、名额 3、`maint_slots=1` ⇒ 只派 1 张；再加一张带 `unblocks` 的 ⇒ 派 2 张。
  - `test_queue_view_orders_by_impact`：两个 card 项，所属票影响 0.2 / 0.5 ⇒ 0.5 的排在前面，不管 blocks 是多少。
  - `test_legacy_queue_order_unchanged`：全是 legacy 票（影响都是 0）⇒ 顺序和 v1 一样（按 -blocks）。
- [ ] **Step 2:** 跑这两个测试文件。预期：新增的测试 FAIL。
- [ ] **Step 3:** 实现。`maint_slots` 从 `cfg.get("analyst", {}).get("maint_slots", 1)` 读，由 tick 调用时传进来。
- [ ] **Step 4:** 跑全量测试。预期：全绿。
- [ ] **Step 5:** 提交：`feat(router): 派发与决定单按业务影响排序，维护票限名额`。

### Task 3: 提议与判真（router.py：`propose`、`answer`、`add --business`）

**Files:**
- Modify: `skills/router/scripts/router.py`
  - 新增 `cmd_propose`；
  - `cmd_answer` :359 加 `verify_problem` 分支；
  - `cmd_add` :264 加 `--business`；
  - 在 `build_parser` :899 登记。
- Create: `tests/router/fixtures/proposals.json`（合成数据，不含真实数据）
- Test: `tests/router/test_business_cli.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `new_business_ticket`；Task 2 的 `verify_problem` 队列类型；`github.load_guard` 和 `github.leak_hits`。
- Produces:
  - **`propose_from(env, st, data) -> dict`**：`cmd_propose` 的内部函数，Task 5 收尾 weekly / migrate 时复用。
  - **`router.py propose --from <file>`**
    - 输入：`{"mode":"weekly"|"migrate", "problems":[{"key","title","segment","want","walls":[...],"metric":{"name","now","direction"},"evidence":[...],"impact":{"size","gap","confidence"},"absorbs":[...],"new_evidence":""}], "maint":[{"title","problem","surfaces":[...],"unblocks":""}], "walls":[{"ticket","text"}], "close":[{"ticket","reason"}]}`。
    - 每个 problem 写进 `state["proposals"][P<n>]`，并推一个 `verify_problem` 项。
    - `maint` 交给 Task 7 的 `maint_add`。
    - `walls`：挂到已开的业务票上（`attach_wall`，`source="weekly"`），不用用户判真。
    - `close`：每条推一个 `close` 项（spec §4.5：分析 agent 提议结束，用户确认）。
    - 输出 `{"ok":true,"proposals":[...],"skipped":[...],"maint":[...]}`。
  - **拒收规则（整份拒，不写任何东西）：**
    - 校准还没通过（`state["analyst"]["calibrated"] is not True`），且 `mode=="weekly"`：拒，error 写「校准未过」。
    - 任一 problem 的 title、segment、want、walls、metric、evidence 文本被防泄漏闸命中：拒。
  - **跳过（只跳这一条）：** `key` 已经在 `<home>/analyst/rejected.jsonl` 里，而且 `new_evidence` 是空的，就记进 `skipped`。
  - **`answer Q<n> "<原话>"` 回答 `verify_problem` 项：**
    - 以「真」开头：建业务票。原话里不含「先放着」，就当场 start（建 issue 进队列，复用 `cmd_start` 的内部函数）。
    - 有 `absorbs` 时：被吸收的老票 `parent` 设成新票号；老票若已有上线验收结论，作为一条历史尝试写进新票的 `attempts`。
    - 以「不是」开头：必须带「：理由」，然后追加一行到 `rejected.jsonl`，格式 `{"key","title","reason","at"}`。
    - 其他写法：返回 error，提示格式「真 / 真，先放着 / 不是：理由」。
  - **`add --business --segment S --want W --wall X [--wall Y] --metric M`：** 用户直接提，等于判真，origin 记为 `user`。
  - **`answer Q<n>` 回答 `close` 项：** 以「关」开头 ⇒ 票 status=done，记 decide；以「不关」开头 ⇒ 不变；其他写法 ⇒ error 并提示格式。

- [ ] **Step 1: 写失败测试**（夹具里的人群、墙都用虚构的文本，例如「测试人群甲」）
  - `test_propose_pushes_verify_items`：2 个 problem ⇒ 推 2 个 `verify_problem` 项，`state["proposals"]` 有 2 条。
  - `test_propose_refused_before_calibration`：没校准时 `mode=weekly` ⇒ `ok:false`，error 含「校准未过」。`mode=migrate` 不受这条限制。
  - `test_propose_leak_refused`：夹具的 guard 文件里放一个假 uid `deadbeef`，problem 的 evidence 里也含它 ⇒ `ok:false`，state 不变。
  - `test_propose_skips_rejected_key`：rejected.jsonl 里有 key=k1 ⇒ k1 进 `skipped`；k1 带 `new_evidence` ⇒ 照常推。
  - `test_answer_true_creates_and_starts`：回「真」⇒ 新票 `kind=business`、`authorized=True`、status 是 queued，用 FakeCtx 记下了建 issue 的调用。
  - `test_answer_true_hold`：回「真，先放着」⇒ 票建好了，但 status 是 draft，不建 issue。
  - `test_answer_not_requires_reason`：回「不是」⇒ error。回「不是：样本是测试机」⇒ rejected.jsonl 多一行。
  - `test_answer_garbage_rejected`：回「好的」⇒ error，含格式提示。
  - `test_weekly_walls_attach_and_close_queues`：data 里有 1 条 walls、1 条 close ⇒ 对应的票多一道墙，并且多一个 `close` 项。回「关」⇒ 票变成 done。
  - `test_absorbs_links_legacy`：absorbs 了 T5 ⇒ T5 的 `parent` 是新票号，新票 `attempts` 有一条 `historical: true`。
- [ ] **Step 2:** 跑 `python3 -m pytest tests/router/test_business_cli.py -q`。预期：FAIL。
- [ ] **Step 3:** 实现。防泄漏闸复用 `cmd_start` 建 issue 前的那套 guard 加载（router.py:300）。
- [ ] **Step 4:** 跑全量测试。预期：全绿。
- [ ] **Step 5:** 提交：`feat(router): 分析 agent 提议 → 用户判真 → 业务票（含吸收老票）`。

### Task 4: 判定、上线与尝试循环（router.py：`verdict`、`released`；`apply_daily`）

**Files:**
- Modify: `skills/router/scripts/router.py`
  - 新增 `cmd_verdict`；
  - `cmd_released` :672 加上业务票分支；
  - 新增 `apply_daily(env, st, data) -> dict`。
- Test: `tests/router/test_business_cli.py`（续）

**Interfaces:**
- Consumes: Task 1 的 `add_attempt`、`record_verdict`；`_resume` router.py:162；`push_queue`。
- Produces:
  - `router.py released --tickets T --what … --rollback "<撤回办法>"`：业务票必须带 `--rollback`。
    - 这张票还有尝试的最终判定是空的，或者是 `pending_sample` ⇒ 拒收。
    - 否则调 `add_attempt`。状态进 `postlaunch`，但不排 T+1/T+3/T+7，验收交给 daily。
  - `router.py verdict T --attempt N --verdict V --touched a --hit-wall b --continued c --note "…" [--new-wall "…"]`，按 `record_verdict` 返回的 actions 执行：
    - `next_wall` 或 `change_direction` ⇒ `_resume(..., "direction", "<结论 + 下一步>")` 叫醒 worker，状态回到 executing。
    - `return_to_user` ⇒ 推一个 `return` 项。
    - `rollback` ⇒ 推一个 `harm` 项，同时设唤醒键 `rollback_needed`，内容是 `{ticket, rollback}`。
    - `check_entry` ⇒ 设唤醒键 `no_entry`。
    - `wait` ⇒ 什么都不做。
  - `apply_daily(env, st, data)`：
    - 输入格式：`{"verdicts":[{ticket, attempt, verdict, touched, hit_wall, continued, note, new_wall}], "walls":[{ticket, text}], "notes": "…"}`。
    - 对每条判定调 `verdict` 的内部函数；对每条墙调 `attach_wall`（`source="daily"`）。
    - `note` 和墙的文本先过防泄漏闸。命中的条目跳过，并记进 errors。

- [ ] **Step 1: 写失败测试**
  - `test_released_business_requires_rollback`：业务票不带 `--rollback` ⇒ error。
  - `test_released_refuses_second_inflight`：第 1 次尝试的判定还是空的，再 released ⇒ error 含「上一次尝试还没判」。
  - `test_verdict_useless_resumes_worker`：FakeCtx 记下了 resume 的调用，inbox 里有「换方向」。
  - `test_verdict_harmful_queues_harm_and_wakes`：多了一个 `harm` 项；tick 结果里有 `rollback_needed`，带上那条 rollback 文本。
  - `test_apply_daily_attaches_wall_and_skips_leak`：一条正常的墙加上；一条含假 uid 的墙被跳过，并记进 errors。
  - `test_legacy_released_unchanged`：legacy 票 released ⇒ 照旧排 3 个检查点，不要求 `--rollback`。
- [ ] **Step 2:** 跑这些测试。预期：FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 跑全量测试。预期：全绿。
- [ ] **Step 5:** 提交：`feat(router): 业务票的上线/判定/尝试循环`。

### Task 5: 分析 agent 运行器（analyst.py、tick.py、router.py analyst）

**Files:**
- Create: `skills/router/scripts/analyst.py`
- Modify: `skills/router/scripts/tick.py`
  - `step0_weekly` :243：不再设 `triage_needed`，改成触发 weekly 跑；
  - 新增 `step_analyst`，接进巡检顺序；
  - `WAKE_KEYS` :28 加 `analyst_done`、`rollback_needed`、`no_entry`、`return_to_user`、`releases_unlabeled`，删掉 `triage_needed`。
- Modify: `skills/router/scripts/router.py`：新增 `analyst` 子命令组。
- Test: `tests/router/test_analyst.py`（新建）

**Interfaces:**
- Consumes: `Ctx.spawn_detached` ctx.py:50；Task 3 的 `propose_from(env, st, data) -> dict`；Task 4 的 `apply_daily(env, st, data) -> dict`。
- Produces:
  - `analyst.build_cmd(cfg, prompt: str) -> list`：`[claude_bin, "-p", prompt, "--model", model, "--output-format", "json", "--permission-mode", pm]` 加上 allowed/disallowed tools。
    - `claude_bin` 和 `permission_mode` 取 `cfg["worker"]` 的；`model` 取 `cfg["analyst"]["model"]`，缺省 `"claude-fable-5-1"`。
  - `analyst.render(env, mode, since, until, out_path) -> str`：
    - 读 `templates/analyst_<mode>.md`，用 string.Template 代入 `$brief`（config `analyst.brief` 的路径）、`$out`、`$since`、`$until`、`$state_summary`；
    - `mode=="calibrate"` 时，`$state_summary` 一律代入空串，模板本身也不许出现票和墙的信息。
  - `analyst.start(env, st, mode, since=None, until=None) -> str`：
    - 起后台进程，写 `state["analyst"]["runs"][run_id] = {mode, pid, started_at, out, log}`，返回 `run_id`；
    - 同一个 `mode` 已经有活着的 run，就不再起，返回已有的 `run_id`。
  - `Run.step_analyst()`，每轮做三件事：
    1. **daily：** UTC 当前时刻 ≥ config `analyst.daily_at_utc`（缺省 `"01:30"`），且 `state["analyst"]["last_daily"]` 不等于今天 ⇒ `start("daily")`，并写 `last_daily`。即使失败也写，次日才重跑。
    2. **weekly：** 发现新周报（原来 `step0_weekly` 的判定）⇒ `start("weekly")`，记 `state["analyst"]["last_weekly"]`。同时读这期 `summary.json` 的 `release_sync.unlabeled`：不为空 ⇒ 设唤醒键 `releases_unlabeled`，内容就是这个清单，由 router 转给用户。这件事原来由分诊转。
    3. **收尾：** run 的进程已退出 ⇒ 读 out。
       - JSON 坏了或文件不存在：报一次 error，标 `run.failed=true`，不改其他状态。
       - daily ⇒ `apply_daily`；weekly / migrate ⇒ `propose_from`；calibrate ⇒ 只设唤醒键 `analyst_done`，由 router session 判过没过。
  - CLI：
    - `router.py analyst run --mode {daily,weekly,calibrate,migrate} [--since D] [--until D]`
    - `router.py analyst calibrated --pass|--fail --note "<…>"`：写 `state["analyst"]["calibrated"]` 和 `calibrated_note`。

- [ ] **Step 1: 写失败测试**（用 `fake_claude.py`，加一个 `OUT_JSON` 环境变量，让它把指定内容写到 prompt 里 `$out` 指向的路径）
  - `test_build_cmd_uses_fable_default`：config 没有 `analyst.model` ⇒ 命令里含 `--model claude-fable-5-1`。
  - `test_calibrate_prompt_has_no_state`：state 里有一张业务票，标题是「测试墙甲」⇒ `render("calibrate")` 的结果里不含「测试墙甲」。
  - `test_daily_runs_once_per_day`：时钟拨到 01:31Z，tick 两轮 ⇒ 只起 1 个进程；拨到次日 01:31Z ⇒ 再起 1 个。
  - `test_daily_failure_no_respawn`：分两种情况：fake 不写 out 就退出；fake 写出坏 JSON。两种都应该 errors 有 1 条，state 只多了 `runs[..].failed`；当天再跑 tick ⇒ 不重起。
  - `test_weekly_replaces_triage`：放一个带 `sent` 的新周报目录 ⇒ 起 weekly run；结果里没有 `triage_needed` 键。summary.json 的 `release_sync.unlabeled` 是 `["x"]` ⇒ 结果里有 `releases_unlabeled`。
  - `test_weekly_output_becomes_proposals`：校准已过，fake 写出 1 个 problem ⇒ 收尾后多 1 个 `verify_problem` 项。
- [ ] **Step 2:** 跑 `python3 -m pytest tests/router/test_analyst.py -q`。预期：FAIL。
- [ ] **Step 3:** 实现。`fake_claude.py` 加 `OUT_JSON`：从 argv 的 prompt 里用正则 `OUT=(\S+)` 找出路径，再写进去。所以模板里必须有一行 `OUT=$out`。
- [ ] **Step 4:** 跑全量测试。预期：全绿。原来断言 `triage_needed` 的测试（test_tick.py:252 等）改成断言 weekly run 已经起来，每改一处都写进提交说明。
- [ ] **Step 5:** 提交：`feat(router): 分析 agent 运行器（Fable，每日/每周/校准/迁移）替代分诊`。

### Task 6: 票级授权的闸门（submit-card、走查、prod-batch、停点标签）

**Files:**
- Modify: `skills/router/scripts/router.py`
  - `cmd_submit_card` :426；
  - `_prod_item` :645 和 `cmd_prod_batch` :654。
- Modify: `skills/router/scripts/tick.py`
  - 合并后进走查的地方（:594 附近，以及决定下一步是 handson 还是 await_prod 的那段）；
  - `STOP_KIND` 的使用处 :311。
- Test: `tests/router/test_business_cli.py`、`tests/router/test_tick.py`

**Interfaces:**
- Consumes: Task 1 的 `ticket_kind` 和 `authorized`。
- Produces（以下只对 `kind=="business" and authorized` 的票生效）：
  - `submit-card`：不推 card 项，直接视为已确认。
    - 记一条 decide：「方向已确认（票级授权）」，`basis` 写卡的路径；
    - 设 `card_confirmed=True`，用 `_resume(..., "card", "方向已确认（票级授权）")` 叫醒 worker。
    - 方向审（Opus + codex）照旧由 router session 先做完，再 submit。
  - 合并 staging 之后：不推 handson 走查项，直接进 `await_prod`。
  - `prod-batch`：不把这类票放进给用户的 prod 决定项；输出里单独列 `router_ships: [T…]`，由 router session 照项目流程上 prod。
  - 停点：worker `decision` 事件的 tags 和 `{"spend","irreversible","store_release"}` 有交集 ⇒ 推 `spend` 项（交用户）。这一条对所有票都生效。

- [ ] **Step 1: 写失败测试**
  - `test_business_card_auto_confirmed`：submit-card 之后队列里没有 card 项；decisions.md 多一行，含「票级授权」；worker 被 resume。
  - `test_legacy_card_still_to_user`：legacy 票 ⇒ 照旧推 card 项。
  - `test_business_no_handson_after_merge`：业务票合并之后 ⇒ status 是 `await_prod`，没有 handson 项。
  - `test_prod_batch_separates_router_ships`：一张 legacy、一张 business ⇒ 决定项只包含 legacy，`router_ships == ["T2"]`。
  - `test_irreversible_tag_goes_to_user`：`decision` 事件带 `tags=["irreversible"]` ⇒ 推 `spend` 项。
- [ ] **Step 2:** 跑这些测试。预期：FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 跑全量测试。预期：全绿。
- [ ] **Step 5:** 提交：`feat(router): 票级授权——业务票方向/走查/上 prod 由 router 推进`。

### Task 7: 维护票（router.py：`maint add`）

**Files:**
- Modify: `skills/router/scripts/router.py`：新增 `maint` 子命令组，以及内部函数 `maint_add(env, st, item) -> str`。
- Test: `tests/router/test_business_cli.py`（续）

**Interfaces:**
- Consumes: Task 1 的 `KINDS`，Task 2 的名额规则。
- Produces: `router.py maint add --title … --problem … --surfaces a,b [--unblocks T]`
  - 不带 `--unblocks`：追加到本周的维护票。这张票 `kind="maint"`，标题是「维护 <ISO 周，如 2026-W46>」，没有就先建；条目进它的 `tasks`，`surfaces` 取并集。
  - 带 `--unblocks T`：单独建一张 maint 票，`unblocks=T`，不受维护名额限制。
  - 返回票号。

- [ ] **Step 1: 写失败测试**
  - `test_maint_add_appends_weekly`：同一周加两条 ⇒ 只有 1 张维护票，`tasks` 有 2 条。
  - `test_maint_unblocks_standalone`：带 `--unblocks T3` ⇒ 单独一张票，`unblocks == "T3"`。
  - `test_maint_new_week_new_ticket`：时钟拨到下一周 ⇒ 新建一张。
- [ ] **Step 2–4:** 先红后绿，再跑全量测试。
- [ ] **Step 5:** 提交：`feat(router): 每周维护票 + 挡验证例外`。

### Task 8: 看板与 status（board.py、router.py `cmd_status`）

**Files:**
- Modify: `skills/router/scripts/board.py`：`_ticket_card` :75、决定单那一行 :181。
- Modify: `skills/router/scripts/router.py`：`_summary` :801。
- Test: `tests/router/test_board.py`

**Interfaces:**
- Consumes: Tasks 1、2。
- Produces:
  - 业务票的卡片显示：人群、想要什么、墙（open 的醒目、broken 的划掉）、指标（现值和方向）、最近 3 次尝试（方向、上线日、判定）、影响分。
  - 决定单这一行：业务票和 `verify_problem` 项显示「影响 0.21」，legacy 项照旧显示「挡 N 张」。
  - `status --json`：每张票加 `kind`；业务票再加 `walls_open` 和 `last_verdict`。

- [ ] **Step 1: 写失败测试**
  - `test_business_card_shows_walls_and_attempts`：渲染出的 HTML 含墙的文本，broken 的墙带 `<s>`，并含最近一次判定。
  - `test_queue_row_shows_impact_for_business`：含「影响 0.21」。
  - `test_legacy_card_unchanged`：用一张 legacy 夹具票，对比现有快照断言，输出不变。
- [ ] **Step 2–4:** 先红后绿，再跑全量测试。
- [ ] **Step 5:** 提交：`feat(router): 看板按业务票展示（墙/尝试/影响分）`。

### Task 9: 模板与协议文档（templates、SKILL.md、worker.md、brief.md、test_docs.py）

**Files:**
- Create: `skills/router/templates/analyst_daily.md`、`analyst_weekly.md`、`analyst_calibrate.md`、`analyst_migrate.md`
- Delete: `skills/router/templates/triage.md`、`tests/router/triage_eval_check.py`、`tests/router/fixtures/triage_eval/`
- Modify: `skills/router/SKILL.md`、`skills/router/worker.md`、`skills/router/templates/brief.md`、`tests/router/test_docs.py`

**内容要求（模板文字由实现者写，下面这些必须有）：**
- 四个模板都要有：
  - 一行 `OUT=$out`；
  - 一个 ```json 输出示例，字段和 Task 3、Task 5 的输入一致；
  - 一句红线：会进 GitHub 的字段（title、segment、want、walls、metric、evidence）不写 uid 和原话。
- daily：把 spec §5.3 写成三步：覆盖 → 读对话 → 判定。判定词只能用 `VERDICTS`。分母只算走到墙的人；不到 3 人就判 `pending_sample`。
- weekly：
  - 方法写 spec §6 的五条；
  - 每个 problem 都要有 impact 三项；
  - 维护项放进 `maint`；
  - 先读 `$home/analyst/rejected.jsonl`，被否过的同类问题不再提，除非填了 `new_evidence`。
- calibrate：只给数据入口和时间范围，不出现 `$state_summary`。
- migrate：给出现有票清单（`$state_summary`），按「同一群人想要的同一件东西」归并，用 `absorbs` 表达归并关系。
- SKILL.md 要改的地方：
  - §1 启动：去掉分诊，加上分析 agent 的状态和校准状态；
  - §2 表格：删掉 `triage_needed` 行，加 `analyst_done` / `rollback_needed` / `no_entry` / `return_to_user` / `releases_unlabeled` 五行，每行写清 router 怎么做。`rollback_needed` 要写明「当天照 rollback 文本撤回，并告诉用户」；
  - §3：改成「分析 agent」，写清校准流程：先盲跑 calibrate，router 对照项目 brief 里的已知问题判过没过，再跑 `analyst calibrated`，然后跑 migrate；
  - §4：业务票的方向卡照旧做方向审，submit 后自动确认；
  - §5 谁定什么：加上「判真 = 用户」「票内换方向、上 prod = router（票级授权）」「花钱 / 撤不回 / 发商店包 = 用户」；
  - §6：加上用户说「真」「真，先放着」「不是：…」「关」「不关」时怎么处理；
  - §7：决定单改按影响排。
- worker.md / brief.md：
  - 加「业务票」一节：一张票就是一个问题；一次尝试 = 一个方向，必须带开关；上线前先做离线回放（spec §5.2），要花钱就停下来报价；上线后等判定再做下一个方向；
  - 停点标签 `spend|irreversible|store_release`。
- test_docs.py：
  - 删掉锁 triage.md 的那些测试（:55、:106、:148–320、:297）；
  - 新增 `test_analyst_templates_contract`：四个模板都有 `OUT=$out`、能解析的 json 示例、红线句；calibrate 不含 `$state_summary`。
  - 新增 `test_skill_v2_sections`：SKILL.md 含「票级授权」「判真」「rollback_needed」，不含「triage_needed」。
  - 现有那条锁「挡住了几张票」的断言，改成锁「按业务影响」。

- [ ] **Step 1:** 先改 test_docs.py，写成新契约。运行 `python3 -m pytest tests/router/test_docs.py -q`。预期：FAIL。
- [ ] **Step 2:** 写四个模板，改 SKILL.md、worker.md、brief.md，删除 triage 相关文件。
- [ ] **Step 3:** 跑全量测试。预期：全绿。
- [ ] **Step 4:** 跑防泄漏扫描。运行：
  ```bash
  python3 -c "import sys,json,subprocess;sys.path.insert(0,'skills/router/scripts');import github;g=github.load_guard(json.load(open('/Users/apple/works/viber-code/aifriends/.claude/router/config.json'))['leak_guard_globs']);print(github.leak_hits(subprocess.run(['git','log','-p','origin/main..HEAD'],capture_output=True,text=True).stdout,g))"
  ```
  预期：`[]`。
- [ ] **Step 5:** 提交：`docs(router): v2 协议——分析 agent 模板、票级授权、业务票 worker 循环`。

### Task 10: 项目接线与上线（aifriends 侧，不进 claude-skills）

**Files（都在 aifriends 的 `.claude/router/`，已被 gitignore）：**
- Modify: `config.json`，加：
  ```json
  "analyst": {"model": "claude-fable-5-1", "daily_at_utc": "01:30", "brief": "<abs>/analyst_brief.md", "maint_slots": 1}
  ```
- Create: `analyst_brief.md`，写项目专属内容：
  - 取数入口：prod 只读查询的命令、周报目录、对话原文的位置、前端覆盖查询（埋点里的 `h5_build`）、图片轮快照键；
  - 已知问题清单：给校准判定用，不进模板；
  - 红线。

- [ ] **Step 1:** 开 PR（base main），过防泄漏扫描。合并后在 `/Users/apple/works/claude-skills` 跑 `git pull`。`~/.claude/skills/router` 是指向这个仓的软链，pull 完即生效。
- [ ] **Step 2:** 写 config 和 brief。在 router session 里跑 `router.py analyst run --mode calibrate --since <两周前> --until <今天>`。
- [ ] **Step 3:** router 对照 brief 里的三个已知问题判校准过没过，跑 `router.py analyst calibrated --pass|--fail`，把结果和它找到的其他问题一起交给用户。
- [ ] **Step 4:** 校准通过后跑 migrate，产出的迁移提议进决定单，由用户逐条判真。
- [ ] **Step 5:** 第一个 daily 跑完后，核对：判定都落到了对应的票上；没有误触发撤回；看板显示正常。
- [ ] **Step 6:** 周报第一屏放问题清单（spec §7 第 8 条）。用 `router.py maint add` 开一条维护项，代码面 `weekly-report`：让周报读 `<router home>/analyst/` 下最新一份 weekly 的产出，在第一屏列出问题清单。走 v2 流程实现。
