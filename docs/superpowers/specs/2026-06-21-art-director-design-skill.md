# Art Director Skill — 设计文档

> 状态:草案待评审 · 日期:2026-06-21 · 仓库:claude-skills
> 工作名:`art-director`(命名待确认,见 §10 开放项)

## 1. 问题与目标

起一个新工程时,常需要带**复杂视觉**的页面:复杂背景大图(bg)、带透明通道的切图(角色 / 物体 / 装饰元素)。
现有 `frontend-design` skill 只产**代码**,且明确用 CSS 假造氛围(gradient mesh / noise / 几何),**不产任何栅格图素材**。
缺口 = 把"按风格出 design"和"生成真·图片素材并接进页面"连成一条可跑的流水线。

**目标(C 范围):** 输入风格 brief → 产出一个**素材齐全、能直接跑的 HTML/CSS 页面**:
1. 复用 `frontend-design` 出页面设计;
2. 用 APImart 的 gpt-image 模型生成真实 bg 大图 + 透明切图;
3. 把生成的素材**回填(wiring)**进页面代码。

## 2. 非目标 / v1 范围收窄(YAGNI)

- **v1 只锁 HTML/CSS 单页**:`url()` / `src` 接线最直接。React/Vue 的 asset import 管线后置。
- 不做"切割已有成品稿"(SAM / 去背)——本 skill 只**从零生成**素材(方案 A 已定)。
- 不做素材二次编辑 / 局部重绘(APImart 有 edit/mask 能力,v1 不用)。
- 不做品牌一致性强约束(生成模型有 style drift,UI chrome / 精确图标不在本 skill 职责内)。

## 3. 本质定义与类比

这个 skill 本质是一个 **编排器(orchestrator)+ 素材生产引擎**,不是一个真 wrap。
类比:**forge 包 fastship** —— 不修改被包的 skill,只在外层加指令、收结构化产物、跑校验门禁。
`frontend-design` 是纯 prompt skill,无脚本 / 无结构化输出,所以"增强"= 在它的设计阶段**注入素材约定**,并由本 skill 强加一个 **manifest 契约**。

## 4. 架构

```
风格 brief
   │
   ▼
[Stage 1: design]  invoke frontend-design + 注入"素材占位约定"
   │   产出: 页面代码(HTML/CSS) + asset manifest(每条: id·kind·prompt·aspect·transparent·path·placeholder)
   ▼
[Stage 2: extract + gate]  从代码抽占位符,与 manifest 双向对账(复用 fastship contract-extractor 模式)
   │   占位符无对应 manifest 行 / manifest 行未被引用 → 硬失败
   ▼
[Stage 3: asset-gen 引擎]  APImart 异步:按 kind 注册表派发模型 → 提交→轮询→下载到约定 path
   │   并发生成、逐素材落盘、可 resume;每素材生成后校验(PNG alpha / 非空 / 尺寸)
   ▼
[Stage 4: wiring gate]  校验 manifest 每条 path 已在磁盘存在且被代码引用 → 页面可跑
   │
   ▼
能跑的页面 + assets/gen/*.png + .art-director/manifest.json
```

**单元边界(每个职责单一、接口清晰、可独立测):**
- `manifest`(数据契约,§5)—— 唯一接口,贯穿所有 stage。
- `registry`(kind→模型派发,§6)—— 纯函数,输入 manifest 条目,输出 APImart 请求体。
- `apimart_client`(§7)—— 提交 / 轮询 / 下载,异步 + 重试,不懂 manifest。
- `extractor` / `wiring_gate`(§8)—— 代码↔manifest 对账,不懂网络。

## 5. Asset Manifest(核心契约 / 那条缝)

```jsonc
{
  "version": 1,
  "style": {
    "brief": "cyberpunk noir, neon-on-black, editorial",
    "palette": ["#0a0a0f", "#ff2d78"],   // 可选,供生图 prompt 注入一致性
    "mood": "rain-soaked, high-contrast"
  },
  "assets": [
    {
      "id": "hero-bg",
      "kind": "bg",                         // bg | cutout —— 唯一派发键
      "prompt": "complex rain-soaked neon alley at night, cinematic, ...",
      "aspect": "16:9",                     // bg: 15 种比例之一
      "resolution": "4k",                   // bg 专属: 1k|2k|4k
      "transparent": false,
      "path": "assets/gen/hero-bg.png",
      "placeholder": "url(assets/gen/hero-bg.png)"   // 代码里实际引用串,供对账
    },
    {
      "id": "mascot",
      "kind": "cutout",
      "prompt": "chibi fox mascot, full body, ...",
      "aspect": "2:3",                      // cutout: 仅 1:1 | 2:3 | 3:2
      "transparent": true,
      "format": "png",                      // cutout 固定 png(透明需要)
      "path": "assets/gen/mascot.png",
      "placeholder": "src=\"assets/gen/mascot.png\""
    }
  ]
}
```

- manifest 由 Stage 1 产出(frontend-design 按约定追加),Stage 2 校验,Stage 3 消费,Stage 4 终验。
- `path` 是**预先约定的终值**:代码生成时就指向它,生图直接下载到它 → 接线"免费"。

## 6. 模型派发注册表(kind → 模型,无 if-else)

