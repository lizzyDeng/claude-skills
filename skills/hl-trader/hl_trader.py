#!/usr/bin/env python3
"""
hl_trader.py — Hyperliquid 交易工具（Testnet）

用法：
  python3 hl_trader.py --status                        # 账户概览
  python3 hl_trader.py buy BTC 0.001 --price 60000     # 限价买
  python3 hl_trader.py sell BTC 0.001 --price 70000    # 限价卖
  python3 hl_trader.py orders                          # 查看挂单
  python3 hl_trader.py cancel BTC <order_id>           # 撤单
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils.constants import TESTNET_API_URL, MAINNET_API_URL
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    print("Run: pip3 install hyperliquid-python-sdk")
    sys.exit(1)


# ---------- HTTP via curl (绕过 Python SSL/代理问题) ----------

def curl_post(url, data, timeout=15):
    """用 curl 子进程发 POST 请求，避免 Python SSL 问题"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(data)],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception as e:
        print(f"[WARN] curl_post failed: {e}")
        return None


def notify(config, message):
    """发送 Telegram 通知"""
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or not chat_id:
        return
    curl_post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
    )


# ---------- Monkey-patch SDK 的 HTTP 层 ----------

def _patch_sdk_post(base_url):
    """替换 SDK 内部的 requests.post 为 curl"""
    import hyperliquid.api as api_module

    original_init = api_module.API.__init__

    def patched_init(self, base_url=None):
        original_init(self, base_url)

    def patched_post(self, url_path, payload, headers=None):
        url = f"{self.base_url}{url_path}"
        return curl_post(url, payload)

    api_module.API.post = patched_post


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

    private_key = config.get("HL_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: HL_PRIVATE_KEY not set in .env")
        sys.exit(1)

    use_testnet = config.get("HL_TESTNET", "true").lower() in ("true", "1", "yes")

    return {
        "private_key": private_key,
        "account_address": config.get("HL_ACCOUNT_ADDRESS", ""),
        "testnet": use_testnet,
        "base_url": TESTNET_API_URL if use_testnet else MAINNET_API_URL,
        "telegram_bot_token": config.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": config.get("TELEGRAM_CHAT_ID", ""),
    }


# ---------- API 查询 (直接用 curl) ----------

def api_query(base_url, req_type, extra=None):
    """查询 Hyperliquid Info API"""
    payload = {"type": req_type}
    if extra:
        payload.update(extra)
    return curl_post(f"{base_url}/info", payload)


def api_user_state(base_url, address):
    return api_query(base_url, "clearinghouseState", {"user": address})


def api_open_orders(base_url, address):
    return api_query(base_url, "openOrders", {"user": address})


def api_all_mids(base_url):
    return api_query(base_url, "allMids")


# ---------- Exchange 初始化 ----------

def init_exchange(config):
    """初始化 Exchange（仅用于下单/撤单）"""
    wallet = eth_account.Account.from_key(config["private_key"])
    base_url = config["base_url"]

    _patch_sdk_post(base_url)

    meta = curl_post(f"{base_url}/info", {"type": "meta"})
    if not meta:
        print("ERROR: 无法连接 Hyperliquid API")
        sys.exit(1)

    spot_meta = curl_post(f"{base_url}/info", {"type": "spotMeta"})
    if not spot_meta:
        spot_meta = {"tokens": [], "universe": []}

    account_address = config["account_address"] if config["account_address"] else None

    exchange = Exchange(
        wallet=wallet,
        base_url=base_url,
        account_address=account_address,
        meta=meta,
        spot_meta=spot_meta,
    )

    address = account_address or wallet.address
    return exchange, address


# ---------- 命令 ----------

def cmd_status(config):
    """账户概览"""
    base_url = config["base_url"]
    wallet = eth_account.Account.from_key(config["private_key"])
    address = config["account_address"] or wallet.address
    env = "TESTNET" if config["testnet"] else "MAINNET"

    print(f"{'='*50}")
    print(f"Hyperliquid 账户概览 ({env})")
    print(f"{'='*50}")
    print(f"地址: {address}")

    state = api_user_state(base_url, address)
    if not state:
        print("\n查询失败，请检查网络连接")
        return

    margin = state.get("crossMarginSummary") or state.get("marginSummary", {})
    print(f"\n💰 账户价值: ${float(margin.get('accountValue', 0)):,.2f}")
    print(f"   持仓名义: ${float(margin.get('totalNtlPos', 0)):,.2f}")
    print(f"   可提现:   ${float(state.get('withdrawable', 0)):,.2f}")

    # 持仓
    positions = state.get("assetPositions", [])
    active = [p for p in positions if float(p["position"]["szi"]) != 0]
    if active:
        print(f"\n📊 持仓 ({len(active)}):")
        for p in active:
            pos = p["position"]
            coin = pos["coin"]
            size = float(pos["szi"])
            entry = float(pos["entryPx"])
            upnl = float(pos.get("unrealizedPnl", 0))
            direction = "LONG" if size > 0 else "SHORT"
            print(f"   {coin}: {direction} {abs(size)} @ ${entry:,.2f} (PnL: ${upnl:+,.2f})")
    else:
        print("\n📊 无持仓")

    # 挂单
    orders = api_open_orders(base_url, address)
    if orders:
        print(f"\n📋 挂单 ({len(orders)}):")
        for o in orders:
            side = "BUY" if o["side"] == "B" else "SELL"
            print(f"   {o['coin']}: {side} {o['sz']} @ ${float(o['limitPx']):,.2f} (oid: {o['oid']})")
    else:
        print("\n📋 无挂单")


