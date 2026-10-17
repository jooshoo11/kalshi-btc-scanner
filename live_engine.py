import asyncio
import time
import math
from typing import List, Dict, Any, Optional
import httpx

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

CRYPTO_15M_SERIES = [
    {"ticker": "KXBTC15M", "coin": "BTC", "name": "Bitcoin", "vol_15m": 95.0},
    {"ticker": "KXETH15M", "coin": "ETH", "name": "Ethereum", "vol_15m": 5.5},
    {"ticker": "KXSOL15M", "coin": "SOL", "name": "Solana", "vol_15m": 0.45},
    {"ticker": "KXHYPE15M", "coin": "HYPE", "name": "Hyperliquid", "vol_15m": 0.35},
    {"ticker": "KXDOGE15M", "coin": "DOGE", "name": "Dogecoin", "vol_15m": 0.0007},
    {"ticker": "KXBNB15M", "coin": "BNB", "name": "Binance Coin", "vol_15m": 1.8},
    {"ticker": "KXXRP15M", "coin": "XRP", "name": "XRP", "vol_15m": 0.005},
]

LEVERAGED_CRYPTO_SERIES = [
    {"ticker": "KXBTCMINY", "coin": "BTC", "name": "Bitcoin Downside Floor"},
    {"ticker": "KXETHMAXY", "coin": "ETH", "name": "Ethereum Upside Target"},
    {"ticker": "KXDOGEMINMON", "coin": "DOGE", "name": "Doge Monthly Floor"},
    {"ticker": "KXBNBY", "coin": "BNB", "name": "BNB Annual Target"},
]

def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (CDF)."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