| kind | model | 透明 | size 取值 | resolution | 上限 |
|---|---|---|---|---|---|
| `bg` | `gpt-image-2` | ❌ | 15 种比例 / 像素 | `1k`/`2k`/`4k` | 4K |
| `cutout` | `gpt-image-1.5-official` | ✅ `background:transparent` + `output_format:png` | `1:1`/`2:3`/`3:2` | —(无 tier) | 1536×1024 |

实现为 handler 注册表:`HANDLERS = { "bg": build_bg_request, "cutout": build_cutout_request }`,
新增素材类型 = 注册新 handler,不动派发逻辑(对齐项目"禁 if-else 用注册表"规约)。

**分辨率可切(两级,bg 专属):**
- **全局默认** `2k`(CLI/config 旗标 `--bg-resolution`,省钱档)。
- **逐素材覆盖**:manifest 某条 bg 写 `"resolution":"4k"` → 单独拉满(典型:hero 4k、其余 2k)。成本随档位 $0.005→0.211/张。
- **cutout 不可切分辨率**:gpt-image-1.5-official 无 resolution tier,只能切 size,天花板 1536。
- 校验落在 handler:bg 断言 `resolution ∈ {1k,2k,4k}`;cutout 若传 resolution → 直接拒。

## 7. APImart 接入契约(已对真实文档核对)

- **Base**:`https://api.apimart.ai/v1/images/generations` · **Auth**:`Authorization: Bearer $APIMART_API_KEY`
- **全异步**:`POST` 提交 → 返回 `task_id` → 轮询 `GET /v1/tasks/{task_id}` → 拿图片 `url` → **自己下载**(不回 base64)。
- **bg 请求体**(gpt-image-2):`model, prompt, n, size, resolution, image_urls?, official_fallback?`(**无** background/output_format)。
- **cutout 请求体**(gpt-image-1 端点,`model:"gpt-image-1.5-official"`):`model, prompt, size, n, quality, background:"transparent", output_format:"png", moderation?, output_compression?, image_urls?, mask_url?`。
- 轮询需:超时上限 + 退避;`status` 终态判定;失败态捕获。

> 校验记录:gpt-image-2 不支持透明(`background:"transparent"` 被拒);透明切图只能走 gpt-image-1 端点 + `gpt-image-1.5-official`,封顶 1536×1024、PNG-only。

## 8. Extractor + Wiring Gate(复用 fastship 模式)

- **Stage 2 extractor**:扫页面代码,正则提取所有指向 `assets/gen/` 的引用 → 与 manifest `placeholder`/`path` **双向对账**。
  - 代码引用了某 path 但 manifest 无此条 → 失败(漏登记)。
  - manifest 有某条但代码未引用 → 失败(白生成 / 接线漏)。
- **Stage 4 wiring gate**:manifest 每条 `path` 必须**磁盘存在 + 非空 + 格式正确**(cutout 必须有 alpha 通道)。
  - 任一缺失 → 失败并报出具体 id,不谎称完成(对齐项目"闭环验证"红线)。

## 9. asset-gen 引擎(Python 驱动)与韧性

- **并发**:asyncio 并发生成(上限可配),各素材独立。
- **逐素材落盘 + resume**:每素材生成后立即写文件 + 更新 manifest 内 per-asset `status`;重跑只补缺失(吸取 persona-bench 韧性教训:有 retry / 部分落盘 / 可 resume)。
- **重试**:网络 / 5xx / 轮询超时 → 指数退避重试 N 次;终败标记该素材 failed,**不拖垮其余**。
- **降级**:某素材终败 → 保留代码占位符 + 注入一行警告注释,页面仍可加载(不白屏)。

## 10. 错误处理

| 情况 | 行为 |
|---|---|
| 缺 `APIMART_API_KEY` | 立即失败,明确提示环境变量名 |
| task 失败 / 轮询超时 | 标记该素材 failed,其余继续,末尾汇总失败清单 |
| 透明请求被路由到不支持模型 | 构造上不可能(registry 强制 cutout→1.5);仍加断言兜底 |
| 部分完成 | manifest 记录 per-asset status,重跑 resume 仅补缺失 |
| extractor / wiring 对账失败 | 硬失败,报具体 id,禁止"已完成"表述 |

## 11. 产物目录

```
<project>/
  index.html              # frontend-design 产出的页面(已接线)
  assets/gen/
    hero-bg.png
    mascot.png
  .art-director/
    manifest.json         # 含 per-asset status
    run.log
```

## 12. 测试

- **单元**:registry 派发(kind→正确 model+参数)、manifest 解析/校验、extractor 对账(能抓占位符/manifest 不匹配)、downloader(mock APImart)。
- **E2E**:fixture manifest → mock APImart(录制 fixture)→ 断言文件落盘 + 代码已接线 + cutout 有 alpha。
- **真 API smoke**:env flag 后置(花钱),对齐本仓库其他 skill 把真模型测试 gate 在 env 之后的惯例。

## 13. 开放项(待用户拍板)

1. **skill 命名**:`art-director` / `asset-forge` / 其他?(工作名 `art-director`)
2. **API key 环境变量名**:建议 `APIMART_API_KEY`,确认?
3. **风格前置**:是否需要在 frontend-design 前再挂一道 `design-consultation` 锁设计系统(token/配色),还是 brief 直接喂 frontend-design 即可(v1 倾向后者,YAGNI)。
4. ~~**bg 默认分辨率**~~ — ✅ 已定:默认 `2k`,两级切换(全局 `--bg-resolution` + 逐素材 manifest 覆盖);cutout 无分辨率档,只切 size 封顶 1536(详 §6)。
```
