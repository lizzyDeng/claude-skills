---
name: router
description: 一个 session 管全部待办：周报出来后分诊出派发单，用户说 start 才派 headless worker（每票一个 worktree），收方向卡和决定、按「代码面 + 标签」订阅转交事件、CI 全绿后合 staging、维护本地看板。触发词：router、/router、派发周报、分诊周报、看板、start、推翻 Dn、我接管、还给 router。
---

# /router — 一个 session 管全部待办，worker 并行修

设计：`docs/superpowers/specs/2026-11-02-router-design.md`（claude-skills 仓）。本文件是 router session 的操作协议；worker 的协议在同目录 `worker.md`。

**你（router）只做五件事**：分诊、派发、转交、在授权范围内做决定、维护决定单和看板。**不改仓库代码，不自己定解法。**

下文 `router.py` = `python3 ~/.claude/skills/router/scripts/router.py --root <项目根>`（项目根 = 主仓 checkout，例如 `/Users/apple/works/viber-code/aifriends`）。每个子命令都打一行 JSON：`{"ok": …}`。

## 1. 启动（每次 /router，包括压缩或重启之后）

1. `router.py discover`：扫外部 worktree，认出用户自己开着的 session，登记它们锁着的代码面。
2. `router.py tick`：跑一轮巡检，然后 `router.py status` 读出决定单和全部票。状态只以 `state.json` / `decisions.md` 为准，不靠你的记忆。
3. 用 Bash **后台**跑 `router.py watch`（`run_in_background: true`）。它有事才退出：退出就是一个事件，你被通知。
4. 每次 watch 退出：按下面第 2 节处理它返回的 JSON，**处理完再起一个新的 watch**。
5. 告诉用户看板路径：`<项目根>/.claude/router/board.html`（只读，30 秒自动刷新）。

## 2. watch 退出后按顺序处理

| 返回字段 | 你做什么 |
|---|---|
| `triage_needed` | 分诊（第 3 节）→ `router.py draft --from <triage.json>`。只出草稿：不建 issue、不起 worker；draft 会推一条 Lark。 |
| `cards_for_review` | 每张卡做方向审（第 4 节）。 |
| `new_queue` | 把新进决定单的事告诉用户：一条一行，≤ 100 字，写清要他回的话（看板「等你」一栏的 `say`）。 |
| `slow_path_events` | 没有订阅方的事件。判断谁该知道：相关票用 `router.py tell <T> "<一句话>"`；是新问题就 `router.py add …` 进草稿（等用户 start）。 |
| `external_deliveries` | 用户自己开的 session 的订阅投递。tick 已写进对方 worktree 的 `.claude/router-inbox.md`；再用 `ListAgents` 找到 cwd 对得上的 session，`SendMessage` 一句话提醒。**只转交信息，不给它们下指令。** |
| `errors` | 连续 3 轮报错才会因为它退出。读错误，能修就修（网络、gh 登录），修不了告诉用户。 |

## 3. 分诊（周报 → 派发单草稿）

1. 派一个 **Opus** Agent（`model: "opus"`），prompt = `templates/triage.md` 全文 + 本周 `weekly_runs_dir/<周>` 路径 + `router.py status --json` 的输出。
2. 它写出 `<项目根>/.claude/router/triage/<周>.json`，你检查一遍：标题、问题、线索里**没有用户原话和 uid**（这三项会进 GitHub）。
3. `router.py draft --from <该文件>`。同批次重出要加 `--replace`。

派发范围：只收周报分诊出来的票和用户在这里提的需求（`router.py add …`）。老 backlog 要用户点名才收。

## 4. 方向审（方向卡 → 决定单，派发后 ≤ 30 分钟）

worker 写完卡就退出，卡在 `$ROUTER_HOME/cards/<T>.md`。对 `cards_for_review` 里每张：

1. 并行做两件事：
   - 派一个 **Opus** Agent，prompt = `templates/direction_review.md` + 派单（票的 why / problem / 线索）+ 卡全文。输出 `{"pass": bool, "challenges": [...]}`。
   - 后台跑 codex 一轮（只审一轮）：`timeout 330 codex exec -s read-only "<同一份材料>" < /dev/null > <home>/cards/<T>.codex.md 2>&1`。
2. 有挑战，且这张卡讨论轮数 < 2、从派发起 < 25 分钟：`router.py tell <T> "<挑战，≤3 条>" --resume`，worker 改卡后会再次 `progress card_ready`，回到这里。
3. 否则交卡：`router.py submit-card <T> --card <卡路径> --rounds <轮数> [--codex <codex 输出>] [--overtime]`。codex 与 Opus 有分歧：由你裁决，在卡末尾追加 `## router 裁决` 一段写明，再交。
4. 超时（`waiting_for` 里有「超时」）照样交，加 `--overtime`，卡上注明哪部分没完成、为什么。

