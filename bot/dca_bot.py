"""Hyperliquid Smart DCA — buys each asset once per rolling 24h cycle.

Cycle anchor: Resume timestamp. Bot keeps rolling cycles until paused.
Hours 0–23: buy on intraday dip vs last fill (or first entry).
Hour 23–24, or first run after a missed deadline: market-buy pending assets.
"""

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
CYCLE_DURATION = timedelta(hours=24)
DEADLINE_OFFSET = timedelta(hours=23)


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
    sz = round(max(amount + 1, 11), 2)
    result = exchange_client.order(
        name="@230", is_buy=True, sz=sz,
        limit_px=1.02, order_type={"limit": {"tif": "Ioc"}},
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
    mids = get_mids(dex)
    price = float(mids.get(coin, 0))
    if price == 0:
        return {"coin": coin, "status": "error", "error": f"no price found for {coin}"}

    notional = margin * leverage
    size = math.floor((notional / price) * (10 ** sz_decimals)) / (10 ** sz_decimals)
    if size <= 0:
        return {"coin": coin, "status": "error", "error": f"size too small"}

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


def last_entry_price(history, coin):
    """Most recent DCA fill price for a coin.

    Dip buys are opportunistic extras; they must not reset the Smart DCA
    reference or suppress the regular DCA cycle.
    """
    for run in reversed(history):
        if run.get("type") != "dca":
            continue
        for t in run.get("trades", []):
            if t["coin"] == coin and t["status"] == "filled" and t.get("price"):
                return float(t["price"])
    return None


def as_utc(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_session_start(config):
    raw = config.get("session_started_at")
    if not raw:
        return None
    return as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))


def cycle_deadline_at(cycle_start):
    return cycle_start + DEADLINE_OFFSET


def cycle_ends_at(cycle_start):
    return cycle_start + CYCLE_DURATION


