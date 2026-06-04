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

## Session Radar（扫 ~/.claude 做 session 维度可视化）+ dynamic workflow 执行的坑

数据源与启发式：

- **`~/.claude` 数据形态**：前台 session = `projects/<encoded>/<uuid>.jsonl`（每行 JSON：`type`/`cwd`/`gitBranch`/`message.{role,content}`；`content` 可为 str 命令壳或 block list）；后台任务 = `jobs/<8字符daemonShort>/state.json`（权威 `state`=active/blocked/done、`intent`=人类原始意图、`cwd`、`linkScanPath` 回链 transcript）。**部分 job 无 state.json**（旧/清理过）→ 每次读必 try/except。子代理在 `<uuid>/subagents/agent-*.jsonl`（4 层深），用 `projects/*/*.jsonl`（2 层）glob 天然排除。
- **后台 liveness 必须只按 job state、绝不靠 mtime**：bg job 两轮间静默是常态，纯 mtime 会把"活着但在想"误判 dormant；更糟的是**无 state.json 的 bg job 若 age 兜底成 0 会漏进 mtime 桶被误判 active**。正解：bg 只按 `state` 分类（active→working / blocked / done），**缺失/未知 state → `unknown`**，bg 分支绝不 fall through 到 mtime 桶。前台没有权威态才用 mtime。
- **命令壳污染 opening**：`/fastship`、`/forge` 起的 session 首条 user 消息是 `<command-message>…<command-args>真实意图</command-args>` 或 `<local-command-caveat>…`。取真实人类意图要剥壳：优先抽 `<command-args>` 内文，否则删所有 `<command-*>`/`<local-command-caveat>` 块；纯文本 prompt（无壳标签）原样放行别误伤尖括号。bg job 直接用 `state.json.intent` 当 opening。
- **漂移启发式要 CJK-aware**：`compute_drift` 若只 `[a-z0-9]+` 分词，**中文 opening（很常见，本仓库用户就用中文）token 全空 → drift 永不触发**（静默失效，比误报更坏）。token 正则要带汉字区间 `[一-鿿㐀-䶿]`（单字成 token）。drift boolean 只是提示，opening/now 并排列才是真信号。
- **"所有 session" 别被默认 window 静默降级**：前台用 recency window 裁噪可以，但必须是**可 opt-out 的 lens**（`--window-min 0` 显示全部），且后台任务无条件全显（含 done/blocked/stateless）。否则 SEED 的"所有 session"被偷偷缩水。
- **复用 forge_dashboard.py 的 stdlib web 壳**：`ThreadingHTTPServer` + `/api/state` JSON + 内联 HTML client-render 形态可整体借；但 forge 无端口轮询（端口占用直接抛），session radar 自己加 try-bind 递增。

dynamic workflow 执行 confirmed plan（Phase 2）：

- **依赖感知拆分按"文件不相交"**：plan 若把一个模块拆成 Task 1-7 但都改**同一个文件**，那是**一条串行链**不能并行；真正可并行的是**不同文件组**（module / tests / e2e-runner+config / skill-doc）。≥2 个不相交文件组才值得 `Workflow parallel()`。
- **并行 implement agent 只许 edit + 语法检查（ast.parse / 真 import），禁止跑 pytest/E2E**：那是主线程 Step 3.1/3.2 的事，并行跑会撞 gate 状态写、语义也错（要全部完成后跑全量）。agent 不各自 commit，主线程逐组统一 commit。
- **plan 代码"逐字符转写"最可靠**：plan 已是 codex-reviewed 的精确代码，让 agent **Read plan → 逐字符转写**（含 CJK、regex 字面量、注释）比把 360 行重嵌进 prompt 字符串靠谱（CJK/引号/反斜杠转义易错）。implement→review pipeline 的 review agent 逐行 diff plan 兜底，主线程 pytest 是硬门。
- **Workflow 脚本是纯 JS，模板串里别放裸反引号**：prompt 文本里写 `` `if __name__` `` 这种反引号会提前终止模板字面量→parse error。用普通引号或转义。

工作单元 LLM 摘要轮（把"当前活动"列从最后一条动作重定义为提炼摘要）新增：

