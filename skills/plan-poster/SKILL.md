---
name: plan-poster
description: "把一份方案/计划渲染成手绘感中文摘要长图（HTML + SVG 抖动滤镜 + Chrome 无头截图），供人一眼看懂并拍板。触发词：方案摘要图、出成长图、画成海报、plan poster、摘要图。🔴 /fastship 步骤 1.6 与 /conductor 步骤 1.5 递方案给用户时必须调用本 skill。"
---

# /plan-poster — 方案摘要图

plan tree 是给 agent 消费的；**这张图是给人消费的**。同一份方案两个出口，缺一不可。

## 何时用

| 场景 | 用不用 |
|---|---|
| `/fastship` 步骤 **1.6 用户确认** | 🔴 必须 |
| `/conductor` 步骤 **1.5 方案确认**（`需求` 类） | 🔴 必须 |
| 用户说「出成长图 / 画成海报 / 给我张摘要图」 | 必须 |
| 纯 bugfix 且改动 < 20 行 | 可跳过，正文写清即可 |
| 说明某个机制怎么运作（非方案） | 可用，换掉第 ③④⑤ 段即可 |

## 做法（3 步，约 3 分钟）

```bash
KIT=~/.claude/skills/plan-poster/kit
mkdir -p .claude/plan-posters
cp "$KIT"/skeleton.html .claude/plan-posters/<slug>.html
#  ← 编辑 <slug>.html，把所有 ※ 换成真内容
python3 "$KIT"/render.py .claude/plan-posters/<slug>.html \
                        .claude/plan-posters/<slug>.png --scale 2
```

1. **抄骨架 + 改文字**：`※` 是单色占位标记，漏改一处会在图上显眼地留着（故意的）
2. **渲染**：`render.py` 会自动把 `dayflow.css` 和 `fonts/` 补到 html 旁边，不用手动拷
3. **给用户看**：`SendUserFile`（`display:"render"`）发 PNG，再 `open <slug>.png`

参考成品：`kit/example-poster.html`（组件用了一遍）。
产出目录建议加进 `.gitignore` —— PNG 是可重新生成的产物，不进版本库。

## 内容规则（比样式重要）

图不是装饰，是**让人 30 秒内能拍板或否掉**。七段，顺序不许换：

| # | 段 | 必须回答 | 常见写坏 |
|---|---|---|---|
| ① | 大标题 | **问题**是什么 | ❌ 写方案名/模块名。读者要先知道疼在哪 |
| ② | 判断 | 根因 + 为什么是这个方案 | ❌ 只写"要做什么"，不写"为什么不是别的" |
| ③ | 怎么做 | ≤5 步，每步落到 `file:line` 或模块 | ❌ 超过 5 步（超了说明该拆票） |
| ④ | 验收标准 | 🔴 **可执行命令 / 可观察行为**，含至少一条非 happy path | ❌ 写"测试通过"这种不可执行的话 |
| ⑤ | 这次不做 | 边界 + 已知风险 | ❌ 整段删掉。这段最容易被跳过，也最容易事后吵架 |
| ⑥ | 一句话总结 | 把取舍说清 | ❌ 写成口号 |
| ⑦ | 出处 | plan 文件路径 + 影响面 + 日期 | ❌ 省掉，图就追不回原始 plan 了 |

🔴 **图上的验收标准必须与 plan 里锁定的逐字一致**。图是 plan 的投影，不是二次创作——
两边不一致时以 plan 为准，并回头修图。

🔴 **装饰气泡（右上角）放症状 / 用户原话 / 报错片段，不放方案**。它的作用是把"疼"摆出来。

## 样式红线（改了就不像了）

1. **抖动滤镜只能加在 `::before` 上**。加在元素本身，里面的中文会跟着抖糊。
2. **`.sketch::before` 的 `z-index` 必须是 `-1`**，不是 `0`。伪元素是绝对定位盒，同层会盖住
   **裸文本节点**（元素子节点能靠 `position:relative` 抬起来，纯文本抬不了）。
   靠 `.poster{position:relative;z-index:0}` 兜住，不会穿到纸底之下。
3. **只有三种颜色**：米底 `--paper` / 深棕墨 `--ink` / 橙 `--accent`。
   橙只给：关键词、编号圈、图表曲线、下划线。**不许引入第四支笔。**
4. **禁用 emoji 图标**。彩色 emoji 一出现，双墨色纪律就废了。要图标就画 20 行 stroke SVG。
5. **不要改 `filter` 定义、不要改 class 名**。三个 `seed`（`sk-2` / `sk-3`）轮着用，
   否则满页框子抖成一个样，一眼就假。

## 组件速查

```html
<div class="sketch fill-warm sk-2">…</div>   <!-- 抖框卡片；fill-warm/fill-sink/fill-paper -->
<div class="sketch dashed">…</div>            <!-- 虚线框（用于"被否掉的做法"）-->
<span class="mark">关键词</span>               <!-- 橙色手绘波浪下划线 -->
<span class="step-no"><span>1</span></span>   <!-- 手绘编号圈 -->
<div class="bubble warm tail">…</div>         <!-- 对话气泡（tail = 带尾巴）-->
<div class="grid-4">…</div> / .grid-2         <!-- 四联卡 / 两栏 -->
<div class="sec-label good">…</div>           <!-- 分节标题；good=绿 bad=红 -->
<div class="callout">…</div>                  <!-- 底部强调条 -->
<div class="hr-dash"></div>                   <!-- 手绘虚线分隔 -->
<b>file.rs:12</b>                             <!-- 行内代码/路径（Poppins 小字）-->
```

## 坑

- Chrome 新版无头**不支持整页截图**，只能开高窗口再裁 —— `render.py` 干的就是这个。
- `render.py` 取**最后一行**的颜色当空白色来裁，不能取左上角：海报满宽时两侧没有桌面色。
- `--scale 2` 出 2x 图，发微信/小红书才不糊；正文预览用 `--scale 1` 更快。
- 字体是本地 woff2（得意黑 Smiley Sans，OFL）。`dayflow.css` 按相对路径找 `./fonts/`，
  **复制 css 时必须连 `fonts/` 一起复制**，否则标题回落成系统黑体，画风就没了。
- 正文用系统 `PingFang SC`，别为正文去拉中文字库（20MB 起，且下载常超时）。
