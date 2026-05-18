# /fastship skill 安装指南

推荐使用 `/fastship-setup` 一键安装。以下是手动安装步骤：

## 0. 前置依赖

### 0.1 superpowers 插件（🔴 必需）

fastship 的阶段 1.4 / 阶段 2 强制调用 superpowers 里的 skill：

- `writing-plans` — 阶段 1.4 写计划
- `executing-plans` — 阶段 2 按计划执行
- `subagent-driven-development` — 有 subagent 环境时替代 executing-plans

### 0.2 全局 grill-me skill（🔴 必需）

fastship 的阶段 1.5 强制调用全局 `grill-me` 对 plan 做结构化拷问。该 skill 应作为全局 skill 安装在 `~/.claude/skills/grill-me/`（或随你常用的 skills 集合一起分发），无需额外配置——只要在 Claude Code 会话里能通过 `Skill(skill="grill-me")` 调用即可。

未装 grill-me → 1.5 拷问关卡无法跑完，等于卡在 1.5 不能进 1.6。

安装 superpowers 插件：

```bash
# 在 Claude Code 中
/plugin install superpowers@claude-plugins-official
```

确认插件已装：

```bash
ls ~/.claude/plugins/cache/claude-plugins-official/superpowers/
# 应能看到 skills/writing-plans/ 和 skills/executing-plans/
```

未装 superpowers → fastship 的 Plan Gate 仍会生效，但你没有对应 skill 可调，等于卡死。

## 1. 复制 skill 定义到目标项目

```bash
# 进入目标项目
cd /path/to/your/project

# 创建 commands 目录
mkdir -p .claude/commands

# 复制 skill 定义
cp /path/to/claude-skills/skills/fastship/SKILL.md .claude/commands/fastship.md
```

## 2. 复制 hooks 脚本到目标项目

```bash
# 创建 hooks 目录
mkdir -p .claude/hooks

# 复制 gate 脚本（orchestrator 内部委托调用）
cp /path/to/claude-skills/skills/fastship/hooks/ship_verify_gate.py .claude/hooks/
```

## 2.5 复制 orchestrator

```bash
# 创建 tools 目录
mkdir -p .claude/tools

# 复制 orchestrator（hook 入口 + CLI 工具）
cp /path/to/claude-skills/skills/fastship/orchestrator.py .claude/tools/fastship_orchestrator.py
```

## 3. 配置 hooks

hooks 指向 orchestrator（orchestrator 内部 subprocess 调用 ship_verify_gate）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py pre_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: phase check + plan gate..."
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py pre_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: phase check + plan gate..."
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py pre_bash",
            "timeout": 10,
            "statusMessage": "Orchestrator: gates 0-5..."
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py post_bash",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect step completion..."
          }
        ]
      },
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py post_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect file writes..."
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py post_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect file writes..."
          }
        ]
      }
    ]
  }
}
```

## 4. 复制 E2E 工具到目标项目

```bash
# 复制 E2E runner 和 gate
mkdir -p tests
cp /path/to/claude-skills/skills/fastship/e2e/e2e_runner.py tests/
cp /path/to/claude-skills/skills/fastship/e2e/e2e_gate.py tests/

# 创建场景目录
mkdir -p tests/e2e_scenarios

# 可选：复制场景模板
cp /path/to/claude-skills/skills/fastship/e2e/scenario_template.json tests/e2e_scenarios/

