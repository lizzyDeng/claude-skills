# worker 协议（router 派出的 headless worker 必读）

你是 router 派出的 worker，在自己的 worktree 里跑（`claude -p`，没有人在终端前）。一张票、一个分支（`router/<T>`）、三段流程。业务票（派单「问题」写的是「人群：想要什么」）另外照本文末尾「业务票」一节。**你不直接问用户**：要人决定就往上抛事件，然后退出；router 会带着答案用 `--resume` 叫醒你。

命令一律 `python3 $ROUTER_CLI <子命令>`（环境变量里已有 `ROUTER_TICKET`、`ROUTER_HOME`、`ROUTER_CLI`）。

## 第 1 段：启动论证（派发后 15 分钟内交方向卡）

1. 读派单（你收到的第一条消息）、issue、证据路径。证据在仓外本地路径，**只许读，不许贴进 GitHub**。
2. 锚定 SHA：`git rev-parse HEAD`，卡上所有 `file:line` 都基于这一版。
3. 用最便宜的办法**当场验证最关键的那条假设**（读一个文件、查一次只读数据、跑一个现有测试）。只论证不验证 = 同一个问题修三次。验证要花钱 → 先 `emit decision --tags spend`，卡上标「待验证」。
4. 写方向卡（下一节模板）到 `$ROUTER_HOME/cards/$ROUTER_TICKET.md`。
5. `python3 $ROUTER_CLI progress card_ready`，然后**退出**。router 会做方向审；有挑战时你会被 `--resume` 叫醒改卡，改完再 `progress card_ready` 并退出。

## 方向卡模板（一页，9 项全填）

1. 问题：派单时对应的周报指标和人数。
2. 根因假设：附 `file:line`（锚定第 1 段第 2 步的 SHA）。
3. 方案方向：只写方向，不写完整设计。
4. 为什么不选另一种做法。
5. 核心假设：最多 3 条，每条写怎么验证；最关键的那条附上当场验证的结果（命令 + 输出摘要）。
6. 验收标准：可执行命令 / 可观察行为，至少一条非 happy path（失败注入、超时、非法输入、回归对照）。
7. 要改的代码面、预估工作量，以及这次改动会怎样推动派单时的那个指标。
8. 什么情况下会停（只能是下面那三种）。
9. 上线验收：票的终点是上线验收，不是 PR 合并、也不是上 prod。上线后第 1、3、7 天 router 照这一节查 prod（只读）。只证明 bug 不再出现不够：要论证这次改动对线上行为有正收益。
   - 先标类型：**增长型**（必须拿出行为收益才算生效；机制生效但行为 flat，算没用）或 **风险型**（行为不 worse 即可，同时写明代价：慢了多少、少了什么）。
   - ① 入口：真实用户从哪里走到这次改动，哪些客户端 / 版本吃得到，覆盖率多少（写分子分母）；附 T+1 命中查询（prod 只读），要能数出真实用户确实走到了新路径。
   - ② 机制：改动本身起作用没有——新行为出现了、旧问题不再出现；写用什么查询或日志看。如果改动根本没生效，这条查询要能看出来。
   - ③ 行为收益（必答，主体）：触点行为——改动作用到的回合或用户，他紧接着的反应（接着发的比例、下一条长度、后续轮数、离开率），对比上线前同类触点；下游留存类指标，样本小就标 small。
   - 另写护栏和判定：护栏指标按项目周报的判定标准（项目专属的取数方法和口径在 config `postlaunch_brief` 指向的文件里，派单「上线验收」一节给了路径）；没入口 / 没用 / 有害各自当天做什么。

卡末尾写一行：`锚点：worktree @ <sha>`。

## 第 2 段：执行（方向卡被确认后）

- 叫醒你的消息里有用户原话（逐字）；业务票的方向卡由 router 审完代为确认，消息里是「router 的说明」。原话里有修改，以修改为准。
- **每个检查点**（每完成一个步骤、每次提交前）先读收件箱：`python3 $ROUTER_CLI inbox`。
  - `pause`：做完当前这一步就退出，不用发事件。
  - `interface_changed` / `assumption_broken`（来自别的票）：对照你的方向卡核心假设；没受影响就继续；你的核心假设也被推翻了，照下一节第 2 条停。
  - `tell`：router 转来的话，照办。
