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
---

## 自定义 fastship E2E runner 的两处 schema/信号坑（forge dashboard 增强时踩到）

- **E2E 报告 Step 3.3 数 turns 用嵌套 schema**：orchestrator `validate_e2e_report` 统计 `scenarios[].rounds[].turns`，**不是**顶层 flat `turns`。自定义 runner 若只产出 flat `{turns, passed}`（给项目自带 gate 用），3.3 会报 `e2e_result.json turns 不足 (0 < N)`。解法：runner 同时产出嵌套镜像 `"scenarios":[{"rounds":[{"turns": turns}]}]`，flat 键保留给项目 gate，两个消费方都满足。
- **e2e_gate_passed 只认 exit code**：ship_verify_gate `gate_post_bash` 对 e2e-gate 命令调 `extract_exit_code(tool_response.exitCode/exit_code/...)`，==0 才置 `e2e_gate_passed=true`；只输出文本 "GATE PASS" 不够（它找的是 exit code，文本里找 "GATE PASSED"）。CLI 模式手动喂 post_bash 时 `tool_response` 必须带 `exitCode:0`。
- **zsh 不对未加引号的 `$CMD` 做分词**：`CMD="python3 x.py -o out"; $CMD >log` 在 zsh 里会把整串当一个命令名（"no such file or directory"），导致 e2e "跑了" 实则用了上一轮 stale 结果文件、hash 不变而不自知。直接写出命令或用 `${=CMD}`/`eval`。

---

## 在 claude-skills 自跑 fastship 喂 Phase-3 gate 的三个污染坑（forge 引擎上搬时踩到）

- **跑 pytest 必须清掉 `FASTSHIP_*` env，否则双向污染**：`tests/fastship/conftest.py` 的 autouse fixture 只 `delenv FASTSHIP_SESSION`，**不删** `FASTSHIP_REPO_ROOT`/`FASTSHIP_STATE_HOME`。若在导出了这两者的 shell 里跑 `pytest`：(a) 测试被污染（repo_root 解析到你的 worktree 而非 tmp_path，出现莫名 fail）；(b) 测试**反写**你的 `FASTSHIP_STATE_HOME`，在里面建出 `f2`/`test-feature` 等假 session、把 registry `current_session` 指歪 → orchestrator 报「没有活跃 session」。解法：跑项目测试用 `env -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME -u FASTSHIP_SESSION bash -c "pytest ..."`；喂 gate 与跑测试**分两次 Bash**，别同 shell。
- **为隔离 /tmp 争用而加的 `.claude/fastship.project.json` 会撑坏 e2e 测试**：claude-skills 无项目 e2e 配置时 `e2e_result_path()` 默认 `/tmp/e2e_result.json`（与并行 fastship job 争用）。给唯一 result_path 要写 `.claude/fastship.project.json`——但 `test_orchestrator.py::TestE2EReportHardened` 会**读到这份真配置**导致 3 个测试 fail。解法：该配置**只在 3.2–3.4 喂 gate 期间临时存在**，跑 pytest（3.1/回归）前必须删掉；且**别提交**（untracked，不进 PR）。
- **stray session → hook fail-open 不记 artifact**：state home 里有 ≥2 个活跃 session 且未 `FASTSHIP_SESSION` 锁定时，orchestrator `post_edit`/`post_bash` 会打印「检测到多个活跃 session…本次 hook 不应用 session 专属逻辑」并**跳过记录**（report_path 记不上、3.3 卡）。解法：`rm -rf` 掉污染 session 目录只留自己的，或喂 hook 时 `export FASTSHIP_SESSION=<sid>` 显式锁定。
- **报告/test/e2e 的 artifact 记录走对入口**：3.3 报告路径由 **orchestrator** `post_edit`（非 ship_verify_gate 的 post_edit）在 `current_step==3.3 且文件名含 e2e/report/质量` 时记 `report_path`；3.1 test_passed / 3.2 e2e_executed / 3.4 e2e_gate_passed 由 **ship_verify_gate** `post_bash`（喂真实 command+stdout，e2e-gate 带 `exitCode:0`）置位。两个 post_edit 入口别搞混。