# 安装依赖
pip install requests
```

### E2E 工具说明

| 文件 | 职责 | 核心原则 |
|------|------|---------|
| `e2e_runner.py` | 编排场景 + 采集响应数据 | **只采集，不判断** |
| `e2e_gate.py` | 合入前检查数据充分性 + 展示原始数据 | **防止 LLM 偷懒** |
| `e2e_scenarios/*.json` | 场景定义 | 在 brainstorm 阶段与用户共同定义 |

### 工作流

```
Runner 采集数据 → LLM 审查出质量报告 → Gate 验证数据充分性 + 展示原始数据给用户
                                                    ↕
                                          用户对照报告和原始数据判断
```

Runner 是模板实现，项目应根据技术栈定制（如 SSE 流式响应、日志 pipeline 提取等），
但必须保持"只采集不判断"的原则。

## 5. 验证安装

```bash
# Hook gate 状态
python3 .claude/hooks/ship_verify_gate.py status

# Orchestrator 状态（应显示"没有活跃 session"）
python3 .claude/tools/fastship_orchestrator.py status
```

hook gate 输出应包含 `Recall / Plan / Test / E2E / Knowledge / Loop` 六行状态。
orchestrator 输出应为 "❌ 没有活跃 session。"（正常，start 后才有）。

### Gate 一览

| Gate | 触发 | 作用 |
|------|------|------|
| **Plan Gate** (pre_edit) | 编辑代码文件 | 未写 plan 文件 或 未跑 `knowledge_recall` → BLOCK |
| **DB 写入拦截** (pre_bash) | test_passed 后 psql 写入 | 禁止 E2E 阶段构造数据 |
| **E2E 前置** (pre_bash) | 跑 E2E | 必须先跑过单测 |
| **E2E Gate 前置** (pre_bash) | 跑 e2e_gate | 必须单测 + Runner 都完成 |
| **merge/push Gate** (pre_bash) | merge / push | 必须单测 + E2E 都完成 |
| **Knowledge Gate** (pre_bash) | merge / push | 必须 KNOWLEDGE.md 已编辑 或 已显式 `knowledge_skip --reason` |
| **Reflection Gate** (pre_bash) | 重跑 E2E | 上一轮必须已 `loop_record`（sequential 单文件 / parallel `--reflection-dir` ≥2 份 + `--winner`）；失败时反思 ≥200B；`loop_count ≥ 3` 锁死 |

### 常用命令

```bash
# 查看状态
python3 .claude/hooks/ship_verify_gate.py status

# 重置（新需求开始时）
python3 .claude/hooks/ship_verify_gate.py reset

# 放行 Plan Gate（非 /fastship 场景的兜底）
python3 .claude/hooks/ship_verify_gate.py plan_bypass

# 或用环境变量一次性跳过 Plan Gate
FASTSHIP_SKIP_PLAN_GATE=1 <你的命令>

# 1.1a-recall：跨 session 学习（每次 fastship session 起步必跑）
python3 .claude/hooks/ship_verify_gate.py knowledge_recall \
  --query "<需求一句话>" --top 5

# 显式跳过 KNOWLEDGE.md（确实无新教训时使用，必须给 ≥10 字原因）
python3 .claude/hooks/ship_verify_gate.py knowledge_skip \
  --reason "纯文档改动，未触及代码或行为"

# 记录本轮 E2E loop 结果（每轮 E2E 跑完都要调一次）
# Sequential：
python3 .claude/hooks/ship_verify_gate.py loop_record --outcome pass
python3 .claude/hooks/ship_verify_gate.py loop_record --outcome fail \
  --reflection docs/superpowers/plans/<plan>.reflections/loop-N.md

# Parallel hypothesis exploration（一次并行 = 1 次 loop_record）：
python3 .claude/hooks/ship_verify_gate.py loop_record --outcome pass \
  --reflection-dir docs/superpowers/plans/<plan>.reflections/loop-N.parallel \
  --winner hypothesis-a
python3 .claude/hooks/ship_verify_gate.py loop_record --outcome fail \
  --reflection-dir docs/superpowers/plans/<plan>.reflections/loop-N.parallel
```

## 支持的技术栈

Gate 脚本会自动检测项目技术栈并识别对应的测试命令：

| 技术栈 | 检测文件 | 识别的测试命令 |
|--------|---------|---------------|
| Rust | `Cargo.toml` | `cargo test` |
| Node.js | `package.json` | `npm test`, `npx vitest`, `npx jest`, `yarn test` |
| Python | `pyproject.toml` / `setup.py` / `requirements.txt` | `pytest`, `python -m pytest` |
| Go | `go.mod` | `go test` |

E2E 命令识别（通用）：`curl localhost`, `playwright`, `cypress`, `puppeteer`, `selenium`, `agent-browser`, `*e2e*`

## .gitignore

建议在项目 `.gitignore` 中添加：

```
.claude/.ship-verify-state.json
.claude/.fastship-orchestrator-state.json
.claude/.fastship-brief.md
.claude/.fastship-grill-result.md
```
