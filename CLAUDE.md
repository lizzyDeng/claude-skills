# Claude Skills Collection

本项目是 Claude Code 自定义 skills（slash commands）的集合。

## 使用方式

在此目录下运行 Claude Code，即可通过 `/skill-name` 调用对应 skill。

## Skills 列表

- `/anthropic-news` — 检查 Anthropic 官方博客是否有新文章，并生成中文摘要
- `/fastship` — 结果驱动开发：Brainstorm → 自主执行 → E2E 验证循环（含 hooks Gate 脚本）
- `/fastship-setup` — 在当前项目一键安装 /fastship 所需的 hooks、E2E 工具链和 skill 定义
- `price-alert` — 监控黄金 (XAU/USD) 和 BTC 价格，大幅波动时通过 Telegram 通知
- `hl-trader` — Hyperliquid 交易工具（限价单、查询持仓、Telegram 通知）
- `/db-agent` — 用自然语言查询任意数据库：Postgres/SQLite → 只读快照 + datasette-agent Web/CLI 问答
- `/project-viewer` — 启动本地 Web 服务浏览项目目录结构和渲染 Markdown 文件
- `/viewer-setup` — 将 /project-viewer 安装为全局 skill，任何项目可用
- `/chunbing` — 安装春饼 statusline，让春饼常驻终端底部陪你写代码
- `/deeplearn` — 苏格拉底式技术文章学习：输入 URL 或文件路径，通过提问引导深度理解
- `/deeplearn-setup` — 将 /deeplearn 安装为全局或项目级 skill
- `/forge` — 项目级 Harness：roadmap 管理 + /fastship 交付 + 收益回收闭环（含 hooks Gate 脚本）
- `/forge-setup` — 在当前项目一键安装 /forge 所需的 hooks、Gate 脚本和 skill 定义
