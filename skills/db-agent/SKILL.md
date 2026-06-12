---
name: db-agent
description: Use when the user wants to explore or query a project database in natural language — triggers include "查库", "看数据库", "数据问答", "用自然语言查数据", "ask the database", "data agent", or wanting a chat UI over Postgres/SQLite data without writing SQL by hand.
---

# DB Agent Skill

把业务数据库（Postgres / SQLite）导成只读 SQLite 快照，用
[datasette-agent](https://github.com/datasette/datasette-agent) 提供：

- **Web 聊天界面** `http://localhost:<port>/-/agent` — 自然语言问数据，每步 SQL 可查看
- **CLI 一次性问答** — 适合脚本和快速验证
- 快照天然只读，agent 写不坏源库

## Usage

统一入口（首次运行自动建 venv，约 1 分钟）：

```bash
SCRIPT=/Users/apple/works/claude-skills/skills/db-agent/scripts/dbagent.py

# 一步到位：导快照 + 起服务（前台进程，放后台跑）
python3 $SCRIPT run "postgresql://user:pass@localhost:5432/mydb" -o /tmp/mydb.db --port 8765

# 或分步
python3 $SCRIPT export "postgresql://..." -o /tmp/mydb.db --exclude messages user_secrets
python3 $SCRIPT serve /tmp/mydb.db --port 8765
python3 $SCRIPT chat  /tmp/mydb.db -p "有多少用户？"
```

## Key behaviors（给 Claude 的执行要点）

1. **先找 API key**：serve/chat 需要可用的 LLM key。先查环境变量；没有就 grep
   项目 `.env`（常见变量名 `CLAUDE_API_KEY` / `ANTHROPIC_API_KEY` / `XAI_API_KEY`）并 export，
   不要把 key 明文打印到对话里。Anthropic key 报 credit 不足时换其他 key（见第 6 条）。
2. **serve 是前台进程**：用后台方式运行（`run_in_background` 或 `nohup ... &`），
   然后 `curl -s -o /dev/null -w '%{http_code}' http://localhost:<port>/-/agent` 确认 200，
   再把 URL 给用户。
3. **验证答案**：给用户结论前，用 `chat` 问一个已知答案的问题，并直连源库交叉验证
   （不要只信 agent 的输出）。
4. **隐私**：快照会把数据发给 LLM。生产/staging 真实用户数据用 `--exclude` 跳过敏感表
   （messages、聊天内容、token 类表），或只导本地 dev 数据。
5. **数据是静态快照**：源库变了要重新 `export`（几秒钟），刷新后重启 serve。
6. **模型**：默认 `anthropic/claude-haiku-4-5-20251001`（省钱够用）；
   复杂分析换 `--model anthropic/claude-sonnet-4-6` 或设 `DB_AGENT_MODEL`。
   任意 OpenAI 兼容端点（xAI Grok / DeepSeek / Ollama...）先注册再用：

   ```bash
   python3 $SCRIPT add-model xai/grok-4.3 --api-base https://api.x.ai/v1 --key-env XAI_API_KEY
   python3 $SCRIPT chat /tmp/mydb.db --model xai/grok-4.3 -p "..."
   ```

## Scope

- 支持：`postgres://` / `postgresql://` URL、SQLite 文件路径
- 不支持：MySQL（可先用 `db-to-sqlite` 转成 SQLite 文件再喂进来）
- JSONB / UUID / 数组 / Decimal 列已做通用转换（这是不直接用 db-to-sqlite 导 PG 的原因——它遇到 JSONB 会崩）
- 权限对匿名全开（本地 demo 用）；要对外暴露必须先收紧 `<snapshot>.datasette.yml`

## Troubleshooting

- `No module named psycopg` / 装包失败 → `python3 $SCRIPT setup --force` 重建 venv
- datasette-agent 装不上 → 它强依赖 datasette 1.0 预发布版，脚本已带 `--prerelease=allow`/`--pre`，别手动 pip install
- agent 答错列名 → 正常，它会自己调 describe_table 纠正；持续答错就换强模型
