#!/bin/bash
# Price Alert 安装脚本
# 用法: bash skills/price-alert/setup.sh

set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SKILL_DIR/com.price-alert.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.price-alert.plist"
ENV_FILE="$SKILL_DIR/.env"

echo "=== Price Alert 安装 ==="
echo "Skill 目录: $SKILL_DIR"

# 检查 .env
if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "❌ 未找到 .env 配置文件"
    echo "   请先复制并填写配置："
    echo "   cp $SKILL_DIR/.env.example $SKILL_DIR/.env"
    echo "   然后编辑 .env 填入 API key 和 Telegram 信息"
    exit 1
fi

# 检查 requests
python3 -c "import requests" 2>/dev/null || {
    echo "安装 requests..."
    pip3 install requests
}

# 生成 plist（替换路径）
sed "s|__SKILL_DIR__|$SKILL_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

# 卸载旧的（如果存在）
launchctl unload "$PLIST_DST" 2>/dev/null || true

# 加载
launchctl load "$PLIST_DST"

echo ""
echo "✅ 安装完成！"
echo "   每 5 分钟自动检查价格波动"
echo "   日志: $SKILL_DIR/price_alert.log"
echo ""
echo "常用命令："
echo "   python3 $SKILL_DIR/price_alert.py --check          # 查看当前价格"
echo "   python3 $SKILL_DIR/price_alert.py --status         # 查看波动状态"
echo "   python3 $SKILL_DIR/price_alert.py --test-telegram  # 测试 Telegram 通知"
echo "   launchctl unload $PLIST_DST                        # 停止监控"
echo "   launchctl load $PLIST_DST                          # 重新启动"
