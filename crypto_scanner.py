import asyncio
import time
import math
from typing import List, Dict, Any, Optional
import httpx

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

CRYPTO_15M_SERIES = [
    {"ticker": "KXBTC15M", "coin": "BTC", "name": "Bitcoin"},
    {"ticker": "KXETH15M", "coin": "ETH", "name": "Ethereum"},
    {"ticker": "KXSOL15M", "coin": "SOL", "name": "Solana"},
    {"ticker": "KXHYPE15M", "coin": "HYPE", "name": "Hyperliquid"},
    {"ticker": "KXDOGE15M", "coin": "DOGE", "name": "Dogecoin"},
    {"ticker": "KXBNB15M", "coin": "BNB", "name": "Binance Coin"},
    {"ticker": "KXXRP15M", "coin": "XRP", "name": "XRP"},
]

LEVERAGED_CRYPTO_SERIES = [
    {"ticker": "KXBTCMINY", "coin": "BTC", "name": "Bitcoin Downside Floor"},
    {"ticker": "KXETHMAXY", "coin": "ETH", "name": "Ethereum Upside Target"},
    {"ticker": "KXDOGEMINMON", "coin": "DOGE", "name": "Doge Monthly Floor"},
    {"ticker": "KXBNBY", "coin": "BNB", "name": "BNB Annual Target"},
]

