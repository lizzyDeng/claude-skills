#!/usr/bin/env python3
"""
hl_bot.py — Telegram Bot 监听模式，通过聊天消息下单

支持命令：
  买 BTC 0.001 67000       → 限价买入
  卖 BTC 0.001 70000       → 限价卖出
  撤单 BTC 369556535415    → 撤销挂单
  挂单                      → 查看当前挂单
  持仓                      → 查看账户概览
  价格                      → 查看当前 BTC/XAU 价格
  帮助                      → 显示命令列表

用法：
  python3 hl_bot.py                    # 前台运行
  nohup python3 hl_bot.py &            # 后台运行
"""

import json
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# ---------- 配置 ----------

def load_config():
    env_path = Path(__file__).parent / ".env"
    config = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"').strip("'")

    return {
        "telegram_bot_token": config.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": config.get("TELEGRAM_CHAT_ID", ""),
    }


# ---------- Telegram API ----------

def tg_request(token, method, data=None):
    """调用 Telegram Bot API"""
    url = f"https://api.telegram.org/bot{token}/{method}"
    args = ["curl", "-s", "--max-time", "30", "-X", "POST", url,
            "-H", "Content-Type: application/json"]
    if data:
        args += ["-d", json.dumps(data)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=35)
        if r.returncode == 0 and r.stdout:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def tg_get_updates(token, offset=None, timeout=20):
    """长轮询获取新消息"""
    data = {"timeout": timeout}
    if offset is not None:
        data["offset"] = offset
    return tg_request(token, "getUpdates", data)


def tg_reply(token, chat_id, text):
    """回复消息"""
    tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    })


# ---------- 执行交易命令 ----------

def run_trader(*args):
    """调用 hl_trader.py"""
    script = str(Path(__file__).parent / "hl_trader.py")
    try:
        r = subprocess.run(
            ["python3", script] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        output = (r.stdout + r.stderr).strip()
        return output if output else "（无输出）"
    except Exception as e:
        return f"执行失败: {e}"


def get_prices():
    """获取当前价格"""
    results = []
    # BTC
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "10",
             "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"],
            capture_output=True, text=True, timeout=15,
        )
        btc = float(json.loads(r.stdout)["price"])
        results.append(f"₿ BTC/USDT: ${btc:,.2f}")
    except Exception:
        results.append("₿ BTC: 获取失败")

    # XAU
    price_alert_env = Path(__file__).parent.parent / "price-alert" / ".env"
    xau_key = ""
    if price_alert_env.exists():
        for line in price_alert_env.read_text().splitlines():
            if line.startswith("TWELVE_DATA_API_KEY="):
                xau_key = line.split("=", 1)[1].strip()
    if xau_key:
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "10",
                 f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={xau_key}"],
                capture_output=True, text=True, timeout=15,
            )
            xau = float(json.loads(r.stdout)["price"])
            results.append(f"🥇 XAU/USD: ${xau:,.2f}")
        except Exception:
            results.append("🥇 XAU: 获取失败")

    return "\n".join(results)


# ---------- 解析命令 ----------

# Spot BTC 在 Hyperliquid 上的名称
SPOT_ALIASES = {
    "BTC": "@142",
    "ETH": "@1",  # 可能需要确认
}


def resolve_coin(coin_str):
    """将用户输入的币种转为 Hyperliquid spot 名称"""
    upper = coin_str.upper()
    if upper in SPOT_ALIASES:
        return SPOT_ALIASES[upper], upper
    return coin_str, coin_str


def parse_and_execute(text):
    """解析用户消息并执行"""
    text = text.strip()

    # 帮助
    if text in ("帮助", "help", "/help", "/start"):
        return (
            "📋 *交易命令*\n\n"
            "`买 BTC 0.001 67000` — 限价买入\n"
            "`卖 BTC 0.001 70000` — 限价卖出\n"
            "`撤单 BTC 369556535415` — 撤销挂单\n"
            "`挂单` — 查看当前挂单\n"
            "`持仓` — 查看账户概览\n"
            "`价格` — 查看 BTC/XAU 价格\n"
            "`帮助` — 显示此菜单"
        )

    # 价格
    if text in ("价格", "price", "p"):
        return f"📊 *当前价格*\n\n{get_prices()}"

    # 持仓/状态
    if text in ("持仓", "状态", "status", "s"):
        return run_trader("--status")

    # 挂单
    if text in ("挂单", "订单", "orders", "o"):
        return run_trader("orders")

    # 买入: 买 BTC 0.001 67000
    m = re.match(r'^[买多buy]\s+(\S+)\s+([\d.]+)\s+([\d.]+)$', text, re.IGNORECASE)
    if m:
        coin_input, size_str, price_str = m.groups()
        coin, display = resolve_coin(coin_input)
        return run_trader("buy", coin, size_str, "--price", price_str)

    # 卖出: 卖 BTC 0.001 70000
    m = re.match(r'^[卖空sell]\s+(\S+)\s+([\d.]+)\s+([\d.]+)$', text, re.IGNORECASE)
    if m:
        coin_input, size_str, price_str = m.groups()
        coin, display = resolve_coin(coin_input)
        return run_trader("sell", coin, size_str, "--price", price_str)

    # 撤单: 撤单 BTC 369556535415
    m = re.match(r'^(撤单|cancel)\s+(\S+)\s+(\d+)$', text, re.IGNORECASE)
    if m:
        _, coin_input, oid = m.groups()
        coin, display = resolve_coin(coin_input)
        return run_trader("cancel", coin, oid)

    return None  # 不认识的命令，不回复


# ---------- 主循环 ----------

def main():
    config = load_config()
    token = config["telegram_bot_token"]
    authorized_chat_id = config["telegram_chat_id"]

    if not token or not authorized_chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置")
        sys.exit(1)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] HL Bot 启动，监听 Telegram 消息...")
    print(f"授权 Chat ID: {authorized_chat_id}")

    offset = None

    # 跳过启动前的旧消息
    result = tg_get_updates(token, timeout=0)
    if result and result.get("ok"):
        updates = result.get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
            print(f"跳过 {len(updates)} 条旧消息")

    while True:
        try:
            result = tg_get_updates(token, offset=offset, timeout=20)
            if not result or not result.get("ok"):
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if not text:
                    continue

                # 安全检查：只响应授权用户
                if chat_id != authorized_chat_id:
                    print(f"[WARN] 未授权的消息来自 chat_id={chat_id}: {text}")
                    continue

                print(f"[{datetime.now().strftime('%H:%M:%S')}] 收到: {text}")

                response = parse_and_execute(text)
                if response:
                    tg_reply(token, chat_id, response)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 已回复")

        except KeyboardInterrupt:
            print("\nBot 已停止")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
