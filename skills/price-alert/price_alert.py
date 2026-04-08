#!/usr/bin/env python3
"""
price_alert.py — 黄金 (XAU/USD) + BTC 价格波动监控

数据源：
  - BTC: Binance 公开 API（免费、无需 key）
  - XAU: Twelve Data API（免费 key）

通知：Telegram Bot

用法：
  python3 price_alert.py --check      # 单次检查，输出当前价格
  python3 price_alert.py --status     # 查看历史价格和波动
  python3 price_alert.py              # 正常运行：获取价格 → 检测波动 → 触发告警
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip3 install requests")
    sys.exit(1)


# ---------- 配置 ----------

def load_config():
    """从 .env 文件加载配置"""
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
        "twelve_data_api_key": config.get("TWELVE_DATA_API_KEY", ""),
        "telegram_bot_token": config.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": config.get("TELEGRAM_CHAT_ID", ""),
        "xau_threshold_pct": float(config.get("XAU_THRESHOLD_PCT", "2.0")),
        "btc_threshold_pct": float(config.get("BTC_THRESHOLD_PCT", "5.0")),
        "window_minutes": int(config.get("WINDOW_MINUTES", "1440")),
        "alert_step_pct": float(config.get("ALERT_STEP_PCT", str(DEFAULT_ALERT_STEP_PCT))),
    }


# ---------- 价格历史 ----------

HISTORY_FILE = Path(__file__).parent / ".price_history.json"
ALERT_STATE_FILE = Path(__file__).parent / ".alert_state.json"

# 告警升级步长默认值（百分点）— 波动率比上次告警高出这么多才再次告警
DEFAULT_ALERT_STEP_PCT = 3.0


def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {"btc": [], "xau": []}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def add_price(history, asset, price, ts=None):
    """添加价格记录，保留最近 25 小时的数据"""
    if ts is None:
        ts = datetime.utcnow().isoformat()
    history.setdefault(asset, []).append({"price": price, "ts": ts})

    # 清理超过 72 小时的旧数据（覆盖 Mac 睡眠导致的数据断层）
    cutoff = (datetime.utcnow() - timedelta(hours=72)).isoformat()
    history[asset] = [r for r in history[asset] if r["ts"] >= cutoff]


# ---------- 价格获取 ----------

def fetch_btc_price():
    """从 Binance 获取 BTC/USDT 价格"""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        print(f"[WARN] 获取 BTC 价格失败: {e}")
        return None


def fetch_xau_price(api_key):
    """从 Twelve Data 获取 XAU/USD 价格"""
    if not api_key:
        print("[WARN] 未配置 TWELVE_DATA_API_KEY，跳过 XAU")
        return None
    try:
        resp = requests.get(
            "https://api.twelvedata.com/price",
            params={"symbol": "XAU/USD", "apikey": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "price" in data:
            return float(data["price"])
        print(f"[WARN] Twelve Data 返回异常: {data}")
        return None
    except Exception as e:
        print(f"[WARN] 获取 XAU 价格失败: {e}")
        return None


# ---------- 波动检测 ----------

def check_volatility(history, asset, threshold_pct, window_minutes):
    """
    检查指定资产在时间窗口内的波动幅度。
    比较窗口内最高价与最低价，检测极值波动。
    返回 (triggered, change_pct, low_price, high_price) 或 (False, None, None, None)
    """
    records = history.get(asset, [])
    if len(records) < 2:
        return False, None, None, None

    now = datetime.utcnow()
    window_start = (now - timedelta(minutes=window_minutes)).isoformat()

    # 找到窗口内的记录
    window_records = [r for r in records if r["ts"] >= window_start]
    if len(window_records) < 2:
        return False, None, None, None

    prices = [r["price"] for r in window_records]
    high_price = max(prices)
    low_price = min(prices)

    if low_price == 0:
        return False, None, None, None

    change_pct = ((high_price - low_price) / low_price) * 100

    # 判断方向：最新价格更接近高点还是低点
    newest_price = prices[-1]
    if newest_price >= (high_price + low_price) / 2:
        # 当前偏高，视为上涨
        signed_change = change_pct
    else:
        # 当前偏低，视为下跌
        signed_change = -change_pct

    if change_pct >= threshold_pct:
        return True, signed_change, low_price, high_price

    return False, signed_change, low_price, high_price


# ---------- 通知 ----------

def send_telegram(token, chat_id, message):
    """发送 Telegram 消息"""
    if not token or not chat_id:
        print("[WARN] Telegram 未配置，跳过通知")
        print(f"[ALERT] {message}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[WARN] Telegram 发送失败: {e}")
        print(f"[ALERT] {message}")
        return False


def format_alert(asset, change_pct, old_price, new_price, window_minutes):
    """格式化告警消息"""
    direction = "📈 上涨" if change_pct > 0 else "📉 下跌"
    asset_name = "🥇 黄金 XAU/USD" if asset == "xau" else "₿ BTC/USDT"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # 价格格式化
    if asset == "xau":
        old_str = f"${old_price:,.2f}"
        new_str = f"${new_price:,.2f}"
    else:
        old_str = f"${old_price:,.0f}"
        new_str = f"${new_price:,.0f}"

    return (
        f"⚠️ *价格波动告警*\n\n"
        f"{asset_name}\n"
        f"{direction} *{abs(change_pct):.2f}%*\n\n"
        f"价格: {old_str} → {new_str}\n"
        f"时间窗口: {window_minutes} 分钟\n"
        f"时间: {now}"
    )


# ---------- 命令 ----------

def cmd_check(config):
    """单次检查，输出当前价格"""
    print("获取当前价格...\n")

    btc = fetch_btc_price()
    xau = fetch_xau_price(config["twelve_data_api_key"])

    if btc is not None:
        print(f"₿  BTC/USDT:  ${btc:,.2f}")
    else:
        print("₿  BTC/USDT:  获取失败")

    if xau is not None:
        print(f"🥇 XAU/USD:   ${xau:,.2f}")
    else:
        print("🥇 XAU/USD:   获取失败")

    # 写入历史
    history = load_history()
    now = datetime.utcnow().isoformat()
    if btc is not None:
        add_price(history, "btc", btc, now)
    if xau is not None:
        add_price(history, "xau", xau, now)
    save_history(history)

    print(f"\n时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")


def cmd_status(config):
    """查看历史价格和波动"""
    history = load_history()
    window = config["window_minutes"]

    for asset, name in [("btc", "BTC/USDT"), ("xau", "XAU/USD")]:
        records = history.get(asset, [])
        print(f"\n{'='*40}")
        print(f"{name}")
        print(f"{'='*40}")

        if not records:
            print("  无历史数据")
            continue

        latest = records[-1]
        print(f"  最新价格: ${latest['price']:,.2f}")
        print(f"  最新时间: {latest['ts']}")
        print(f"  历史记录数: {len(records)}")

        _, change, old_p, new_p = check_volatility(
            history, asset,
            config[f"{asset}_threshold_pct"],
            window,
        )
        if change is not None:
            threshold = config[f"{asset}_threshold_pct"]
            status = "⚠️ 超阈值" if abs(change) >= threshold else "✅ 正常"
            print(f"  {window}分钟波动: {change:+.2f}% (阈值: ±{threshold}%) {status}")


def load_alert_state():
    """加载告警冷却状态"""
    if ALERT_STATE_FILE.exists():
        try:
            return json.loads(ALERT_STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_alert_state(state):
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))


def should_alert(alert_state, asset, current_pct, step_pct):
    """
    判断是否应该告警。逻辑：
    - 首次超阈值：告警，记录当前波动率
    - 后续：只有波动率比上次告警高出 step_pct 才再次告警
    - 波动回落到阈值以下时：自动重置（由调用方处理）
    """
    last_pct = alert_state.get(f"{asset}_last_pct", 0)
    if last_pct == 0:
        return True  # 首次告警
    return abs(current_pct) >= last_pct + step_pct


def cmd_run(config):
    """正常运行：获取价格 → 检测波动 → 触发告警（含冷却）"""
    btc = fetch_btc_price()
    xau = fetch_xau_price(config["twelve_data_api_key"])

    history = load_history()
    now = datetime.utcnow().isoformat()

    if btc is not None:
        add_price(history, "btc", btc, now)
    if xau is not None:
        add_price(history, "xau", xau, now)

    save_history(history)

    # 检测波动
    window = config["window_minutes"]
    alerts = []
    alert_state = load_alert_state()

    step_pct = config["alert_step_pct"]
    state_changed = False

    for asset, threshold_key in [("btc", "btc_threshold_pct"), ("xau", "xau_threshold_pct")]:
        triggered, change, old_p, new_p = check_volatility(
            history, asset, config[threshold_key], window
        )
        if triggered and should_alert(alert_state, asset, change, step_pct):
            msg = format_alert(asset, change, old_p, new_p, window)
            alerts.append((asset, msg, change))
        elif not triggered and alert_state.get(f"{asset}_last_pct", 0) > 0:
            # 波动回落到阈值以下，重置状态
            alert_state[f"{asset}_last_pct"] = 0
            state_changed = True

    # 发送告警
    for asset, msg, change in alerts:
        if send_telegram(config["telegram_bot_token"], config["telegram_chat_id"], msg):
            alert_state[f"{asset}_last_pct"] = abs(change)
            state_changed = True

    if state_changed or alerts:
        save_alert_state(alert_state)

    # 静默日志
    ts = datetime.utcnow().strftime("%H:%M:%S")
    btc_str = f"${btc:,.0f}" if btc else "N/A"
    xau_str = f"${xau:,.2f}" if xau else "N/A"
    alert_str = f" | {len(alerts)} alert(s) sent" if alerts else ""
    print(f"[{ts}] BTC={btc_str} XAU={xau_str}{alert_str}")


def cmd_test_telegram(config):
    """发送测试消息验证 Telegram 配置"""
    msg = "✅ *Price Alert 测试*\n\nTelegram 通知配置正常！"
    ok = send_telegram(config["telegram_bot_token"], config["telegram_chat_id"], msg)
    if ok:
        print("Telegram 测试消息发送成功")
    else:
        print("Telegram 测试消息发送失败，请检查配置")


# ---------- 入口 ----------

def main():
    parser = argparse.ArgumentParser(description="黄金 + BTC 价格波动监控")
    parser.add_argument("--check", action="store_true", help="单次检查当前价格")
    parser.add_argument("--status", action="store_true", help="查看历史价格和波动状态")
    parser.add_argument("--test-telegram", action="store_true", help="发送 Telegram 测试消息")
    args = parser.parse_args()

    config = load_config()

    if args.check:
        cmd_check(config)
    elif args.status:
        cmd_status(config)
    elif args.test_telegram:
        cmd_test_telegram(config)
    else:
        cmd_run(config)


if __name__ == "__main__":
    main()