- **"看最后一条消息 ≠ 提炼"（产品红线）**：session-radar 的"当前活动"列若只显示某 session 的 assistant 最新一条消息/最新工具动作，和直接打开 session 没区别、零提炼价值。真正的价值是**跨多事件归纳出那一件具体事情**（按 feature/bugfix/重构 维度）的总结。用户明确要求时优先做归纳，宁可放弃 5s 自动刷新（改手动 `🔄 刷新`）换更重的提炼。
- **真实 `~/.claude` 的 todo 工具是 TaskCreate/TaskUpdate，不是 TodoWrite**：实测最近 40 个 transcript `TodoWrite` 出现 **0 次**，全是 `TaskCreate`(`{subject,activeForm}`，无 id) + `TaskUpdate`(`{taskId,status}`，`taskId`=**创建顺序**字符串 "1"/"2"/…)。「此刻在做 X」要**事件溯源**重建：TaskCreate 按序定义 task，TaskUpdate 迁移状态，取 in_progress 项的 activeForm。别假设 TodoWrite 快照（兼容它即可，但别只认它）。混合 transcript 里旧 TodoWrite 快照可能盖掉更晚的 Task 生命周期 → 按事件位置取**最近**的 task 源。
- **同文件 TDD plan 仍可并行：按 impl-文件 vs test-文件 切缝**（对 #「同一文件=串行链」的精确化补充）：writing-plans 的 plan 常把 Task1-4 的「写测试 / 跑测试 / 实现」交错塞进**同两个文件**（module + test）。只要 plan 给了两端的**精确代码**（codex-reviewed），就能沿**文件边界**拆成 3 个不相交组并行：agentA 写 module 全部函数、agentB 写 test 全部用例、agentC 写 e2e-runner —— 三者文件不相交、确定性匹配，主线程 `pytest` 是收敛硬门（impl/test 由同一 spec 派生，必然对齐）。比真按 Task 串行省一大截。
- **importlib.spec_from_file_location 加载的模块会吃 stale `.pyc`**：动态加载被测 module 时，implement→review 之间若不清字节码，review 可能 import 到**改前**的编译缓存 → 伪失败（80 测试里偶发 1 个）。implement/review 周期之间 `pytest -B` 或清 `tests/.../__pycache__` + `skills/.../__pycache__`。
- **stdlib-only 工具里接 LLM（当 SEED「无 LLM」被用户推翻）**：shell out 本地 `claude -p --model claude-haiku-4-5`（prompt 走 stdin，复用订阅鉴权、无需 API key，冷启~15s）。务必：① **可注入**（`make_*_llm()` 返回 callable，测试/E2E 传 stub → 确定性，真 CLI 路径不进 CI）；② **按信号指纹缓存**（type|title|doing|detail 变了才重算）；③ **绝不阻塞 `/api/state`**（未命中丢后台 `ThreadPoolExecutor`，`threading.Lock` + 原子 `_claim_pending` 防 `ThreadingHTTPServer` 下重复 job/丢更新，手动刷新时渐显）；④ **启发式兜底**（LLM 缺失/超时/非零退出 → `source="heuristic"`）；⑤ **`build_snapshot` 默认 `use_llm=False`** 让既有全套测试/E2E 保持确定性；⑥ **drift 喂启发式 title+doing 不喂 LLM 文案**，否则随机措辞污染确定性 drift。
- **把函数存成类属性会被绑成方法（吞掉整功能的隐形 bug）**：`_Handler.llm = some_func` 后，`self.llm` 经描述符协议变成**绑定方法**、自动塞入 `self` 作首参 → 调用 `self.llm(prompt)` 实为 `some_func(self, prompt)` → `TypeError: takes 1 positional argument but 2 were given`，被 `_bg_refine` 的 `except` 静默吞掉 → 永远回退启发式。表象是"web UI 永不出 LLM"，真因极隐蔽。**修法 `_Handler.llm = staticmethod(func)`**。教训：① 任何把**可调用对象**赋给类属性、再经实例访问的地方都要 `staticmethod`；② 别用 `except: pass` 吞后台 worker 异常——加一行日志能省几小时（本 bug 直到无条件日志才一锤定音）；③ 写**走实例访问路径**的回归测试，光测 `func(x)` 直调测不出绑定。
- **本机 `claude -p` 调用的运维坑（排查了很久才定位，多为干扰项）**：① CLI 会 re-exec 成带版本号的 node 子进程，`subprocess` 超时只杀 wrapper、漏掉子进程 → 僵尸堆积抢 CPU、后续全超时 → 用 `start_new_session=True` + 超时 `os.killpg(SIGKILL)`；② 在**仓库 cwd** 跑会加载整个项目上下文（CLAUDE.md/文件）→ 慢 ~30% 还跑偏去聊项目 → 用**中性 cwd**（tempdir）；③ `pkill -f claude-haiku-4-5` 会**误杀服务本身**（服务命令行也含 `--llm-model claude-haiku-4-5`）→ 只匹配 `-p --model` 杀 helper；④ 雷达用 LLM 扫自己所在的 `~/.claude` 会形成**反馈环**（每次 `claude -p` 写一个新 session，雷达又去摘要它）→ 按 prompt 签名排除自己的 helper session。
- **生成式摘要会臆造方向/主被动**：把「对齐 A 至 B」反转成「对齐 B 至 A」之类。prompt 要硬性要求**忠实保留原文方向与主被动关系、严禁反转/臆造**，并保留 opening 原文在 UI 上供核验。LLM 摘要换来凝练，但**方向/事实细节非 100% 可靠**，是该取舍的固有代价。
