"""Dip-buy checker — runs hourly, buys when price drops X% from last entry."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from trade_exec import asset_mid_price, execute_spot_buy, get_spot_balance

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


def last_entry_price(history, coin):
    """Find the fill price of the most recent entry (DCA or dip) for a coin."""
    for run in reversed(history):
        for t in run.get("trades", []):
            if t["coin"] == coin and t["status"] == "filled" and t.get("price"):
                return float(t["price"])
    return None


def main():
    agent_key = os.environ.get("AGENT_PRIVATE_KEY")
    main_wallet = os.environ.get("MAIN_WALLET_ADDRESS")
    if not agent_key or not main_wallet:
        print("ERROR: AGENT_PRIVATE_KEY and MAIN_WALLET_ADDRESS env vars required")
        sys.exit(1)

    config = load_config()
    if config.get("paused"):
        print("DCA is PAUSED. Toggle via config.json or the pause workflow.")
        return

    margin = config["daily_margin_usd"]
    slippage = config["slippage"]
    assets = config["assets"]
    history = load_history()

    dip_assets = [a for a in assets if a.get("dip_threshold")]
    if not dip_assets:
        print("No assets with dip thresholds configured")
        return

    triggered = []
    print(f"Checking {len(dip_assets)} assets for dip triggers (vs last entry price)...")

    for asset in dip_assets:
        coin = asset["coin"]
        threshold = asset["dip_threshold"]

        ref_price = last_entry_price(history, coin)
        if ref_price is None:
            print(f"  {coin}: no previous entry — skipping")
            continue

        current = asset_mid_price(asset)
        if current == 0:
            print(f"  {coin}: no price")
            continue

        drop = (ref_price - current) / ref_price
        print(f"  {coin}: last entry=${ref_price:,.2f}, now=${current:,.2f}, drop={drop*100:.1f}%, threshold={threshold*100:.0f}%")

        if drop >= threshold:
            print(f"  >>> TRIGGERED (-{drop*100:.1f}% from last buy)")
            triggered.append((asset, ref_price, current, drop))
        else:
            print(f"  --- no trigger")

    if not triggered:
        print("\nNo dip triggers fired. Done.")
        return

    agent_wallet = eth_account.Account.from_key(agent_key)
    exchange = Exchange(agent_wallet, constants.MAINNET_API_URL, account_address=main_wallet)

    usdc_before = get_spot_balance(main_wallet, "USDC")
    now = datetime.now(timezone.utc)
    run = {
        "timestamp": now.isoformat(),
        "type": "dip",
        "usdc_balance_before": round(usdc_before, 2),
        "trades": [],
    }

    print(f"\nExecuting {len(triggered)} dip-buys (${margin} each)...")

    for asset, ref_price, current, drop in triggered:
        coin = asset["coin"]
        trade = execute_spot_buy(exchange, asset, margin, asset.get("slippage", slippage))
        trade["ref_price"] = ref_price
        trade["drop_pct"] = round(drop, 4)
        print(f"  {coin}: {trade['status']} {trade.get('size', '')} @ ${trade.get('price', '')} (was ${ref_price:,.2f}, -{drop*100:.1f}%) {trade.get('error', '')}")
        run["trades"].append(trade)

    usdc_after = get_spot_balance(main_wallet, "USDC")
    run["usdc_balance_after"] = round(usdc_after, 2)

    filled = sum(1 for t in run["trades"] if t["status"] == "filled")
    if filled > 0 or any(t["status"] == "error" for t in run["trades"]):
        history.append(run)
        save_history(history)
    print(f"\nDone: {filled}/{len(run['trades'])} dip-buys filled")


if __name__ == "__main__":
    main()