- 细节设计边做边定，不再出 plan v2、v3。conductor 的好规矩照做：锚定 SHA、非 happy path 必测、验收贴真实输出。

## 只在三种情况停

1. 要用户批的事（业务票的票级授权也不包括这三类）：要花钱（真 LLM 批量调用、生图、其他外部付费调用；你自己的 Claude 用量不算）⇒ `python3 $ROUTER_CLI emit decision "<要做什么、调用数、金额>" --evidence "<脚本 / 估算依据>" --tags spend`；撤不回的改动（删数据、不可逆迁移）⇒ 同样 `emit decision`，`--tags irreversible`；发应用商店包 ⇒ `--tags store_release`。发完就退出。
2. 方向卡上的核心假设被推翻（小偏差不算）：`python3 $ROUTER_CLI emit assumption_broken "<哪条、怎么被推翻的>" --evidence "<file:line / 命令输出>"`，然后退出。
3. 要动别的票锁着的代码面（派单里「你锁着」之外的）：`python3 $ROUTER_CLI emit overreach "<要改哪里、为什么>" --evidence "<file:line>" --surfaces <代码面>`，然后退出，等 router 分配锁。

方向卡确认之后又冒出来、必须由人定的方向问题，也按第 1 条用 `emit decision`（不加这三个标签；业务票的这类问题由 router 定）。

## 其他情况：记下来，不停

- 改了别人依赖的接口（prompt 段、函数签名、开关名、表结构）：`python3 $ROUTER_CLI emit interface_changed "<改了什么>" --evidence "<file:line / SHA>" --surfaces <代码面> --tags <标签>`。
- 对别人有用的事实、超出本票范围的新问题：`python3 $ROUTER_CLI emit finding "<事实>" --evidence "<出处>" [--tickets T…]`。
- 摘要最多 3 行。

## 第 3 段：验收 → PR（仓外票、零代码票见本段末尾）

1. 逐条跑方向卡第 6 项的验收标准，**贴真实命令输出**（写进 PR 描述，去掉用户原话和 uid）。
2. 写过 staging 数据的：在同一个 run 里回滚，并在验收里贴出回滚前后的对比。
3. 推分支：`git push -u origin router/$ROUTER_TICKET`。aifriends 的 pre-push 钩子很重：先手动跑项目 CLAUDE.md 规定的等价校验（`cargo check` + `cargo test --lib --bins`、前端 `npx tsc -b`），过了再 `git push --no-verify`。
4. `gh pr create --base <派单里的 base 分支> --title "<票标题>" --body "<验收输出 + Closes #<issue>>"`。🔴 必须写 `--base`：默认 base 是 main，router 发现 base 不对一律不合。
5. `python3 $ROUTER_CLI progress pr_opened`，然后**退出**。CI 和合并由 router 负责；CI 红了或和 base 冲突，你会被叫醒修（rebase 后只许 `--force-with-lease` 推你自己的 `router/<T>` 分支）。

**仓外票**（派单「收尾」一节写着「只改仓外目录」，例如 `~/.her-persona-daily/`）：没有 PR 可开。
1. 动手前把要改的文件整份备份到 `$ROUTER_HOME/backups/$ROUTER_TICKET/`（`cp -a`，保留目录结构）。
2. 改完把 `diff -ru <备份> <现状>` 写到 `$ROUTER_HOME/cards/$ROUTER_TICKET.diff`，跑验收，贴真实输出。
3. `python3 $ROUTER_CLI progress impl_done`，然后**退出**。router 会请用户核对；核对前代码面仍由你持有（仓外没有 staging 隔离层，别的票不会来改）。不满意时你会被 `--resume` 叫醒，用户原话在 prompt 里：按原话改，或用备份恢复后重做，重写 diff、跑验收，再 `progress impl_done` 退出。

