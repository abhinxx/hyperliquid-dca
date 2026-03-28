"""Dip-buy checker — runs hourly, buys assets that dipped past their threshold."""

import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import eth_account
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

API = "https://api.hyperliquid.xyz/info"
LOGS_PATH = Path(__file__).parent / "logs" / "history.json"


def load_config():
    with open(Path(__file__).parent / "config.json") as f:
        return json.load(f)


def load_history():
    if LOGS_PATH.exists():
        with open(LOGS_PATH) as f:
            return json.load(f)
    return []


def save_history(history):
    LOGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOGS_PATH, "w") as f:
        json.dump(history, f, indent=2)


def get_mids(dex=""):
    body = {"type": "allMids"}
    if dex:
        body["dex"] = dex
    return requests.post(API, json=body, timeout=10).json()


def get_spot_balance(wallet, coin="USDC"):
    data = requests.post(API, json={"type": "spotClearinghouseState", "user": wallet}, timeout=10).json()
    for b in data.get("balances", []):
        if b["coin"] == coin:
            return float(b["total"])
    return 0.0


def get_rolling_high(coin, dex, lookback_hours):
    """Fetch 1h candles and return the highest price in the lookback window."""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = now - lookback_hours * 3600000
    body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start, "endTime": now}}
    if dex:
        body["req"]["coin"] = coin
    candles = requests.post(API, json=body, timeout=10).json()
    if not candles:
        return None
    return max(float(c["h"]) for c in candles)


def last_dip_buy_time(history, coin):
    """Find the most recent dip-buy timestamp for a given coin."""
    for run in reversed(history):
        if run.get("type") != "dip":
            continue
        for t in run.get("trades", []):
            if t["coin"] == coin and t["status"] == "filled":
                return datetime.fromisoformat(run["timestamp"])
    return None


def swap_usdc_to_usdh(exchange_client, amount):
    result = exchange_client.order(
        name="@230", is_buy=True, sz=round(amount, 2),
        limit_px=1.02, order_type={"limit": {"tif": "Ioc"}},
    )
    status = result.get("status", "unknown")
    if status == "ok":
        for s in result.get("response", {}).get("data", {}).get("statuses", []):
            if "filled" in s:
                return {"status": "filled"}
            if "error" in s:
                return {"status": "error", "error": s["error"]}
    return {"status": "error", "error": f"swap status: {status}"}


def execute_trade(exchange_client, coin, dex, sz_decimals, is_cross, margin, leverage, slippage):
    mids = get_mids(dex)
    price = float(mids.get(coin, 0))
    if price == 0:
        return {"coin": coin, "status": "error", "error": f"no price found"}

    notional = margin * leverage
    size = math.floor((notional / price) * (10 ** sz_decimals)) / (10 ** sz_decimals)
    if size <= 0:
        return {"coin": coin, "status": "error", "error": "size too small"}

    lev_result = exchange_client.update_leverage(leverage, coin, is_cross=is_cross)
    if lev_result.get("status") != "ok":
        exchange_client.update_leverage(leverage, coin, is_cross=not is_cross)

    result = exchange_client.market_open(coin, is_buy=True, sz=size, px=None, slippage=slippage)
    if result.get("status") == "ok":
        for s in result.get("response", {}).get("data", {}).get("statuses", []):
            if "filled" in s:
                f = s["filled"]
                return {"coin": coin, "status": "filled", "size": f["totalSz"], "price": f["avgPx"],
                        "notional": round(float(f["totalSz"]) * float(f["avgPx"]), 2)}
            if "error" in s:
                return {"coin": coin, "status": "error", "error": s["error"]}
    return {"coin": coin, "status": "error", "error": f"order failed: {json.dumps(result)}"}