def cmd_buy(config, coin, size, price):
    """限价买入"""
    exchange, _ = init_exchange(config)
    coin = coin.upper()

    print(f"下单: 买入 {size} {coin} @ ${price:,.2f}")

    try:
        result = exchange.order(
            name=coin, is_buy=True, sz=size, limit_px=price,
            order_type={"limit": {"tif": "Gtc"}},
        )
        _handle_order_result(config, result, "BUY", coin, size, price)
    except Exception as e:
        print(f"❌ 下单失败: {e}")
        notify(config, f"❌ 下单失败: 买入 {size} {coin} @ ${price:,.2f}\n错误: {e}")


def cmd_sell(config, coin, size, price):
    """限价卖出"""
    exchange, _ = init_exchange(config)
    coin = coin.upper()

    print(f"下单: 卖出 {size} {coin} @ ${price:,.2f}")

    try:
        result = exchange.order(
            name=coin, is_buy=False, sz=size, limit_px=price,
            order_type={"limit": {"tif": "Gtc"}},
        )
        _handle_order_result(config, result, "SELL", coin, size, price)
    except Exception as e:
        print(f"❌ 下单失败: {e}")
        notify(config, f"❌ 下单失败: 卖出 {size} {coin} @ ${price:,.2f}\n错误: {e}")


def _handle_order_result(config, result, side, coin, size, price):
    """处理下单结果"""
    status = result.get("status", "")
    response = result.get("response", {})

    if status == "ok":
        data = response.get("data", {})
        statuses = data.get("statuses", [])
        if statuses:
            s = statuses[0]
            if "resting" in s:
                oid = s["resting"]["oid"]
                print(f"✅ 挂单成功 (oid: {oid})")
                notify(config,
                    f"✅ *挂单成功*\n\n"
                    f"{side} {size} {coin} @ ${price:,.2f}\n"
                    f"Order ID: `{oid}`"
                )
            elif "filled" in s:
                fill = s["filled"]
                print(f"✅ 已成交! 成交价: ${float(fill.get('avgPx', price)):,.2f}")
                notify(config,
                    f"🎉 *订单已成交*\n\n"
                    f"{side} {size} {coin} @ ${float(fill.get('avgPx', price)):,.2f}"
                )
            elif "error" in s:
                print(f"❌ 下单被拒: {s['error']}")
                notify(config, f"❌ 下单被拒: {s['error']}")
            else:
                print(f"结果: {json.dumps(s)}")
        else:
            print(f"结果: {json.dumps(response)}")
    else:
        print(f"❌ 请求失败: {json.dumps(result)}")
        notify(config, f"❌ 下单请求失败: {status}")


def cmd_orders(config):
    """查看挂单"""
    base_url = config["base_url"]
    wallet = eth_account.Account.from_key(config["private_key"])
    address = config["account_address"] or wallet.address

    orders = api_open_orders(base_url, address)
    if not orders:
        print("无挂单")
        return

    print(f"当前挂单 ({len(orders)}):\n")
    for o in orders:
        side = "BUY " if o["side"] == "B" else "SELL"
        ts = datetime.fromtimestamp(o["timestamp"] / 1000).strftime("%m-%d %H:%M")
        print(f"  {o['coin']:>5}  {side}  {o['sz']:>10}  @ ${float(o['limitPx']):>12,.2f}  oid: {o['oid']}  ({ts})")


def cmd_cancel(config, coin, oid):
    """撤销挂单"""
    exchange, _ = init_exchange(config)
    coin = coin.upper()

    print(f"撤单: {coin} oid={oid}")

    try:
        result = exchange.cancel(name=coin, oid=oid)
        status = result.get("status", "")
        if status == "ok":
            print(f"✅ 撤单成功")
            notify(config, f"🔄 *撤单成功*\n\n{coin} Order ID: `{oid}`")
        else:
            print(f"❌ 撤单失败: {json.dumps(result)}")
            notify(config, f"❌ 撤单失败: {coin} oid={oid}")
    except Exception as e:
        print(f"❌ 撤单失败: {e}")


# ---------- 入口 ----------

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid 交易工具")
    parser.add_argument("--status", action="store_true", help="账户概览")

    subparsers = parser.add_subparsers(dest="command")

    buy_parser = subparsers.add_parser("buy", help="限价买入")
    buy_parser.add_argument("coin", help="币种 (如 BTC, ETH)")
    buy_parser.add_argument("size", type=float, help="数量")
    buy_parser.add_argument("--price", type=float, required=True, help="限价")

    sell_parser = subparsers.add_parser("sell", help="限价卖出")
    sell_parser.add_argument("coin", help="币种 (如 BTC, ETH)")
    sell_parser.add_argument("size", type=float, help="数量")
    sell_parser.add_argument("--price", type=float, required=True, help="限价")

    subparsers.add_parser("orders", help="查看挂单")

    cancel_parser = subparsers.add_parser("cancel", help="撤销挂单")
    cancel_parser.add_argument("coin", help="币种")
    cancel_parser.add_argument("oid", type=int, help="订单 ID")

    args = parser.parse_args()
    config = load_config()

    if args.status:
        cmd_status(config)
    elif args.command == "buy":
        cmd_buy(config, args.coin, args.size, args.price)
    elif args.command == "sell":
        cmd_sell(config, args.coin, args.size, args.price)
    elif args.command == "orders":
        cmd_orders(config)
    elif args.command == "cancel":
        cmd_cancel(config, args.coin, args.oid)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
