# claude-skills — 开发经验知识库

> 记录 skill / hook 维护中的关键经验教训，供后续开发参考。

---

## Fastship Ship Gate 与项目本地 Hook 漂移

### 关键认知

- **先查 source-of-truth，再修项目本地副本**：项目 `.claude/hooks/ship_verify_gate.py` 可能是旧版或简化版，不能据此判断 `claude-skills` 源头缺能力。本次 aifriends 本地 hook 缺 `post_edit` knowledge 置位，但 `/Users/apple/works/claude-skills/skills/fastship/hooks/ship_verify_gate.py` 已经有完整 `post_edit`、`knowledge_skip`、`knowledge_recall`。
- **安装/复制脚本会覆盖本地修复**：如果只修项目本地 `.claude/hooks/ship_verify_gate.py`，后续按 INSTALL 或 settings 重新从 `claude-skills` 复制 hook 时可能丢失补丁。维护 hook 行为时要明确改的是源头、项目副本，还是两者都需要。
- **Forge 依赖 fastship state 字段兼容性**：Forge Gate 4 接受 `test_passed` 或 `cargo_test_passed`，但必须有 `e2e_executed` 和 `knowledge_acknowledged` / `knowledge_skipped` / `knowledge_done` 之一。改 fastship state schema 时要同步验证 Forge gate。
- **文档存在不等于项目安装生效**：`fastship/SKILL.md` 和 `INSTALL.md` 可以描述完整流程，但项目本地 hook 仍可能是旧代码。遇到 gate blocker 时要同时检查文档、源 hook、项目 hook、`.ship-verify-state.json` 四处。
