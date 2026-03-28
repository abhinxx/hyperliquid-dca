"""Hyperliquid DCA Bot — buys fixed USD amount of each configured asset daily."""

import json
import math
import os
import sys
from datetime import datetime, timezone
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


def swap_usdc_to_usdh(exchange_client, amount):
    """Buy USDH with USDC on spot pair @230."""
    result = exchange_client.order(
        name="@230",
        is_buy=True,
        sz=round(amount, 2),
        limit_px=1.02,
        order_type={"limit": {"tif": "Ioc"}},
    )
    status = result.get("status", "unknown")
    if status == "ok":
        for s in result.get("response", {}).get("data", {}).get("statuses", []):
            if "filled" in s:
                return {"status": "filled", "size": s["filled"]["totalSz"], "price": s["filled"]["avgPx"]}
            if "error" in s:
                return {"status": "error", "error": s["error"]}
    return {"status": "error", "error": f"swap status: {status}"}


def execute_trade(exchange_client, coin, dex, sz_decimals, is_cross, margin, leverage, slippage):
    """Set leverage and place a market buy for one asset."""
    mids = get_mids(dex)
    price = float(mids.get(coin, 0))
    if price == 0:
        return {"coin": coin, "status": "error", "error": f"no price found for {coin}"}

    notional = margin * leverage
    raw_size = notional / price
    size = math.floor(raw_size * (10 ** sz_decimals)) / (10 ** sz_decimals)
    if size <= 0:
        return {"coin": coin, "status": "error", "error": f"size too small: {raw_size}"}

    lev_result = exchange_client.update_leverage(leverage, coin, is_cross=is_cross)
    if lev_result.get("status") != "ok":
        alt_cross = not is_cross
        lev_result = exchange_client.update_leverage(leverage, coin, is_cross=alt_cross)
        if lev_result.get("status") != "ok":
            return {"coin": coin, "status": "error", "error": f"leverage failed: {json.dumps(lev_result)}"}

    result = exchange_client.market_open(coin, is_buy=True, sz=size, px=None, slippage=slippage)
    status = result.get("status", "unknown")
    if status == "ok":
        for s in result.get("response", {}).get("data", {}).get("statuses", []):
            if "filled" in s:
                f = s["filled"]
                return {
                    "coin": coin,
                    "status": "filled",
                    "size": f["totalSz"],
                    "price": f["avgPx"],
                    "notional": round(float(f["totalSz"]) * float(f["avgPx"]), 2),
                }
            if "error" in s:
                return {"coin": coin, "status": "error", "error": s["error"]}
    return {"coin": coin, "status": "error", "error": f"order status: {status}, response: {json.dumps(result)}"}


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
    assets = config["assets"]

    all_dexes = list({a["dex"] for a in assets})
    perp_dexs = [d if d else "" for d in all_dexes]
    if "" not in perp_dexs:
        perp_dexs.insert(0, "")

    agent_wallet = eth_account.Account.from_key(agent_key)
    exchange = Exchange(agent_wallet, constants.MAINNET_API_URL, account_address=main_wallet, perp_dexs=perp_dexs)

    usdc_before = get_spot_balance(main_wallet, "USDC")
    print(f"USDC balance: ${usdc_before:.2f}")
    print(f"Trading {len(assets)} assets, ${margin} margin each, {leverage}x leverage")
    print("=" * 50)

    run = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usdc_balance_before": round(usdc_before, 2),
        "trades": [],
    }

    for asset in assets:
        coin = asset["coin"]
        dex = asset["dex"]
        sz_dec = asset["sz_decimals"]
        is_cross = asset.get("cross", True)
        collateral = asset.get("collateral")
        swap_pair = asset.get("swap_pair")

        print(f"\n--- {coin} ---")

        if collateral == "USDH" and swap_pair:
            print(f"  Swapping ${margin} USDC -> USDH via {swap_pair}")
            swap_result = swap_usdc_to_usdh(exchange, margin)
            print(f"  Swap: {swap_result['status']}")
            if swap_result["status"] != "filled":
                run["trades"].append({"coin": coin, "status": "error", "error": f"USDH swap failed: {swap_result.get('error', 'unknown')}"})
                continue

        trade = execute_trade(exchange, coin, dex, sz_dec, is_cross, margin, leverage, slippage)
        print(f"  {trade['status']}: {trade.get('size', '')} @ ${trade.get('price', '')} {trade.get('error', '')}")
        run["trades"].append(trade)

    usdc_after = get_spot_balance(main_wallet, "USDC")
    run["usdc_balance_after"] = round(usdc_after, 2)

    filled = sum(1 for t in run["trades"] if t["status"] == "filled")
    failed = sum(1 for t in run["trades"] if t["status"] == "error")
    print(f"\n{'=' * 50}")
    print(f"Done: {filled} filled, {failed} failed")
    print(f"USDC: ${usdc_before:.2f} -> ${usdc_after:.2f} (spent ${usdc_before - usdc_after:.2f})")

    history = load_history()
    history.append(run)
    save_history(history)
    print(f"Log saved to {LOGS_PATH}")


if __name__ == "__main__":
    main()
