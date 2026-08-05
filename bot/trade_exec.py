"""Shared Hyperliquid spot buy execution."""

import json
import math

import requests

API = "https://api.hyperliquid.xyz/info"


def get_spot_mids():
    return requests.post(API, json={"type": "allMids"}, timeout=10).json()


def get_spot_balance(wallet, coin="USDC"):
    data = requests.post(API, json={"type": "spotClearinghouseState", "user": wallet}, timeout=10).json()
    for b in data.get("balances", []):
        if b["coin"] == coin:
            return float(b["total"])
    return 0.0


def asset_mid_price(asset, mids=None):
    if mids is None:
        mids = get_spot_mids()
    return float(mids.get(asset["spot_pair"], 0))


def execute_spot_buy(exchange_client, asset, usd_amount, slippage):
    coin = asset["coin"]
    pair = asset["spot_pair"]
    sz_decimals = asset["sz_decimals"]
    mids = get_spot_mids()
    price = float(mids.get(pair, 0))
    if price == 0:
        return {"coin": coin, "status": "error", "error": f"no price for {pair}"}

    size = math.floor((usd_amount / price) * (10 ** sz_decimals)) / (10 ** sz_decimals)
    if size <= 0:
        return {"coin": coin, "status": "error", "error": "size too small"}

    result = exchange_client.market_open(pair, is_buy=True, sz=size, px=None, slippage=slippage)
    if result.get("status") == "ok":
        for s in result.get("response", {}).get("data", {}).get("statuses", []):
            if "filled" in s:
                f = s["filled"]
                return {
                    "coin": coin,
                    "spot_pair": pair,
                    "status": "filled",
                    "size": f["totalSz"],
                    "price": f["avgPx"],
                    "notional": round(float(f["totalSz"]) * float(f["avgPx"]), 2),
                }
            if "error" in s:
                return {"coin": coin, "status": "error", "error": s["error"]}
    return {"coin": coin, "status": "error", "error": f"order failed: {json.dumps(result)}"}
