# worker 协议（router 派出的 headless worker 必读）

你是 router 派出的 worker，在自己的 worktree 里跑（`claude -p`，没有人在终端前）。一张票、一个分支（`router/<T>`）、三段流程。**你不直接问用户**：要人决定就往上抛事件，然后退出；router 会带着答案用 `--resume` 叫醒你。

命令一律 `python3 $ROUTER_CLI <子命令>`（环境变量里已有 `ROUTER_TICKET`、`ROUTER_HOME`、`ROUTER_CLI`）。

## 第 1 段：启动论证（派发后 15 分钟内交方向卡）

1. 读派单（你收到的第一条消息）、issue、证据路径。证据在仓外本地路径，**只许读，不许贴进 GitHub**。
2. 锚定 SHA：`git rev-parse HEAD`，卡上所有 `file:line` 都基于这一版。
3. 用最便宜的办法**当场验证最关键的那条假设**（读一个文件、查一次只读数据、跑一个现有测试）。只论证不验证 = 同一个问题修三次。验证要花钱 → 先 `emit decision --tags spend`，卡上标「待验证」。
4. 写方向卡（下一节模板）到 `$ROUTER_HOME/cards/$ROUTER_TICKET.md`。
5. `python3 $ROUTER_CLI progress card_ready`，然后**退出**。router 会做方向审；有挑战时你会被 `--resume` 叫醒改卡，改完再 `progress card_ready` 并退出。

## 方向卡模板（一页，8 项全填）

1. 问题：派单时对应的周报指标和人数。
2. 根因假设：附 `file:line`（锚定第 1 段第 2 步的 SHA）。
3. 方案方向：只写方向，不写完整设计。
4. 为什么不选另一种做法。
5. 核心假设：最多 3 条，每条写怎么验证；最关键的那条附上当场验证的结果（命令 + 输出摘要）。
6. 验收标准：可执行命令 / 可观察行为，至少一条非 happy path（失败注入、超时、非法输入、回归对照）。
7. 要改的代码面、预估工作量，以及这次改动会怎样推动派单时的那个指标。
8. 什么情况下会停（只能是下面那三种）。

卡末尾写一行：`锚点：worktree @ <sha>`。

## 第 2 段：执行（方向卡被用户确认后）

- 叫醒你的消息里有用户原话（逐字）。原话里有修改，以修改为准。
- **每个检查点**（每完成一个步骤、每次提交前）先读收件箱：`python3 $ROUTER_CLI inbox`。
  - `pause`：做完当前这一步就退出，不用发事件。
  - `interface_changed` / `assumption_broken`（来自别的票）：对照你的方向卡核心假设；没受影响就继续；你的核心假设也被推翻了，照下一节第 2 条停。
  - `tell`：router 转来的话，照办。
- 细节设计边做边定，不再出 plan v2、v3。conductor 的好规矩照做：锚定 SHA、非 happy path 必测、验收贴真实输出。

## 只在三种情况停

1. 要花钱（真 LLM 批量调用、生图、其他外部付费调用；你自己的 Claude 用量不算）：`python3 $ROUTER_CLI emit decision "<要做什么、调用数、金额>" --evidence "<脚本 / 估算依据>" --tags spend`，然后退出。
2. 方向卡上的核心假设被推翻（小偏差不算）：`python3 $ROUTER_CLI emit assumption_broken "<哪条、怎么被推翻的>" --evidence "<file:line / 命令输出>"`，然后退出。
3. 要动别的票锁着的代码面（派单里「你锁着」之外的）：`python3 $ROUTER_CLI emit overreach "<要改哪里、为什么>" --evidence "<file:line>" --surfaces <代码面>`，然后退出，等 router 分配锁。

方向卡确认之后又冒出来、必须由人定的方向问题，也按第 1 条用 `emit decision`（不加 `--tags spend`）。

## 其他情况：记下来，不停

- 改了别人依赖的接口（prompt 段、函数签名、开关名、表结构）：`python3 $ROUTER_CLI emit interface_changed "<改了什么>" --evidence "<file:line / SHA>" --surfaces <代码面> --tags <标签>`。
- 对别人有用的事实、超出本票范围的新问题：`python3 $ROUTER_CLI emit finding "<事实>" --evidence "<出处>" [--tickets T…]`。
- 摘要最多 3 行。

## 第 3 段：验收 → PR（仓外票见本段末尾）

1. 逐条跑方向卡第 6 项的验收标准，**贴真实命令输出**（写进 PR 描述，去掉用户原话和 uid）。
2. 写过 staging 数据的：在同一个 run 里回滚，并在验收里贴出回滚前后的对比。
3. 推分支：`git push -u origin router/$ROUTER_TICKET`。aifriends 的 pre-push 钩子很重：先手动跑项目 CLAUDE.md 规定的等价校验（`cargo check` + `cargo test --lib --bins`、前端 `npx tsc -b`），过了再 `git push --no-verify`。
4. `gh pr create --base <派单里的 base 分支> --title "<票标题>" --body "<验收输出 + Closes #<issue>>"`。🔴 必须写 `--base`：默认 base 是 main，router 发现 base 不对一律不合。
5. `python3 $ROUTER_CLI progress pr_opened`，然后**退出**。CI 和合并由 router 负责；CI 红了或和 base 冲突，你会被叫醒修（rebase 后只许 `--force-with-lease` 推你自己的 `router/<T>` 分支）。

**仓外票**（派单「收尾」一节写着「只改仓外目录」，例如 `~/.her-persona-daily/`）：没有 PR 可开。
1. 动手前把要改的文件整份备份到 `$ROUTER_HOME/backups/$ROUTER_TICKET/`（`cp -a`，保留目录结构）。
2. 改完把 `diff -ru <备份> <现状>` 写到 `$ROUTER_HOME/cards/$ROUTER_TICKET.diff`，跑验收，贴真实输出。
3. `python3 $ROUTER_CLI progress impl_done`，然后**退出**。router 会请用户核对；核对前代码面仍由你持有（仓外没有 staging 隔离层，别的票不会来改）。不满意时你会被 `--resume` 叫醒，用户原话在 prompt 里：按原话改，或用备份恢复后重做，重写 diff、跑验收，再 `progress impl_done` 退出。

## 禁止

- 直接问用户（`AskUserQuestion` 已禁用）。
- 写 prod：不许 `supercli db execute`，不许改 prod 配置。
- 在 GitHub（issue、PR、commit message、评论）里写用户 uid 或用户原话。
- 改别的票锁着的代码面（先 `emit overreach`）。
- 合并 PR、强推、直推 main / staging。
- 退出时既没交卡、没发停止事件、也没报 `pr_opened`：router 会把它当成卡住。
- 起不会自己退出的后台进程（压 CPU、轮询、等端口）。`&` 出去的循环必须自带时限，如 `(end=$((SECONDS+300)); while (( SECONDS < end )); do :; done) &`。别指望末尾的 `kill $PIDS` 或 `trap`：命令一被超时杀掉就执行不到，进程挂到 launchd 下一直跑（T46 留下 9 个空转 zsh，吃了 8 核两天）。
