#!/bin/bash
# 春饼 + claude-hud 合并 statusline
# 先跑 claude-hud，再追加春饼那行

input=$(cat)

# 1. claude-hud 输出
plugin_dir=$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/claude-hud/claude-hud/*/ 2>/dev/null | awk -F/ '{ print $(NF-1) "\t" $0 }' | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -1 | cut -f2-)
HUD_OUT=$(echo "$input" | "/Users/apple/.bun/bin/bun" "${plugin_dir}src/index.ts" 2>/dev/null)

# 2. 春饼输出
CHUNBING_OUT=$(echo "$input" | /Users/apple/works/claude-skills/skills/chunbing/scripts/statusline.sh 2>/dev/null)

# 3. 合并：先 hud 再春饼
if [ -n "$HUD_OUT" ]; then
    echo "$HUD_OUT"
fi
echo "$CHUNBING_OUT"
