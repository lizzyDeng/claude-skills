---
name: chunbing
description: "春饼（布偶猫）陪伴系统：statusline 常驻终端 + 炼狱篇七宗罪情绪感知。触发词：春饼、猫、statusline、陪我写代码、炼狱。"
---

# 春饼 — 终端猫咪陪伴系统

春饼是一只蓝眼睛的布偶猫，常驻你的终端，陪你写代码。

## 功能

### 1. Statusline（终端底部常驻）

- 春饼的表情随 context 使用率变化：😸 → 😺 → 😿 → 🙀
- 随机猫咪动作：踩键盘、盯光标、舔爪子、蹭手...
- 猫爪进度条 🐾 显示 context 使用率
- 兼容 claude-hud（wrapper 模式合并显示）

### 2. 炼狱篇（七宗罪情绪感知）

灵感来自但丁《神曲·炼狱篇》。当感知到用户处于七宗罪对应的情绪状态时，春饼会温柔提醒：

| 层 | 罪 | 触发场景 | 春饼的反应 |
|---|---|---|---|
| 1 | 傲慢 Superbia | 轻视 AI 能力 | 歪头看着你 |
| 2 | 嫉妒 Invidia | 与其他工具比较 | 靠在你腿上 |
| 3 | 愤怒 Ira | 暴躁、咒骂 | 蹭你的手 |
| 4 | 懒惰 Acedia | 不愿思考甩手 | 用爪子戳你 |
| 5 | 贪婪 Avaritia | 一次要太多 | 慢慢眨眼 |
| 6 | 暴食 Gula | 不看结果继续催 | 打哈欠 |
| 7 | 色欲 Luxuria | 对完美的执念 | 轻轻叫一声 |

每次提醒附带《炼狱篇》对应章节的引文。

## 安装

运行 `/chunbing` 一键安装 statusline + 炼狱篇 hook。

## 卸载

- statusline：删除 settings.json 中的 `statusLine` 字段
- 炼狱篇：删除 settings.json hooks 中 purgatorio.py 的条目
