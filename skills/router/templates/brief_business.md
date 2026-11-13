## 业务票（这张票是业务票：上面「上线验收」一节不适用）
用户判真时已经把整张票授权给 router（票级授权）：方向卡由 router 审完代为确认，合 staging 后不走查，router 自己上 prod。细节见 $worker_md「业务票」一节。
- 一张票就是一个问题：上面这群人想要的这件东西。任务是你的清单项，不另开票；超出这件东西 = 越界，emit finding。
- 上面「router 线索」= 墙（重新派来的写着没拆的墙和已试过的方向，已试的标了判定，不是线索，别在原路上再试）。
- 一次尝试 = 一个方向，对准第一道没拆的墙；可以改好几处，但必须带开关。方向卡第 9 项写：开关名和撤回办法（router 上 prod 时记成 `released --rollback`）、第二天怎么数覆盖、墙还在不在怎么看。不写 T+1/T+3/T+7。
- 上线前先做离线回放：从真实对话里撞墙的那一句开始，用新版本重放，盲判墙还在不在；要花钱就先停下来报价（emit decision --tags spend）。
- 上线后等判定再做下一个方向：第二天分析 agent 判（覆盖 → 读对话 → 判定）。下一个方向由调度器派一个新 worker 来做，判定和下一步在收件箱里。每次开工先读 inbox 和尝试记录（status --json 里这张票的 attempts / walls）。
- PR 正文写 `Refs #<issue>`，不写 `Closes #`：业务票是长期票，`Closes` 会在第一次发版时把 issue 自动关掉。
- 停点：票级授权不包括花钱（--tags spend）、撤不回的改动（--tags irreversible）、发应用商店包（--tags store_release），照下面红线 emit decision 后退出。票内的方向问题 emit decision（不带这三个标签）由 router 定。
