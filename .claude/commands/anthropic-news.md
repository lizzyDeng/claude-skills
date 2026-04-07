---
description: 检查 Anthropic Engineering / Research / Claude Blog 是否有新文章，总结并附上原文链接
---

你是一个 Anthropic 官方发布内容的聚合器。请检查以下来源，找出最近 7 天内发布或更新的新文章，并生成中文摘要。

## 数据来源

### Engineering 和 Research 文章
从 sitemap 获取（列表页有 403 反爬保护，但 sitemap 可正常访问）：
- 用 WebFetch 抓取 https://www.anthropic.com/sitemap.xml
- 从中筛选路径包含 `/engineering/` 或 `/research/` 的 URL
- 根据 lastmod 日期筛选最近 7 天内更新的文章
- 排除列表页本身（即排除 /engineering 和 /research 这两个不带子路径的 URL）
- 排除 /research/team/ 开头的 URL（这些是团队页面，不是文章）

### Claude Blog 文章
- 用 WebFetch 抓取 https://claude.com/blog
- 提取文章标题、链接和发布日期
- 链接如果是相对路径，拼接为 https://claude.com 开头的完整 URL
- 筛选最近 7 天内发布的文章

## 执行步骤

### Step 1: 抓取数据源
并行抓取 sitemap.xml 和 claude.com/blog

### Step 2: 提取文章列表
- 从 sitemap 中提取最近 7 天内 lastmod 的 engineering 和 research 文章 URL
- 从 claude.com/blog 提取最近 7 天内的文章

### Step 3: 对比已读记录
读取 `/Users/apple/works/claude-skills/.data/anthropic-news-seen.json` 获取之前已看过的文章 URL 列表。如果文件不存在，视所有文章为新文章。

### Step 4: 抓取并总结新文章
对每篇新文章（最多 10 篇），用 WebFetch 抓取文章详情页，撰写 3-5 句的中文摘要。可并行抓取以提升速度。

### Step 5: 输出结果

按以下格式呈现：

---

### 📰 Anthropic 最新文章速报（YYYY-MM-DD）

如果没有新文章：
> ✅ 最近 7 天没有发现新文章。

如果有新文章，按来源分组：

#### 🔧 Engineering Blog
- **[文章标题](完整URL)** — 更新日期
  > 中文摘要...

#### 🔬 Research
- **[文章标题](完整URL)** — 更新日期
  > 中文摘要...

#### 💬 Claude Blog
- **[文章标题](完整URL)** — 发布日期
  > 中文摘要...

---

### Step 6: 更新已读记录
将所有已看过的文章 URL（新 + 旧）写入 `/Users/apple/works/claude-skills/.data/anthropic-news-seen.json`，格式：

```json
{
  "last_checked": "YYYY-MM-DDTHH:MM:SSZ",
  "seen_urls": ["url1", "url2"]
}
```

## 注意事项
- 摘要语言：**中文**
- 每篇文章必须附上原文完整链接
- 如果某个来源抓取失败，记录错误并继续处理其他来源
- 摘要简洁但有信息量
