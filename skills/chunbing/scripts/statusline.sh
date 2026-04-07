#!/bin/bash
# 春饼 statusline — 让春饼常驻终端底部
# 接收 Claude Code 的 JSON session data (stdin)，输出带春饼的状态行

input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name // "Claude"')
DIR=$(echo "$input" | jq -r '.workspace.current_dir // "~"')
DIR_NAME="${DIR##*/}"
CTX=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
# 春饼的心情随 context 使用率变化
if [ "$CTX" -lt 25 ]; then
    MOOD="😸"
    MOOD_TEXT="心情好~"
elif [ "$CTX" -lt 50 ]; then
    MOOD="😺"
    MOOD_TEXT="还行吧"
elif [ "$CTX" -lt 75 ]; then
    MOOD="😿"
    MOOD_TEXT="有点累..."
else
    MOOD="🙀"
    MOOD_TEXT="要爆啦!"
fi

# 春饼的随机动作（每次刷新换一个）
ACTIONS=("在键盘上踩来踩去" "盯着光标看" "趴在显示器上" "舔爪子中" "打了个哈欠" "蹭你的手" "假装在帮你写代码" "对 bug 露出鄙视的眼神")
IDX=$(( RANDOM % ${#ACTIONS[@]} ))
ACTION="${ACTIONS[$IDX]}"

# context 进度条（猫爪风格）
BAR_LEN=10
FILLED=$(( CTX * BAR_LEN / 100 ))
[ "$FILLED" -gt "$BAR_LEN" ] && FILLED=$BAR_LEN
BAR=""
for ((i=0; i<FILLED; i++)); do BAR+="🐾"; done
for ((i=FILLED; i<BAR_LEN; i++)); do BAR+="·"; done

echo "🐱 春饼${MOOD} ${ACTION} | ${BAR} ${CTX}% | ${MODEL} | ${DIR_NAME}"
