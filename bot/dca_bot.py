"""Hyperliquid Smart DCA — buys each asset once per rolling 24h cycle.

Cycle anchor: Resume timestamp. Bot keeps rolling cycles until paused.
Hours 0–23: buy on intraday dip vs last fill (or first entry).
Hour 23–24, or first run after a missed deadline: market-buy pending assets.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from trade_exec import asset_mid_price, buy_usd_for_asset, execute_spot_buy, get_spot_balance

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

        current = asset_mid_price(asset)
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

    agent_wallet = eth_account.Account.from_key(agent_key)
    exchange = Exchange(agent_wallet, constants.MAINNET_API_URL, account_address=main_wallet)

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

        trigger_label = {"deadline": "DEADLINE", "first_entry": "FIRST", "dip_target": "DIP_TARGET"}[reason]
        drop_str = f" ({drop*100:+.1f}% from ${ref_price:,.2f})" if drop else ""
        print(f"\n  {coin} [{trigger_label}]{drop_str}")

        trade = execute_spot_buy(exchange, asset, buy_usd_for_asset(asset, margin), asset.get("slippage", slippage))
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
