# Router headless worker spike（2026-11-02）

环境：macOS Darwin 24.6.0，`claude` 2.1.316，从一个正在运行的 Claude Code session（router 的位置）的 Bash 里嵌套启动。

## 结论：**GO**

S1–S3 全部 PASS，headless 方案成立，不需要回退到 `claude -w --tmux`。

| # | 项 | 结果 | 关键证据 |
|---|---|---|---|
| S1 | 指定 session id 单发 | PASS | `session_id == $SID`、`is_error=false`、`result="OK"`，7.4 s |
| S2 | `--resume` 续跑 | PASS | 问「上一条回复了什么」→ `result="OK"`，同一 `session_id` |
| S3 | 父进程退出后 worker 存活 | PASS | `start_new_session=True` 起进程后父 python 立即退出，`PPID=1 PGID=pid`；约 95 s 后进程自然结束，log 里是完整结果 JSON：`"result":"DONE\n\n\`sleep 90\` 已跑完，退出码为 0。"` |
| S3b | 去掉 router 会话变量后起 worker | PASS | 剔除 `CLAUDECODE / CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_CHILD_SESSION / CLAUDE_CODE_MESSAGING_* / CLAUDE_CODE_SESSION_ATTENDED / CLAUDE_PID` 后，`Ctx.spawn_detached` 起的 worker 正常回 `OK2` |
| S4 | 权限与工具 | PASS（见下） | 两种模式下 Write / `git status` / Agent 子代理都能用；`--disallowedTools "Bash(supercli db execute*)"` 在两种模式下都拦住了 `supercli db execute --help`（`permission_denials` 有记录；用户 settings 里没有 supercli 规则，确认是参数生效） |
| S5 | 启动论证计时 | PASS | 真实票（#940 第 4 条「周报口径」），Opus 5.5，**214 s（3 分 34 秒）**，9 轮，$1.03（订阅额度，不算「花钱」）。8 项全部填写；最关键假设当场验证（读了 `state/weeks/*.json`，并推翻了票面上的一条前提） |

## 几个实测细节（实现必须照做）

1. **stdin 必须重定向**：不重定向时 `claude -p` 会等 3 s 并打印 `Warning: no stdin data received in 3s`。`Ctx.run` 和 `spawn_detached` 一律 `stdin=DEVNULL`。
2. **输出**：`--output-format json` 在进程结束时写**一个**结果对象（`type=result`、`subtype`、`is_error`、`result`、`session_id`、`num_turns`、`total_cost_usd`、`permission_denials`）。进程中途死掉就没有这个对象 → `read_result` 返回 None → 判 STUCK。
3. **会话变量**：router 自己的 `CLAUDE_CODE_*` 会话绑定变量会被子进程继承，worker 会被当成 router 的子会话。`spawn_detached` 要剔除（见 S3b）。
4. **acceptEdits 不够用**：它放行了 Write 和只读的 `git status`，但 `-p` 下没有人能点确认，`cargo test`、`gh pr create`、`git push` 这类命令会被直接拒绝。worker 要编译、推分支，所以用 `bypassPermissions`，再用 `--disallowedTools` 收回红线。

## worker 配置（Task 7 / Task 11 照抄）

```json
{
  "model": "claude-opus-5-5",
  "permission_mode": "bypassPermissions",
  "allowed_tools": [],
  "disallowed_tools": [
    "Bash(supercli db execute*)",
    "Bash(gh pr merge*)",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git push origin main*)",
    "Bash(git push origin staging*)",
    "AskUserQuestion"
  ]
}
```

- `supercli db execute`：prod 只读红线（写 prod 本来还需要用户本人登录，这里是第二道）。
- `gh pr merge`：合 staging 只由 router 在 CI 全绿后做。
- 强推、直推 main / staging：worker 只推自己的 `router/<T>` 分支。
- `AskUserQuestion`：worker 不直接问用户，只通过事件往上抛。
- 已知局限：Bash 规则按命令前缀匹配，`bash -c "…"` 这类包一层的写法拦不住。它是护栏，不是沙箱；prod 写入的真正闸门是 supercli 的人工登录。

## S5 观察

- 卡片质量够用：根因都带 `weekly.py:<行>`，A1 当场查数据验证并改写了方案（安全指标的对照窗口必须排除回填周），A2 推翻了票面上「复读盖住泄漏」的前提。
- 这正是 spec §6.2「只论证不验证会修三次」想拦住的情况：卡片在派单 4 分钟内就纠正了 router 线索里的一条错误前提。
- 15 分钟预算宽裕，不需要精简模板。方向审（router 挑战 + codex 一轮）有 25 分钟以上余量。
- 卡片第 8 项自己判断「补标用 `claude -p` 走订阅额度」不算花钱，同时写明「如果 router 认为这也算，就停下来问」——符合用户 11-02 的裁决。

## 原始输出

- 本地：`/private/tmp/claude-501/-Users-apple-works-viber-code-aifriends/c8bdcbb0-863d-4071-95b3-f1638c1f4e05/scratchpad/spike/`（s1.json、s2.json、s3.log、s3b.log、s4-*.json、s5.json）。scratchpad 是临时目录，关键片段已摘进上表。
