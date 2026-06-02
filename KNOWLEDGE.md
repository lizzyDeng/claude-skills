# claude-skills — 开发经验知识库

> 记录 skill / hook 维护中的关键经验教训，供后续开发参考。

---

## Fastship Ship Gate 与项目本地 Hook 漂移

### 关键认知

- **先查 source-of-truth，再修项目本地副本**：项目 `.claude/hooks/ship_verify_gate.py` 可能是旧版或简化版，不能据此判断 `claude-skills` 源头缺能力。本次 aifriends 本地 hook 缺 `post_edit` knowledge 置位，但 `/Users/apple/works/claude-skills/skills/fastship/hooks/ship_verify_gate.py` 已经有完整 `post_edit`、`knowledge_skip`、`knowledge_recall`。
- **安装/复制脚本会覆盖本地修复**：如果只修项目本地 `.claude/hooks/ship_verify_gate.py`，后续按 INSTALL 或 settings 重新从 `claude-skills` 复制 hook 时可能丢失补丁。维护 hook 行为时要明确改的是源头、项目副本，还是两者都需要。
- **Forge 依赖 fastship state 字段兼容性**：Forge Gate 4 接受 `test_passed` 或 `cargo_test_passed`，但必须有 `e2e_executed` 和 `knowledge_acknowledged` / `knowledge_skipped` / `knowledge_done` 之一。改 fastship state schema 时要同步验证 Forge gate。
- **文档存在不等于项目安装生效**：`fastship/SKILL.md` 和 `INSTALL.md` 可以描述完整流程，但项目本地 hook 仍可能是旧代码。遇到 gate blocker 时要同时检查文档、源 hook、项目 hook、`.ship-verify-state.json` 四处。

---

## Forge Worktree 清理 + 在 claude-skills 自身上跑 fastship 的坑

### 关键认知

- **`FASTSHIP_SESSION` 环境变量会污染 pytest**：跑 forge/fastship 测试套件时若 shell 里 `export FASTSHIP_SESSION=...`，`test_requires_feature_session_when_current_state_missing` 这类「无当前 session」隔离测试会失败（守卫测试正确地抓到了泄漏）。教训：**跑项目测试用干净环境**，`FASTSHIP_SESSION` 只在「驱动 gate（post_bash/loop_record/done）」时设置，不要全局 export。
- **驱动 ship_verify_gate（CLI/无 hook 模式）**：在非 claude-skills session 里跑（hook 不自动触发）时，手动 `python3 skills/fastship/hooks/ship_verify_gate.py post_bash` 并喂真实 `{tool_input.command, tool_response.stdout, exitCode}`，即可置位 test_passed / e2e_executed / e2e_result_hash / e2e_gate_passed。命令+输出必须真实（validator 会独立 re-hash result、re-run e2e_gate.py）。
- **纯 Python skill 的 E2E**：无 HTTP 服务时，写一个真 git「runner」（命名匹配 `e2e[_-]?runner` 才会触发 result hash），跑真实代码、把每一步真实结果写进 e2e_result.json（scenarios→rounds→turns，≥10 turns），既是诚实证据又能过 e2e_gate.py，**无需 fastship.project.json**。
- **`zsh` 不对未引号变量做分词**：`CMD="python3 a b"; $CMD` 会把整串当一个命令名（exit 127）。驱动 gate 的命令要直接写出，别塞进变量。
- **Worktree 清理安全契约**：只删「干净 + 分支已真合并进 trunk（`git merge-base --is-ancestor`，squash-merge 保守保留）」的 managed worktree。managed scope 锚定在**主 worktree**（`git worktree list` 第一项），否则从 linked worktree 跑会误判 scope 清不掉 sibling。绝不删脏/未合并/当前/主/外部 worktree。已提交代码由 git 自身兜底：`worktree remove` 不带 `--force`（拒删脏）+ `branch -d`（拒删未合并）。