class CryptoKalshiScanner:
    def __init__(self, taker_fee_cents: float = 0.5):
        self.taker_fee_cents = taker_fee_cents
        self.cache: Dict[str, Any] = {
            "timestamp": 0,
            "elapsed_seconds": 0,
            "spot_prices": {},
            "crypto_15m_trades": [],
            "leveraged_options": [],
            "stats": {}
        }
        self.is_scanning = False

    async def fetch_live_spot_prices(self) -> Dict[str, float]:
        """Fetch real-time spot/perp prices from Hyperliquid."""
        spot = {}
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.post(HYPERLIQUID_INFO_URL, json={"type": "allMids"})
                if resp.status_code == 200:
                    data = resp.json()
                    for coin in ["BTC", "ETH", "SOL", "HYPE", "DOGE", "BNB", "XRP"]:
                        val = data.get(coin)
                        if val is not None:
                            spot[coin] = float(val)
        except Exception as e:
            print(f"Error fetching Hyperliquid spot prices: {e}")
        return spot

    async def scan_15min_crypto_markets(self, client: httpx.AsyncClient, spot_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """Scan Kalshi 15-minute crypto up/down markets and generate simple trade signals."""
        trades = []
        for item in CRYPTO_15M_SERIES:
            series_ticker = item["ticker"]
            coin = item["coin"]
            coin_name = item["name"]
            curr_price = spot_prices.get(coin, 0.0)

            try:
                resp = await client.get(f"{KALSHI_API_BASE}/markets?series_ticker={series_ticker}&status=open")
                if resp.status_code != 200:
                    continue
                markets = resp.json().get("markets", [])
                for m in markets:
                    ticker = m.get("ticker", "")
                    title = m.get("title", "")
                    y_ask = float(m.get("yes_ask_dollars") or 0.0)
                    y_bid = float(m.get("yes_bid_dollars") or 0.0)
                    n_ask = float(m.get("no_ask_dollars") or 0.0)
                    n_bid = float(m.get("no_bid_dollars") or 0.0)
                    close_time = m.get("close_time", "")

                    if y_ask <= 0.0:
                        continue

                    # Probability implied by Kalshi
                    kalshi_yes_pct = round(y_ask * 100.0, 1)
                    kalshi_no_pct = round(n_ask * 100.0, 1) if n_ask > 0 else round((1.0 - y_bid) * 100.0, 1)

                    # Determine trade signal:
                    # If YES ask is > 0.70, the market is heavily trending UP.
                    # Buying YES yields payout when candle finishes green.
                    if y_ask >= 0.65:
                        signal = "BUY YES (BULLISH UP)"
                        signal_color = "emerald"
                        buy_price = y_ask
                        payout_profit = round(1.0 - y_ask, 2)
                        roi_pct = round((payout_profit / y_ask) * 100.0, 1)
                        why = (
                            f"{coin_name} ({coin}) is currently at ${curr_price:,.2f}. "
                            f"The 15-minute candle is strongly green. Kalshi YES is priced at {int(y_ask*100)}¢. "
                            f"If this 15-minute window closes green, buying YES for {int(y_ask*100)}¢ pays out $1.00, "
                            f"locking in a +{int(payout_profit*100)}¢ profit (+{roi_pct}% return in minutes)."
                        )
                        simple_action = f"BUY YES at {int(y_ask*100)}¢ → Pays $1.00 (+{roi_pct}%)"
                    elif y_ask <= 0.35:
                        signal = "BUY NO (BEARISH DOWN)"
                        signal_color = "rose"
                        buy_price = n_ask if n_ask > 0 else (1.0 - y_bid)
                        payout_profit = round(1.0 - buy_price, 2)
                        roi_pct = round((payout_profit / buy_price) * 100.0, 1) if buy_price > 0 else 0
                        why = (
                            f"{coin_name} ({coin}) is currently at ${curr_price:,.2f}. "
                            f"The 15-minute candle is trending red. Kalshi YES has dropped to {int(y_ask*100)}¢. "
                            f"Buying NO for {int(buy_price*100)}¢ bets on {coin} closing red. "
                            f"If it closes red, you collect $1.00 (+{int(payout_profit*100)}¢ profit, +{roi_pct}% return)."
                        )
                        simple_action = f"BUY NO at {int(buy_price*100)}¢ → Pays $1.00 (+{roi_pct}%)"
                    else:
                        signal = "NEUTRAL / 50-50 COIN FLIP"
                        signal_color = "amber"
                        buy_price = y_ask
                        roi_pct = round(((1.0 - y_ask) / y_ask) * 100.0, 1)
                        why = (
                            f"{coin_name} ({coin}) is sitting near the flat line. "
                            f"Kalshi is pricing a 50/50 flip ({int(y_ask*100)}¢ Yes / {int(n_ask*100)}¢ No). "
                            f"Wait for momentum or place a resting limit order inside the spread."
                        )
                        simple_action = f"WAIT or Maker Bid at {int(y_bid*100 + 1)}¢"

                    trades.append({
                        "ticker": ticker,
                        "coin": coin,
                        "coin_name": coin_name,
                        "spot_price": curr_price,
                        "title": title,
                        "signal": signal,
                        "signal_color": signal_color,
                        "simple_action": simple_action,
                        "yes_ask_cents": int(round(y_ask * 100)),
                        "yes_bid_cents": int(round(y_bid * 100)),
                        "no_ask_cents": int(round(n_ask * 100)) if n_ask > 0 else int(round((1.0 - y_bid) * 100)),
                        "roi_pct": roi_pct,
                        "leverage": round(1.0 / buy_price, 1) if buy_price > 0 else 1.0,
                        "close_time": close_time,
                        "market_url": f"https://kalshi.com/markets/{ticker}",
                        "why": why
                    })
            except Exception as e:
                print(f"Error scanning 15m {series_ticker}: {e}")
        return trades

    async def scan_leveraged_crypto_options(self, client: httpx.AsyncClient, spot_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        Scan leveraged options & strike contracts on Kalshi (e.g. BTC floor/target, ETH upside target).
        Computes leverage multiplier (e.g. 10x, 20x) and buffer to current spot price.
        """
        options = []
        for item in LEVERAGED_CRYPTO_SERIES:
            series_ticker = item["ticker"]
            coin = item["coin"]
            coin_name = item["name"]
            curr_price = spot_prices.get(coin, 0.0)

            try:
                resp = await client.get(f"{KALSHI_API_BASE}/markets?series_ticker={series_ticker}&status=open")
                if resp.status_code != 200:
                    continue
                markets = resp.json().get("markets", [])
                for m in markets:
                    ticker = m.get("ticker", "")
                    title = m.get("title", "")
                    y_ask = float(m.get("yes_ask_dollars") or 0.0)
                    y_bid = float(m.get("yes_bid_dollars") or 0.0)
                    vol = float(m.get("volume_24h_fp") or 0.0)
                    exp_time = m.get("expiration_time", "")

                    if y_ask <= 0.0:
                        continue

                    cost_cents = int(round(y_ask * 100))
                    leverage_mult = round(1.0 / y_ask, 1) if y_ask > 0 else 1.0
                    no_cost_cents = int(round((1.0 - y_bid) * 100)) if y_bid > 0 else (100 - cost_cents)

                    # Determine option type & strike
                    # E.g. "Will Bitcoin be below $50000.00"
                    is_downside_floor = "below" in title.lower() or "min" in series_ticker.lower()
                    is_upside_target = "reach above" in title.lower() or "max" in series_ticker.lower()

                    # Extract strike number
                    strike = 0.0
                    import re
                    nums = re.findall(r'\$([0-9,.]+)', title) or re.findall(r'([0-9]+)', ticker.split('-')[-1])
                    if nums:
                        try:
                            strike = float(nums[0].replace(',', ''))
                        except ValueError:
                            pass

                    # Calculate buffer to current spot price
                    buffer_pct = 0.0
                    if curr_price > 0 and strike > 0:
                        if is_downside_floor:
                            buffer_pct = round(((curr_price - strike) / curr_price) * 100.0, 1)
                        else:
                            buffer_pct = round(((strike - curr_price) / curr_price) * 100.0, 1)

                    # Tier 1: Asymmetric Upside Moonshot (Leverage >= 10x)
                    if 0.01 <= y_ask <= 0.10:
                        option_type = "🚀 Asymmetric Moonshot Call"
                        badge_color = "purple"
                        simple_action = f"BUY YES at {cost_cents}¢ ({leverage_mult}x Leverage)"
                        why = (
                            f"High-Leverage Call Option: Costs only {cost_cents}¢ per contract. "
                            f"If triggered, each contract pays out $1.00, generating a massive {leverage_mult}x return "
                            f"(+{int((leverage_mult - 1)*100)}% ROI). Ideal for small asymmetric upside bets."
                        )
                    # Tier 2: Safe Yield / Floor Protection (Betting NO on extreme downside)
                    elif is_downside_floor and buffer_pct >= 25.0:
                        option_type = "🛡️ Safe Yield / Floor Put"
                        badge_color = "emerald"
                        safe_yield_pct = round(((100 - no_cost_cents) / no_cost_cents) * 100.0, 1) if no_cost_cents > 0 else 0
                        simple_action = f"BUY NO at {no_cost_cents}¢ → Pays $1.00 (+{safe_yield_pct}%)"
                        why = (
                            f"{coin} is currently at ${curr_price:,.2f}. This strike is at ${strike:,.2f}, "
                            f"giving a massive {buffer_pct}% safety cushion! "
                            f"For you to lose, {coin} would have to crash by more than {buffer_pct}%. "
                            f"Buying NO at {no_cost_cents}¢ collects an easy +{safe_yield_pct}% return on capital."
                        )
                    # Tier 3: Standard Swing Option (5x to 10x)
                    elif 0.10 < y_ask <= 0.25:
                        option_type = "⚡ Leveraged Swing Option"
                        badge_color = "cyan"
                        simple_action = f"BUY YES at {cost_cents}¢ ({leverage_mult}x Leverage)"
                        why = (
                            f"Moderate Leverage: Trading at {cost_cents}¢ with {leverage_mult}x payout potential. "
                            f"Target strike is ${strike:,.2f} ({buffer_pct}% distance from current spot)."
                        )
                    else:
                        continue

                    options.append({
                        "ticker": ticker,
                        "coin": coin,
                        "coin_name": coin_name,
                        "spot_price": curr_price,
                        "strike": strike,
                        "buffer_pct": buffer_pct,
                        "title": title,
                        "option_type": option_type,
                        "badge_color": badge_color,
                        "cost_cents": cost_cents,
                        "no_cost_cents": no_cost_cents,
                        "leverage_mult": leverage_mult,
                        "simple_action": simple_action,
                        "volume_24h": vol,
                        "exp_time": exp_time,
                        "market_url": f"https://kalshi.com/markets/{ticker}",
                        "why": why
                    })
            except Exception as e:
                print(f"Error scanning option {series_ticker}: {e}")

        # Sort by leverage and buffer
        options.sort(key=lambda x: (x["leverage_mult"], x["buffer_pct"]), reverse=True)
        return options

    async def run_scan(self) -> Dict[str, Any]:
        """Run full crypto-focused scan."""
        self.is_scanning = True
        start_time = time.time()

        spot_prices = await self.fetch_live_spot_prices()

        async with httpx.AsyncClient(timeout=10.0) as client:
            trades_15m = await self.scan_15min_crypto_markets(client, spot_prices)
            options = await self.scan_leveraged_crypto_options(client, spot_prices)

        elapsed = round(time.time() - start_time, 2)
        self.cache = {
            "timestamp": int(time.time()),
            "elapsed_seconds": elapsed,
            "spot_prices": spot_prices,
            "crypto_15m_trades": trades_15m,
            "leveraged_options": options,
            "stats": {
                "total_15m": len(trades_15m),
                "total_options": len(options),
                "max_leverage": max([o["leverage_mult"] for o in options], default=1.0),
                "btc_price": spot_prices.get("BTC", 0.0),
                "eth_price": spot_prices.get("ETH", 0.0),
                "sol_price": spot_prices.get("SOL", 0.0),
                "hype_price": spot_prices.get("HYPE", 0.0),
            }
        }
        self.is_scanning = False
        return self.cache
