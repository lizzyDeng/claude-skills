# E2E 报告 — Session Radar

- **Runner**：`python3 tests/session_radar/session_radar_e2e_runner.py -o tmp/session_radar_e2e_result.json`
- **结果**：`status=pass`，**18/18 turn 通过（通过率 100%）**，嵌套 `scenarios[].rounds[].turns` 计 18 turn（≥10）。
- **绑定的 result hash（gate.json `e2e_result_hash`）**：`ffa07addcddfb29d806afea02ccd0226157914a3deee92a04ca3627de57c32c5`
- **证据形态**：runner 造一个假 `~/.claude` home（8 个 fixture：drifted fg / 命令壳 fg / 错误 fg / worktree fg / alive bg / done bg / blocked bg / stateless bg / 30天旧 fg），直接调 `build_snapshot` 断言派生态，并 `subprocess` **真起 HTTP server** 用 urllib 打 `/healthz`、`/api/state`、`/`，每个 turn 断言**业务结果**（字段值 / 派生态 / HTTP body），非冒烟。

## AC 覆盖度（5 个 P0 全覆盖，每个映射到验证业务结果的 turn）

| P0 | 覆盖 turn | 业务断言 |
|----|-----------|----------|
| P0-1 liveness（bg 不靠 mtime；错误态） | 5, 11, 12 | stateless bg→`unknown`（非 active）；alive-but-stale bg→`working`（state 胜 mtime）；`API Error` 尾巴→`errored` |
| P0-2 当前 repo/worktree/branch | 9, 10 | 取事件流尾部：drifted 会话在 `aifriends`/`fix/provider`；worktree 行 `claude-skills ⟨wt:session-radar⟩`/`session-radar` |
| P0-3 opening→now drift + 命令壳 | 7, 8 | opening==真实 `build the session radar dashboard`（无 `<command-*>`）；diverge→`drift=True` |
| P0-4 范围=所有 session 含全部后台 | 1, 2, 3, 4, 6 | 7 session（含 4 类 bg）；done/blocked/stateless bg 全 surface；window=0 显示旧 fg（recency lens 非 scope cap） |
| P0-5 复用 web 壳 + --once | 13, 14, 15, 16, 17, 18 | `/healthz` 200；`/api/state` 7 session 且各派生态经 HTTP 保真；`/` HTML 引 `/api/state`；`--once` 含 repo/DRIFT/worktree；`--json` 含 7 session、bg=4 |

## 逐轮审查（完整输出）

```
 1 [pass] snapshot has 7 sessions (2 fg + 1 worktree fg + 4 bg)  -> total=7
 2 [pass] P0-4 scope: fg + all 4 bg kinds present; counts.bg == 4  -> bg=4 shorts=['0d0d0d0d','32ea05ca','aaaaaaaa','bbbbbbbb','beef5678','cafe1234','dddddddd']
 3 [pass] P0-4 done bg job (no transcript) surfaces with liveness 'done' + intent opening  -> done_row=done/'earlier finished job'
 4 [pass] P0-4 blocked bg job surfaces with liveness 'blocked'  -> blk_row=blocked
 5 [pass] P0-1 stateless bg job (no state.json) is 'unknown', never 'active' off fabricated age  -> nost_row=unknown
 6 [pass] P0-4 window is a recency LENS not a scope cap: old fg hidden@120, in scope@0  -> window opt-out surfaces all foreground sessions
 7 [pass] P0-3 command-shell stripped: opening is real human intent  -> opening='build the session radar dashboard'
 8 [pass] P0-3 drift flagged when current action diverges from opening  -> drift=True
 9 [pass] P0-2 current repo/branch derived from transcript TAIL (not opening worktree)  -> repo=aifriends br=fix/provider
10 [pass] P0-2 worktree session ROW exposes worktree+repo+branch from current cwd  -> wt_row=claude-skills ⟨wt:session-radar⟩/session-radar
11 [pass] P0-1 bg job alive-but-stale reads 'working' (state beats mtime)  -> live=working age=3600.0
12 [pass] P0-1 errored tail classified 'errored' (not reported as work)  -> live=errored
13 [pass] P0-5 server healthz 200  -> up
14 [pass] P0-5 /api/state 200 + all 7 sessions over HTTP  -> total=7
15 [pass] P0-5 /api/state preserves working+done+blocked+unknown+errored+worktree over HTTP  -> http snapshot coherent across all derived states
16 [pass] P0-5 GET / serves HTML that fetches /api/state  -> html ok
17 [pass] P0-5 --once table shows real rows: repo + DRIFT marker + worktree  -> once rows present
18 [pass] P0-5 --json snapshot carries the derived fields (7 sessions, bg=4)  -> json business fields present
```

## 总结

- 18/18 通过（100% ≥ 80%），5 个 P0 AC 全部由验证业务结果的 turn 覆盖，3 个硬问题（命令壳剥离 turn 7、bg state 权威 turn 5/11、错误态识别 turn 12）各有 E2E 证据。
- 弱 turn（13 healthz）仅作服务器起来的前置 gating，不计入任一 P0 核心覆盖；P0-5 的真实覆盖在 14/15/16/17/18。
- result 文件未在 runner 执行后被改；本报告引用的 `e2e_result_hash=ffa07addcddfb29d806afea02ccd0226157914a3deee92a04ca3627de57c32c5` 与 gate.json 记录一致，validator 会重算比对。
- **额外真实证据**：Step 3.0 冒烟已用本工具扫真实 `~/.claude`（10 session，8 bg，6 drifted），抓到本 fastship session 自身 `opening 做 SEED... → NOW Bash(...)` 的真实漂移，命令壳剥离与 stateless→unknown 在真数据上成立。