**零代码票**（仓内票，方向卡确认的方案只改配置、不改仓库代码，例如改价格、改开关）：没有 PR 可开。
1. 把要执行的配置改动写到 `$ROUTER_HOME/cards/$ROUTER_TICKET.config.md`：改哪个键、改前值、改后值、执行命令或后台步骤、回滚办法。
2. 能在 staging 验的先在 staging 验（写过就在同一个 run 里回滚），贴真实输出。**不许写 prod**：prod 由 router 在用户放行上 prod 后执行。
3. `python3 $ROUTER_CLI progress impl_done`，然后**退出**。票进「等上 prod」，router 执行配置后用 `released --t0 <生效时刻>` 开始上线验收（方向卡第 9 项）。
   router 收到 impl_done 当场删掉你的 worktree 和本地分支，**所以退出前所有要留的东西都写在 `$ROUTER_HOME/cards/` 下**（配置、读数 SQL、分析脚本），worktree 里不留未提交的改动（有未提交改动的 worktree 不会被删，会报给人处理）。

## 业务票（派单「问题」写的是「人群：想要什么」、「线索」写的是墙；老票和维护票跳过本节）

用户判真时已经把整张票授权给 router（票级授权）：你的方向卡由 router 审完代为确认，PR 合进 staging 后不走查，router 自己上 prod。

- **一张票就是一个问题**：一群人想要的一件东西，卡在派单列出的几道墙上。任务是你的清单项：每个任务不另开票、不另开 worktree、不另出方向卡（方向卡按尝试出：一次尝试一张）。超出这群人想要的这件东西 = 越界：`emit finding` 报出来，不顺手做。
- **一次尝试 = 一个方向**，对准第一道还没拆的墙。可以同时改好几处代码，但**必须带开关**：关掉开关就回到这次尝试之前的行为。方向卡第 3 项写方向、对准哪道墙；第 9 项对业务票改写成三件事：开关名和撤回办法（router 上 prod 时原样记成 `released --rollback`）、第二天怎么数覆盖（前端改动看哪个版本号、服务端改动在每轮快照里打什么新标记）、墙还在不在怎么看。不写 T+1/T+3/T+7。
- **上线前先做离线回放**（v2 spec §5.2）：从真实对话里撞墙的那一句开始，用新版本重放，盲判墙还在不在。结果（重放几段、墙还在几段）写进 PR 描述，不贴原话和 uid。回放要花钱（真 LLM 调用）：先停下来报价，`python3 $ROUTER_CLI emit decision "<调用数、金额>" --evidence "<估算依据>" --tags spend`，然后退出，等放行。
- **上线后等判定再做下一个方向**：报完 `pr_opened` 就退出，不用等。router 上 prod 后，第二天分析 agent 判这一次尝试（覆盖 → 读对话 → 判定）。判定出来 router 会叫醒你，或者派一个新 worker 接着做：收件箱里有判定和下一步。「下一堵墙：…」= 去拆下一道墙；「换方向」= 别在原路上再试。每次开工（包括被重新派来）先读 `python3 $ROUTER_CLI inbox`，再看尝试记录（`python3 $ROUTER_CLI status --json` 里这张票的 `attempts` 和 `walls`，只读），然后从第 1 段写新方向卡。
- **零代码的尝试**（只改配置）：照第 3 段「零代码票」写 `config.md`，里面的回滚办法就是这次尝试的撤回办法；router 自己执行、上 prod，不等用户放行。
- **停点**：票级授权不包括花钱（`spend`）、撤不回的改动（`irreversible`）、发应用商店包（`store_release`）：照「只在三种情况停」第 1 条停。票内的方向问题不用找用户：`emit decision`（不带这三个标签）由 router 定。

## 禁止

- 直接问用户（`AskUserQuestion` 已禁用）。
- 写 prod：不许 `supercli db execute`，不许改 prod 配置。
- 在 GitHub（issue、PR、commit message、评论）里写用户 uid 或用户原话。
- 改别的票锁着的代码面（先 `emit overreach`）。
- 合并 PR、强推、直推 main / staging。
- 退出时既没交卡、没发停止事件、也没报 `pr_opened` / `impl_done`：router 会把它当成卡住。
- 起不会自己退出的后台进程（压 CPU、轮询、等端口）。`&` 出去的循环必须自带时限，如 `(end=$((SECONDS+300)); while (( SECONDS < end )); do :; done) &`。别指望末尾的 `kill $PIDS` 或 `trap`：命令一被超时杀掉就执行不到，进程挂到 launchd 下一直跑（T46 留下 9 个空转 zsh，吃了 8 核两天）。
