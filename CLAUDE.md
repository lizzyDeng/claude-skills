# Claude Skills Collection

本项目是 Claude Code 自定义 skills（slash commands）的集合。

## 使用方式

在此目录下运行 Claude Code，即可通过 `/skill-name` 调用对应 skill。

## Skills 列表

- `/anthropic-news` — 检查 Anthropic 官方博客是否有新文章，并生成中文摘要
- `/ship` — 结果驱动开发：Brainstorm → 自主执行 → E2E 验证循环（含 hooks Gate 脚本）
- `/ship-setup` — 在当前项目一键安装 /ship 所需的 hooks + E2E 工具链
- `price-alert` — 监控黄金 (XAU/USD) 和 BTC 价格，大幅波动时通过 Telegram 通知