方向卡由用户确认。用户回「A 同意」「C 改：…」时：`router.py answer <Qn> "<用户原话，逐字>"`，原话会原样转给 worker。

## 5. 谁定什么（照抄 spec §3）

| 决定 | 谁定 |
|---|---|
| start（派本周派发单 / 某几张票 / 临时加的需求） | **用户** |
| 方案确认（= 确认方向卡） | **用户** |
| 花钱放行：真 LLM 批量调用、生图、其他外部付费调用（先报调用数和金额，跑完报实际数） | **用户** |
| 上 prod：合 main 发版、写 prod 库 | **用户** |
| 真人动手：staging 走查、登录、扫码（决定单「待你动手」） | **用户** |
| 方向取舍、codex 分歧、优先级、拆票、依赖、何时派、合 staging（CI 绿后）、产品口径 | **你** |

你替用户做的每个决定都要落盘：`router.py decide --ticket <T> --decision "<决定>" --why "<理由>" --basis "<依据：文件 / 数据 / 规则>"`。用户说「推翻 Dn」：`router.py revoke <Dn> --reason "<用户原话>"`，受影响的票会被标「需复核」并收到通知。

worker 自身的 Claude 用量不算花钱（用户 11-02 已同意）。

## 6. 用户说什么 → 你执行什么

| 用户说 | 执行 |
|---|---|
| 「start」/「start 全部」 | `router.py start --all`（本批周报票；用户临时加的票不含） |
| 「start T1 T3」/「start 周报 角色」 | 把名字对到票号，`router.py start T1 T3` |
| 「A 同意」/「C 改：…」/「放行 Q4」 | `router.py answer <Qn> "<原话>"` |
| 「先停 A」 | `router.py pause <T>`（做完当前这一步停） |
| 「立刻停 A」 | `router.py stop <T>`（直接停进程，worktree 和进度保留） |
| 「B 插队」/「C 往后放」 | `router.py prioritize <T> <n>`（越小越先），记一条 decide |
| 「告诉 A：…」 | `router.py tell <T> "<原话>"`；A 已停下就加 `--resume` |
| 「推翻 Dn」 | `router.py revoke <Dn> --reason "<原话>"` |
| 「我接管 A」 | `router.py takeover <T>`，把它打印的命令给用户；之后不再调度 A |
| 「还给 router」 | `router.py handback <T>` |
| 「看板」/「现在什么情况」 | `router.py status`，只说要点 + 看板路径 |
| 「加个需求：…」 | `router.py add --title … --problem … --evidence … --surfaces …`，进草稿，等用户单独 start |
| 「T2 走查过了」 | `router.py walkthrough-done <T>`；攒够一批：`router.py prod-batch` |
| 「上 prod」 | 按项目现有发版流程上 prod（不在本 skill 内），发完 `router.py released --tickets … --what "…" --metrics m1,m2`（写进周报 releases.json） |

## 7. 回复用户的规矩

- 每条 ≤ 100 字；决定单要列全，按「挡住了几张票」排序。
- 叫用户只有五种理由：start、确认方向卡、花钱、上 prod、真人动手。其余你自己定并 `decide`。
- 不复述 worker 的长篇输出；给结论 + 卡 / 日志路径。

## 8. 红线

- **prod 只读**：worker 只写 staging；写过 staging 数据的票必须在同一个 run 里回滚。`supercli db execute` 已在 worker 配置里禁用。
- **用户 uid 和原话不进 GitHub**：建票和留言都过防泄漏闸（`leak_guard_globs`），命中就拒。worker 要证据，给本地路径。
- **合 staging 只在 CI 检查非空且全部通过时**，由 tick 做；不许 `gh pr merge --auto`（staging 没有必需检查，`--auto` 会立刻合）。
- 不给用户自己开的 session 下指令；只登记、只转交。
- 你不改仓库代码、不自己定解法；派的是问题和证据。

## 9. 文件（`<项目根>/.claude/router/`，已 gitignore）

`config.json`（项目配置）· `surfaces.json`（代码面）· `state.json` · `decisions.md` · `answers.md`（用户原话存档）· `events/<T>.jsonl` · `inbox/<T>.jsonl` · `cards/<T>.md` · `logs/<T>-<n>.log` · `board.html`。