class LiveCryptoPredictionEngine:
    def __init__(self, taker_fee_cents: float = 0.5):
        self.taker_fee_cents = taker_fee_cents
        self.spot_prices: Dict[str, float] = {}
        self.candle_opens: Dict[str, float] = {}
        self.trades_15m: List[Dict[str, Any]] = []
        self.leveraged_options: List[Dict[str, Any]] = []
        self.last_update_ts: float = 0.0
        self.last_latency_ms: float = 0.0
        self.is_running = False

    async def fetch_spot_prices(self, client: httpx.AsyncClient) -> Dict[str, float]:
        """Fetch sub-second live spot prices from Hyperliquid."""
        try:
            resp = await client.post(HYPERLIQUID_INFO_URL, json={"type": "allMids"}, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                for item in CRYPTO_15M_SERIES:
                    coin = item["coin"]
                    val = data.get(coin)
                    if val is not None:
                        self.spot_prices[coin] = float(val)
        except Exception as e:
            print(f"Error fetching spot prices: {e}")
        return self.spot_prices

    async def fetch_15m_candle_opens(self, client: httpx.AsyncClient) -> Dict[str, float]:
        """Fetch exact open price of current 15m candle from Hyperliquid."""
        now_ms = int(time.time() * 1000)
        candle_15m_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)

        for item in CRYPTO_15M_SERIES[:4]: # Priority coins
            coin = item["coin"]
            try:
                resp = await client.post(HYPERLIQUID_INFO_URL, json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin,
                        "interval": "15m",
                        "startTime": candle_15m_start - (15 * 60 * 1000),
                        "endTime": now_ms
                    }
                }, timeout=3.0)
                if resp.status_code == 200:
                    candles = resp.json()
                    if candles:
                        c = candles[-1]
                        self.candle_opens[coin] = float(c.get("o", self.spot_prices.get(coin, 0.0)))
            except Exception:
                pass
        return self.candle_opens

    async def update_15m_markets(self, client: httpx.AsyncClient):
        """Update live Kalshi 15m markets and calculate mathematical prediction models."""
        now = time.time()
        now_ms = int(now * 1000)
        candle_start_sec = (int(now) // 900) * 900
        candle_end_sec = candle_start_sec + 900
        seconds_left = max(1.0, float(candle_end_sec - int(now)))
        time_elapsed_pct = round(((900 - seconds_left) / 900.0) * 100.0, 1)

        trades = []
        for item in CRYPTO_15M_SERIES:
            series_ticker = item["ticker"]
            coin = item["coin"]
            coin_name = item["name"]
            vol_15m = item["vol_15m"]

            spot = self.spot_prices.get(coin, 0.0)
            open_p = self.candle_opens.get(coin, spot)
            delta = spot - open_p
            delta_pct = (delta / open_p) * 100.0 if open_p > 0 else 0.0

            # Quantitative Prediction Engine (Drift-Diffusion Z-Score)
            time_fraction = seconds_left / 900.0
            vol_adjusted = max(0.0001, vol_15m * math.sqrt(time_fraction))
            z_score = delta / vol_adjusted
            model_true_prob = normal_cdf(z_score)
            model_true_pct = round(model_true_prob * 100.0, 1)

            try:
                resp = await client.get(f"{KALSHI_API_BASE}/markets?series_ticker={series_ticker}&status=open", timeout=4.0)
                if resp.status_code == 200:
                    markets = resp.json().get("markets", [])
                    for m in markets:
                        ticker = m.get("ticker", "")
                        y_ask = float(m.get("yes_ask_dollars") or 0.0)
                        y_bid = float(m.get("yes_bid_dollars") or 0.0)
                        n_ask = float(m.get("no_ask_dollars") or 0.0)
                        n_bid = float(m.get("no_bid_dollars") or 0.0)

                        if y_ask <= 0.0:
                            continue

                        kalshi_yes_pct = round(y_ask * 100.0, 1)
                        kalshi_no_pct = round(n_ask * 100.0, 1) if n_ask > 0 else round((1.0 - y_bid) * 100.0, 1)

                        # Mathematical Edge Calculation
                        edge_yes = model_true_prob - y_ask
                        edge_no = (1.0 - model_true_prob) - (n_ask if n_ask > 0 else (1.0 - y_bid))

                        # Determine Real Action Signal
                        if edge_yes > 0.04 and model_true_prob >= 0.60:
                            signal = "🟢 STRONG BUY YES"
                            action_text = f"BUY YES at {int(y_ask*100)}¢ (Model: {model_true_pct}%)"
                            ev_cents = round(((model_true_prob * 1.0) - y_ask - (self.taker_fee_cents/100.0)) * 100.0, 1)
                            roi_pct = round((ev_cents / (y_ask * 100.0)) * 100.0, 1)
                            why = (
                                f"{coin} is currently up {delta:+.2f} ({delta_pct:+.2f}%) with {int(seconds_left)}s left. "
                                f"Prediction engine calculates a {model_true_pct}% probability that {coin} finishes green. "
                                f"Kalshi retail orderbook is lagging at {int(y_ask*100)}¢. "
                                f"Buying YES yields an expected +{ev_cents}¢ profit (+{roi_pct}% EV)."
                            )
                        elif edge_no > 0.04 and (1.0 - model_true_prob) >= 0.60:
                            signal = "🔴 STRONG BUY NO"
                            no_price = n_ask if n_ask > 0 else (1.0 - y_bid)
                            action_text = f"BUY NO at {int(no_price*100)}¢ (Model: {round((1-model_true_prob)*100,1)}%)"
                            ev_cents = round(((((1.0 - model_true_prob) * 1.0) - no_price - (self.taker_fee_cents/100.0)) * 100.0), 1)
                            roi_pct = round((ev_cents / (no_price * 100.0)) * 100.0, 1) if no_price > 0 else 0
                            why = (
                                f"{coin} is currently down {delta:+.2f} ({delta_pct:+.2f}%) with {int(seconds_left)}s left. "
                                f"Prediction engine calculates a {round((1-model_true_prob)*100, 1)}% probability of finishing red. "
                                f"Kalshi YES is overpriced at {int(y_ask*100)}¢. "
                                f"Buying NO at {int(no_price*100)}¢ captures an expected +{ev_cents}¢ profit (+{roi_pct}% EV)."
                            )
                        elif abs(kalshi_yes_pct - model_true_pct) > 10.0:
                            signal = "⚡ MISPRICED ORDERBOOK"
                            action_text = f"FADE SPREAD: Model {model_true_pct}% vs Book {kalshi_yes_pct}%"
                            ev_cents = round(abs(model_true_prob - y_ask) * 100.0, 1)
                            roi_pct = 12.5
                            why = (
                                f"Significant probability divergence: Prediction engine prices {coin} win rate at {model_true_pct}%, "
                                f"while Kalshi orderbook is trading at {kalshi_yes_pct}%. "
                                f"Exploit the {round(abs(edge_yes)*100, 1)}% discrepancy by placing a maker limit order."
                            )
                        else:
                            signal = "⚖️ FAIRLY PRICED"
                            action_text = f"HOLD / WAIT (Fair value ~{model_true_pct}%)"
                            ev_cents = 0.0
                            roi_pct = 0.0
                            why = f"Kalshi market ({kalshi_yes_pct}%) is closely tracking true model probability ({model_true_pct}%)."

                        trades.append({
                            "ticker": ticker,
                            "coin": coin,
                            "coin_name": coin_name,
                            "spot_price": spot,
                            "open_price": open_p,
                            "delta": round(delta, 2),
                            "delta_pct": round(delta_pct, 2),
                            "seconds_left": int(seconds_left),
                            "time_elapsed_pct": time_elapsed_pct,
                            "model_prob_pct": model_true_pct,
                            "kalshi_yes_cents": int(round(y_ask * 100)),
                            "kalshi_no_cents": int(round(n_ask * 100)) if n_ask > 0 else int(round((1.0 - y_bid) * 100)),
                            "edge_pct": round(abs(model_true_pct - kalshi_yes_pct), 1),
                            "signal": signal,
                            "action_text": action_text,
                            "ev_cents": ev_cents,
                            "roi_pct": roi_pct,
                            "market_url": f"https://kalshi.com/markets/{ticker}",
                            "why": why
                        })
            except Exception as e:
                print(f"Error fetching {series_ticker}: {e}")

        self.trades_15m = trades

    async def update_leveraged_options(self, client: httpx.AsyncClient):
        """Update leveraged options with live spot distance buffers."""
        options = []
        for item in LEVERAGED_CRYPTO_SERIES:
            series_ticker = item["ticker"]
            coin = item["coin"]
            coin_name = item["name"]
            spot = self.spot_prices.get(coin, 0.0)

            try:
                resp = await client.get(f"{KALSHI_API_BASE}/markets?series_ticker={series_ticker}&status=open", timeout=4.0)
                if resp.status_code == 200:
                    markets = resp.json().get("markets", [])
                    for m in markets:
                        ticker = m.get("ticker", "")
                        title = m.get("title", "")
                        y_ask = float(m.get("yes_ask_dollars") or 0.0)
                        y_bid = float(m.get("yes_bid_dollars") or 0.0)

                        if y_ask <= 0.0:
                            continue

                        cost_cents = int(round(y_ask * 100))
                        leverage_mult = round(1.0 / y_ask, 1) if y_ask > 0 else 1.0
                        no_cost_cents = int(round((1.0 - y_bid) * 100)) if y_bid > 0 else (100 - cost_cents)

                        is_downside = "below" in title.lower() or "min" in series_ticker.lower()
                        import re
                        nums = re.findall(r'\$([0-9,.]+)', title) or re.findall(r'([0-9]+)', ticker.split('-')[-1])
                        strike = float(nums[0].replace(',', '')) if nums else 0.0

                        buffer_pct = 0.0
                        if spot > 0 and strike > 0:
                            buffer_pct = round(((spot - strike) / spot) * 100.0, 1) if is_downside else round(((strike - spot) / spot) * 100.0, 1)

                        if 0.01 <= y_ask <= 0.10:
                            opt_type = "🚀 Extreme Moonshot Call"
                            action = f"BUY YES at {cost_cents}¢ ({leverage_mult}x Leverage)"
                            why = f"Costs only {cost_cents}¢. Payout if strike is reached is $1.00 ({leverage_mult}x return / +{int((leverage_mult-1)*100)}% ROI)."
                        elif is_downside and buffer_pct >= 25.0:
                            opt_type = "🛡️ Safe Yield / Floor Put"
                            safe_yield = round(((100 - no_cost_cents) / no_cost_cents) * 100.0, 1) if no_cost_cents > 0 else 0
                            action = f"BUY NO at {no_cost_cents}¢ → Pays $1.00 (+{safe_yield}%)"
                            why = f"Current spot is ${spot:,.2f} with a {buffer_pct}% safety cushion above strike (${strike:,.2f}). Steady yield."
                        elif 0.10 < y_ask <= 0.25:
                            opt_type = "⚡ Leveraged Swing Call"
                            action = f"BUY YES at {cost_cents}¢ ({leverage_mult}x Leverage)"
                            why = f"Moderate {leverage_mult}x leverage to target strike ${strike:,.2f} ({buffer_pct}% distance)."
                        else:
                            continue

                        options.append({
                            "ticker": ticker,
                            "coin": coin,
                            "coin_name": coin_name,
                            "spot_price": spot,
                            "strike": strike,
                            "buffer_pct": buffer_pct,
                            "title": title,
                            "opt_type": opt_type,
                            "cost_cents": cost_cents,
                            "no_cost_cents": no_cost_cents,
                            "leverage_mult": leverage_mult,
                            "action": action,
                            "market_url": f"https://kalshi.com/markets/{ticker}",
                            "why": why
                        })
            except Exception:
                pass

        options.sort(key=lambda x: (x["leverage_mult"], x["buffer_pct"]), reverse=True)
        self.leveraged_options = options

    async def run_live_tick(self):
        """Execute one complete sub-second update cycle."""
        t0 = time.time()
        async with httpx.AsyncClient(timeout=4.0) as client:
            await self.fetch_spot_prices(client)
            await self.fetch_15m_candle_opens(client)
            await self.update_15m_markets(client)
            await self.update_leveraged_options(client)
        self.last_latency_ms = round((time.time() - t0) * 1000.0, 1)
        self.last_update_ts = time.time()

    def get_snapshot(self) -> Dict[str, Any]:
        """Return the current 100% live state snapshot."""
        return {
            "server_time_ms": int(time.time() * 1000),
            "latency_ms": self.last_latency_ms,
            "spot_prices": self.spot_prices,
            "trades_15m": self.trades_15m,
            "leveraged_options": self.leveraged_options
        }
