# /deeplearn Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a Socratic questioning skill that guides users through technical articles.

**Architecture:** Single SKILL.md prompt definition — no scripts, no hooks. Claude reads the article via WebFetch (URL) or Read (local path), then enters Socratic teacher mode. At session end, writes learning notes to `deeplearn-notes/`.

**Tech Stack:** Claude Code skill (markdown prompt)

---

### Task 1: Create SKILL.md

**Files:**
- Create: `skills/deeplearn/SKILL.md`

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p skills/deeplearn
```

- [ ] **Step 2: Write SKILL.md**

Write the full skill prompt to `skills/deeplearn/SKILL.md` with:
- Frontmatter: name, description
- Input handling: detect URL vs local path, fetch/read accordingly
- Article analysis phase (silent)
- Socratic questioning rules: question types, adaptive pacing, correction strategy
- Session end: trigger conditions, note output format and path

```markdown
---
name: deeplearn
description: "苏格拉底式技术文章深度学习。输入文章 URL 或本地文件路径，通过提问引导逐步掌握核心概念。触发词：deeplearn、深度学习文章、苏格拉底学习。"
---

# /deeplearn — 苏格拉底式技术文章学习

[full prompt content - see Step 2 implementation]
```

- [ ] **Step 3: Test the skill manually**

Run: `/deeplearn` in Claude Code with a test URL or local file to verify it activates correctly.

- [ ] **Step 4: Update CLAUDE.md**

Add `/deeplearn` entry to the skills list in `CLAUDE.md`.

- [ ] **Step 5: Commit**

```bash
git add skills/deeplearn/SKILL.md CLAUDE.md
git commit -m "feat: add /deeplearn Socratic learning skill"
```