def current_cycle_start(anchor, now):
    if now < anchor:
        return anchor
    cycles_elapsed = int((now - anchor).total_seconds() // CYCLE_DURATION.total_seconds())
    return anchor + cycles_elapsed * CYCLE_DURATION


def same_cycle(left, right):
    return as_utc(left) == as_utc(right)


def run_belongs_to_cycle(run, cycle_start):
    run_cycle_start = run.get("cycle_start")
    cycle_end = cycle_ends_at(cycle_start)
    if run_cycle_start:
        return same_cycle(datetime.fromisoformat(run_cycle_start), cycle_start)
    run_time = as_utc(datetime.fromisoformat(run["timestamp"]))
    return cycle_start <= run_time < cycle_end


def already_bought_cycle(history, coin, cycle_start):
    """DCA fill assigned to this cycle.

    New runs carry cycle_start explicitly so catch-up buys after a missed deadline
    do not accidentally count toward the next cycle. Legacy runs fall back to the
    timestamp window.
    """
    for run in history:
        if run.get("type") != "dca":
            continue
        if not run_belongs_to_cycle(run, cycle_start):
            continue
        for t in run.get("trades", []):
            if t["coin"] == coin and t["status"] == "filled":
                return True
    return False


def pending_assets(history, assets, cycle_start):
    return [a for a in assets if not already_bought_cycle(history, a["coin"], cycle_start)]


def deadline_attempted_cycle(history, cycle_start):
    for run in history:
        if run.get("type") != "dca":
            continue
        if not run_belongs_to_cycle(run, cycle_start):
            continue
        if run.get("deadline_catch_up"):
            return True
        for t in run.get("trades", []):
            if t.get("trigger") == "DEADLINE":
                return True
    return False


def select_cycle_for_run(history, assets, anchor, now):
    """Return (cycle_start, is_deadline, is_catch_up).

    If GitHub skips the final hour, the next DCA run catches up the most recent
    completed cycle before moving on. It does not backfill multiple missed days.
    """
    cycle_start = current_cycle_start(anchor, now)

    if cycle_start > anchor:
        previous_cycle = cycle_start - CYCLE_DURATION
        if (
            now >= cycle_ends_at(previous_cycle)
            and pending_assets(history, assets, previous_cycle)
            and not deadline_attempted_cycle(history, previous_cycle)
        ):
            return previous_cycle, True, True

    return cycle_start, now >= cycle_deadline_at(cycle_start), False


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
    leverage = config["leverage"]
    slippage = config["slippage"]
    assets = config["assets"]
    history = load_history()
    now = datetime.now(timezone.utc)

    session_start = parse_session_start(config)
    if session_start is None:
        print("No session_started_at — Resume via toggle-pause (no trades until then)")
        return

    cycle_start, is_deadline, is_catch_up = select_cycle_for_run(history, assets, session_start, now)
    cycle_end = cycle_ends_at(cycle_start)
    dl_at = cycle_deadline_at(cycle_start)
    if is_catch_up:
        dl_tag = f"CATCH-UP (missed {dl_at.strftime('%Y-%m-%d %H:%M')} UTC)"
    else:
        dl_tag = "YES" if is_deadline else f"no (from {dl_at.strftime('%Y-%m-%d %H:%M')} UTC)"

    print(f"Time: {now.strftime('%H:%M UTC')} | Cycle: {cycle_start.strftime('%Y-%m-%d %H:%M')} -> {cycle_end.strftime('%Y-%m-%d %H:%M')} | Force-buy: {dl_tag}")

    to_buy = []
    skipped = []

    for asset in assets:
        coin = asset["coin"]

        if already_bought_cycle(history, coin, cycle_start):
            skipped.append(coin)
            continue

        if is_deadline:
            to_buy.append((asset, "deadline", None, None))
            continue

        ref_price = last_entry_price(history, coin)
        if ref_price is None:
            to_buy.append((asset, "first_entry", None, None))
            continue

        mids = get_mids(asset["dex"])
        current = float(mids.get(coin, 0))
        if current == 0:
            continue

        drop = (ref_price - current) / ref_price
        intraday_threshold = asset.get("intraday_drop", 0.03)

        if drop >= intraday_threshold:
            to_buy.append((asset, "dip_target", ref_price, drop))
        else:
            print(f"  {coin}: ${current:,.2f} (ref=${ref_price:,.2f}, drop={drop*100:+.1f}%, need {intraday_threshold*100:.1f}%) — waiting")

    if skipped:
        print(f"Already bought this cycle: {', '.join(skipped)}")

    if not to_buy:
        print("Nothing to buy this hour.")
        return

    all_dexes = list({a["dex"] for a, _, _, _ in to_buy})
    perp_dexs = [d if d else "" for d in all_dexes]
    if "" not in perp_dexs:
        perp_dexs.insert(0, "")

    agent_wallet = eth_account.Account.from_key(agent_key)
    exchange = Exchange(agent_wallet, constants.MAINNET_API_URL, account_address=main_wallet, perp_dexs=perp_dexs)

    usdc_before = get_spot_balance(main_wallet, "USDC")
    run = {
        "timestamp": now.isoformat(),
        "type": "dca",
        "cycle_start": cycle_start.isoformat(),
        "cycle_end": cycle_end.isoformat(),
        "deadline_catch_up": is_catch_up,
        "usdc_balance_before": round(usdc_before, 2),
        "trades": [],
    }

    for asset, reason, ref_price, drop in to_buy:
        coin = asset["coin"]
        collateral = asset.get("collateral")
        swap_pair = asset.get("swap_pair")

        trigger_label = {"deadline": "DEADLINE", "first_entry": "FIRST", "dip_target": "DIP_TARGET"}[reason]
        drop_str = f" ({drop*100:+.1f}% from ${ref_price:,.2f})" if drop else ""
        print(f"\n  {coin} [{trigger_label}]{drop_str}")

        if collateral == "USDH" and swap_pair:
            swap = swap_usdc_to_usdh(exchange, margin)
            if swap["status"] != "filled":
                run["trades"].append({"coin": coin, "status": "error", "error": f"USDH swap failed: {swap.get('error', '')}"})
                continue

        trade = execute_trade(exchange, coin, asset["dex"], asset["sz_decimals"], asset.get("cross", True), margin, leverage, asset.get("slippage", slippage))
        trade["trigger"] = trigger_label
        if ref_price:
            trade["ref_price"] = ref_price
        if drop:
            trade["drop_pct"] = round(drop, 4)
        print(f"    {trade['status']}: {trade.get('size', '')} @ ${trade.get('price', '')} {trade.get('error', '')}")
        run["trades"].append(trade)

    usdc_after = get_spot_balance(main_wallet, "USDC")
    run["usdc_balance_after"] = round(usdc_after, 2)

    filled = sum(1 for t in run["trades"] if t["status"] == "filled")
    if filled > 0 or any(t["status"] == "error" for t in run["trades"]):
        history.append(run)
        save_history(history)
        print(f"\n{'='*50}")
        print(f"Bought {filled}/{len(run['trades'])}. USDC: ${usdc_before:.2f} -> ${usdc_after:.2f}")
    else:
        print("\nNo trades executed.")


if __name__ == "__main__":
    main()
