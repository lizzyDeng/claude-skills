# /ship skill 安装指南

## 1. 复制 SKILL.md 到全局 skills 目录

```bash
mkdir -p ~/.claude/skills/ship
cp skills/ship/SKILL.md ~/.claude/skills/ship/SKILL.md
```

## 2. 复制 hooks 脚本到目标项目

```bash
# 进入目标项目
cd /path/to/your/project

# 创建 hooks 目录
mkdir -p .claude/hooks

# 复制 gate 脚本
cp /path/to/claude-skills/skills/ship/hooks/ship_verify_gate.py .claude/hooks/
```

## 3. 配置 hooks

在项目的 `.claude/settings.local.json` 中添加以下配置（合并到已有配置中）：

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py post_bash",
            "timeout": 10,
            "statusMessage": "Tracking ship verification..."
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Checking state file protection..."
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Checking state file protection..."
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_bash",
            "timeout": 10,
            "statusMessage": "Checking ship verification..."
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
cp /path/to/claude-skills/skills/ship/e2e/e2e_runner.py tests/
cp /path/to/claude-skills/skills/ship/e2e/e2e_gate.py tests/

# 创建场景目录
mkdir -p tests/e2e_scenarios

# 可选：复制场景模板
cp /path/to/claude-skills/skills/ship/e2e/scenario_template.json tests/e2e_scenarios/

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
# 查看当前验证状态
python3 .claude/hooks/ship_verify_gate.py status

# Phase 2→3 Gate 检查（LLM 在进入阶段 3 前必须调用）
python3 .claude/hooks/ship_verify_gate.py pre_phase3
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
```