def main():
    agent_key = os.environ.get("AGENT_PRIVATE_KEY")
    main_wallet = os.environ.get("MAIN_WALLET_ADDRESS")
    if not agent_key or not main_wallet:
        print("ERROR: AGENT_PRIVATE_KEY and MAIN_WALLET_ADDRESS env vars required")
        sys.exit(1)

    config = load_config()
    margin = config["daily_margin_usd"]
    leverage = config["leverage"]
    slippage = config["slippage"]
    lookback = config.get("dip_lookback_hours", 288)
    cooldown = config.get("dip_cooldown_hours", 24)
    assets = config["assets"]
    history = load_history()
    now = datetime.now(timezone.utc)

    dip_assets = [a for a in assets if a.get("dip_threshold")]
    if not dip_assets:
        print("No assets with dip thresholds configured")
        return

    all_dexes = list({a["dex"] for a in dip_assets})
    perp_dexs = [d if d else "" for d in all_dexes]
    if "" not in perp_dexs:
        perp_dexs.insert(0, "")

    triggered = []
    print(f"Checking {len(dip_assets)} assets for dip triggers...")

    for asset in dip_assets:
        coin = asset["coin"]
        dex = asset["dex"]
        threshold = asset["dip_threshold"]

        last_dip = last_dip_buy_time(history, coin)
        if last_dip and (now - last_dip) < timedelta(hours=cooldown):
            hrs_left = cooldown - (now - last_dip).total_seconds() / 3600
            print(f"  {coin}: cooldown ({hrs_left:.1f}h remaining)")
            continue

        mids = get_mids(dex)
        current = float(mids.get(coin, 0))
        if current == 0:
            print(f"  {coin}: no price")
            continue

        high = get_rolling_high(coin, dex, lookback)
        if not high:
            print(f"  {coin}: no candle data")
            continue

        drawdown = (high - current) / high
        print(f"  {coin}: price=${current:,.2f}, 12d-high=${high:,.2f}, drawdown={drawdown*100:.1f}%, threshold={threshold*100:.0f}%")

        if drawdown >= threshold:
            print(f"  >>> TRIGGERED")
            triggered.append(asset)
        else:
            print(f"  --- no trigger")

    if not triggered:
        print("\nNo dip triggers fired. Done.")
        return

    agent_wallet = eth_account.Account.from_key(agent_key)
    exchange = Exchange(agent_wallet, constants.MAINNET_API_URL, account_address=main_wallet, perp_dexs=perp_dexs)

    usdc_before = get_spot_balance(main_wallet, "USDC")
    run = {
        "timestamp": now.isoformat(),
        "type": "dip",
        "usdc_balance_before": round(usdc_before, 2),
        "trades": [],
    }

    print(f"\nExecuting {len(triggered)} dip-buys (${margin} each, {leverage}x)...")

    for asset in triggered:
        coin = asset["coin"]
        collateral = asset.get("collateral")
        swap_pair = asset.get("swap_pair")

        if collateral == "USDH" and swap_pair:
            swap = swap_usdc_to_usdh(exchange, margin)
            if swap["status"] != "filled":
                run["trades"].append({"coin": coin, "status": "error", "error": f"USDH swap failed: {swap.get('error')}"})
                continue

        trade = execute_trade(exchange, coin, asset["dex"], asset["sz_decimals"], asset.get("cross", True), margin, leverage, slippage)
        mids = get_mids(asset["dex"])
        high = get_rolling_high(coin, asset["dex"], lookback)
        trade["drawdown"] = round((high - float(mids.get(coin, 0))) / high, 4) if high else None
        print(f"  {coin}: {trade['status']} {trade.get('size', '')} @ ${trade.get('price', '')} {trade.get('error', '')}")
        run["trades"].append(trade)

    usdc_after = get_spot_balance(main_wallet, "USDC")
    run["usdc_balance_after"] = round(usdc_after, 2)

    history.append(run)
    save_history(history)

    filled = sum(1 for t in run["trades"] if t["status"] == "filled")
    print(f"\nDone: {filled}/{len(run['trades'])} dip-buys filled")


if __name__ == "__main__":
    main()
