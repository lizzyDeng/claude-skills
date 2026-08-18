---
name: plan-poster
description: "把一份方案/计划渲染成手绘感中文摘要长图，产出**自包含单文件 HTML**（内联 CSS + base64 内嵌字体 + SVG 抖动滤镜），供人一眼看懂并拍板。触发词：方案摘要图、出成长图、画成海报、plan poster、摘要图。🔴 /fastship 步骤 1.6 与 /conductor 步骤 1.5 递方案给用户时必须调用本 skill。"
---

# /plan-poster — 方案摘要图

plan tree 是给 agent 消费的；**这张图是给人消费的**。同一份方案两个出口，缺一不可。

## 何时用

| 场景 | 用不用 |
|---|---|
| `/fastship` 步骤 **1.6 用户确认** | 🔴 必须 |
| `/conductor` 步骤 **1.5 方案确认**（`需求` + `bugfix`，gate PASS 之后） | 🔴 必须 |
| 用户说「出成长图 / 画成海报 / 给我张摘要图」 | 必须 |
| 纯 bugfix 且改动 < 20 行 | 可跳过，正文写清即可 |
| 说明某个机制怎么运作（非方案） | 可用，换掉第 ③④⑤ 段即可 |

## 做法（3 步，约 3 分钟）

```bash
KIT=~/.claude/skills/plan-poster/kit
mkdir -p .claude/plan-posters
cp "$KIT"/skeleton.html .claude/plan-posters/<slug>.html
#  ← 编辑 <slug>.html，把所有 ※ 换成真内容
python3 "$KIT"/bundle.py .claude/plan-posters/<slug>.html      # 原地变自包含
```

1. **抄骨架 + 改文字**：`※` 是单色占位标记，漏改一处会在图上显眼地留着（故意的）
2. **打包自包含**：`bundle.py` 把 `dayflow.css` 内联进 `<style>`、把得意黑 woff2 转成
   base64 data URI 塞进 css。产出**单文件约 1.5 MB**。**幂等**——文字改完再跑一次即可，
   不会重复内联、不会膨胀。
3. **给用户看**：`SendUserFile(files:["<slug>.html"], display:"render")`

🔴 **为什么必须 bundle（这一步不能省）**：海报靠 `<link href="./dayflow.css">` 找样式，
css 又靠 `url('./fonts/…')` 找得意黑。**单发一个 html 给别人、或丢进 artifact，这两跳全断**——
样式没了、标题回落系统黑体，画风就没了，**但页面照样能打开**，所以这个失败是静默的。

实测（三方对照，`--scale 1` 逐像素比）：

| 变体 | 与「资源齐全」基准的像素差异 | 内容高度 |
|---|---|---|
| **bundle 后**丢进空目录 | **0.0000%**（完全一致） | 3248 |
| 裸 html 丢进空目录 | **100%**（布局塌掉） | 2856 |

参考成品：`kit/example-poster.html`（组件用了一遍）。
产出目录建议加进 `.gitignore` —— html 是可重新生成的产物，不进版本库。

**编辑期预览**：想在浏览器里边改边看，先跑一次 `bundle.py` 就行（自包含之后直接
`open <slug>.html`）；或者把 `dayflow.css` 与 `fonts/` 拷到 html 旁边。

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

- 🔴 **资源缺失是静默失败**：少了 `dayflow.css` 或 `fonts/`，页面照样打得开，只是样式没了、
  标题回落系统黑体。**没有报错、没有 404 提示**，所以别靠"能打开"判断成功——靠 `bundle.py`。
- 1.5 MB 里 **1.4 MB 是字体**（得意黑 Smiley Sans，OFL，本地 woff2 转 base64）。
  正文用系统 `PingFang SC`，别为正文去拉中文字库（20MB 起，且下载常超时）。
- 🔴 `bundle.py` 只替换 `url(<路径>)`，**不碰 `url(#wob-1)`** ——那是同文档 SVG filter 引用，
  当成资源路径替换掉的话满页抖动滤镜全废。改 bundle.py 时别把这个负向匹配 `(?!#)` 弄丢。
- `bundle.py` **幂等**：认 `<style id="poster-inline-css">` 标记，重跑先摘旧的再重新内联，
  不会滚雪球。改完文字直接重跑，不必从 skeleton 重来。
- **测「自包含」必须挪到空目录测**。留在原地测永远是绿的——资源就在旁边。
  而且别用 `render.py` 截图来测：它的 `ensure_assets()` 会**自动把 css/fonts 拷过去**，
  当场把对照组修好（本 skill 改版时真踩过：三个变体截出来尺寸一模一样才发现）。
  直接调 Chrome 截图。
- 需要 PNG 时（发微信 / 小红书 / 存档），`kit/render.py` 还在：
  `python3 render.py <slug>.html <slug>.png --scale 2`。不是主流程，按需用。
