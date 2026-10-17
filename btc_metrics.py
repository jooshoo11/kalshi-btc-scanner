import asyncio
import time
import math
from typing import Dict, Any, List, Optional
import numpy as np
import httpx

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=15"

class BitcoinMetricsProvider:
    """
    Comprehensive live Bitcoin data aggregator.
    Pulls spot prices, multi-window realized volatilities, technical indicators,
    and derivatives microstructure from Hyperliquid, Coinbase, and Kraken.
    """
    def __init__(self):
        self.spot_price: float = 81500.0
        self.coinbase_spot: float = 81500.0
        self.kraken_spot: float = 81500.0
        self.hyperliquid_spot: float = 81500.0
        
        # 24h metrics
        self.high_24h: float = 81800.0
        self.low_24h: float = 81000.0
        self.change_24h_pct: float = 0.0
        self.volume_24h_btc: float = 0.0
        self.vwap_24h: float = 81400.0

        # Multi-window Volatilities (Annualized)
        self.vol_15m_ann: float = 0.50
        self.vol_1h_ann: float = 0.48
        self.vol_24h_ann: float = 0.45
        self.vol_7d_ann: float = 0.52
        self.vol_parkinson_24h: float = 0.46
        self.vol_garman_klass_24h: float = 0.45
        self.vol_15m_dollar: float = 95.0 # Typical 15-minute dollar standard deviation

        # Technical Indicators
        self.rsi_14: float = 50.0
        self.bb_upper: float = 82000.0
        self.bb_mid: float = 81500.0
        self.bb_lower: float = 81000.0
        self.bb_pct_b: float = 0.50
        self.bb_bandwidth: float = 1.2
        self.macd_line: float = 0.0
        self.macd_signal: float = 0.0
        self.macd_hist: float = 0.0
        self.atr_14: float = 120.0
        self.ema_9: float = 81500.0
        self.ema_21: float = 81500.0
        self.sma_50: float = 81500.0
        self.sma_200: float = 81500.0

        # Derivatives & Microstructure
        self.funding_rate_8h: float = 0.0001
        self.funding_rate_ann: float = 0.1095
        self.open_interest_btc: float = 40000.0
        self.bid_ask_spread: float = 0.50

        # 15m Candle open tracking
        self.current_15m_candle_open: float = 81500.0
        self.current_15m_candle_high: float = 81500.0
        self.current_15m_candle_low: float = 81500.0
        self.current_15m_start_ts: int = 0

        # Status
        self.last_update_ts: float = 0.0
        self.latency_ms: float = 0.0

    async def update_subsecond_spot(self, client: httpx.AsyncClient):
        """Update live spot price from Hyperliquid and Coinbase."""
        t0 = time.time()
        try:
            # 1. Hyperliquid mid
            resp_hl = await client.post(HYPERLIQUID_INFO_URL, json={"type": "allMids"}, timeout=2.5)
            if resp_hl.status_code == 200:
                mids = resp_hl.json()
                if "BTC" in mids:
                    self.hyperliquid_spot = float(mids["BTC"])
                    self.spot_price = self.hyperliquid_spot

            # 2. Coinbase spot (for CFTC index reference)
            resp_cb = await client.get(COINBASE_SPOT_URL, timeout=2.5)
            if resp_cb.status_code == 200:
                data = resp_cb.json().get("data", {})
                amt = data.get("amount")
                if amt:
                    self.coinbase_spot = float(amt)

            # Update 15m candle high/low
            if self.spot_price > self.current_15m_candle_high:
                self.current_15m_candle_high = self.spot_price
            if self.spot_price < self.current_15m_candle_low or self.current_15m_candle_low == 0.0:
                self.current_15m_candle_low = self.spot_price

            self.latency_ms = round((time.time() - t0) * 1000.0, 1)
            self.last_update_ts = time.time()
        except Exception as e:
            # print(f"Error updating subsecond spot: {e}")
            pass

    async def update_candles_and_technicals(self, client: httpx.AsyncClient):
        """
        Pull full Kraken 15m OHLC history (720+ candles) and calculate:
        - Realized volatilities across multiple horizons (15m, 1h, 24h, 7d)
        - Parkinson & Garman-Klass high/low volatility estimators
        - RSI(14), MACD(12,26,9), Bollinger Bands, ATR(14), EMAs
        """
        try:
            resp = await client.get(KRAKEN_OHLC_URL, timeout=5.0)
            if resp.status_code != 200:
                return

            raw_candles = resp.json().get("result", {}).get("XXBTZUSD", [])
            if not raw_candles or len(raw_candles) < 50:
                return

            opens = np.array([float(c[1]) for c in raw_candles])
            highs = np.array([float(c[2]) for c in raw_candles])
            lows = np.array([float(c[3]) for c in raw_candles])
            closes = np.array([float(c[4]) for c in raw_candles])
            volumes = np.array([float(c[6]) for c in raw_candles])

            current_c = raw_candles[-1]
            self.kraken_spot = float(current_c[4])
            self.current_15m_candle_open = float(current_c[1])
            self.current_15m_candle_high = float(current_c[2])
            self.current_15m_candle_low = float(current_c[3])
            self.current_15m_start_ts = int(current_c[0])

            # Periods per year for 15-min candles = 365.25 * 24 * 4 = 35064
            ann_factor = np.sqrt(35064)

            # 1. 24h Volatility (last 96 15m candles)
            c_24h = closes[-96:]
            log_ret_24h = np.diff(np.log(c_24h))
            self.vol_24h_ann = float(np.std(log_ret_24h) * ann_factor) if len(log_ret_24h) > 1 else 0.45
            self.vol_15m_dollar = float(np.std(np.diff(c_24h))) if len(c_24h) > 1 else 95.0

            # 2. 1h Volatility (last 4 candles)
            c_1h = closes[-8:]
            if len(c_1h) > 2:
                self.vol_1h_ann = float(np.std(np.diff(np.log(c_1h))) * ann_factor)
                if np.isnan(self.vol_1h_ann) or self.vol_1h_ann < 0.05:
                    self.vol_1h_ann = self.vol_24h_ann

            # 3. 7d Volatility (last 672 candles)
            c_7d = closes[-672:] if len(closes) >= 672 else closes
            log_ret_7d = np.diff(np.log(c_7d))
            self.vol_7d_ann = float(np.std(log_ret_7d) * ann_factor) if len(log_ret_7d) > 1 else self.vol_24h_ann

            # 4. Parkinson Volatility (24h)
            h_24h = highs[-96:]
            l_24h = lows[-96:]
            parkinson_sq = (1.0 / (4.0 * np.log(2.0) * len(h_24h))) * np.sum((np.log(h_24h / l_24h)) ** 2)
            self.vol_parkinson_24h = float(np.sqrt(parkinson_sq) * ann_factor)

            # 5. Garman-Klass Volatility (24h)
            o_24h = opens[-96:]
            gk_terms = 0.5 * (np.log(h_24h / l_24h)) ** 2 - (2.0 * np.log(2.0) - 1.0) * (np.log(c_24h / o_24h)) ** 2
            self.vol_garman_klass_24h = float(np.sqrt(np.mean(gk_terms)) * ann_factor)

            # 6. Technical Indicators
            # RSI(14)
            deltas = np.diff(closes)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            avg_gain = np.mean(gains[-14:])
            avg_loss = np.mean(losses[-14:])
            rs = avg_gain / (avg_loss + 1e-9)
            self.rsi_14 = float(100.0 - (100.0 / (1.0 + rs)))

            # Bollinger Bands (20)
            self.bb_mid = float(np.mean(closes[-20:]))
            std20 = float(np.std(closes[-20:]))
            self.bb_upper = self.bb_mid + 2.0 * std20
            self.bb_lower = self.bb_mid - 2.0 * std20
            self.bb_pct_b = float((closes[-1] - self.bb_lower) / (self.bb_upper - self.bb_lower + 1e-9))
            self.bb_bandwidth = float((self.bb_upper - self.bb_lower) / (self.bb_mid + 1e-9) * 100.0)

            # MACD (12, 26, 9)
            def compute_ema(vals, span):
                alpha = 2.0 / (span + 1.0)
                out = np.zeros_like(vals)
                out[0] = vals[0]
                for i in range(1, len(vals)):
                    out[i] = alpha * vals[i] + (1 - alpha) * out[i - 1]
                return out

            ema12 = compute_ema(closes, 12)
            ema26 = compute_ema(closes, 26)
            macd_raw = ema12 - ema26
            macd_sig = compute_ema(macd_raw, 9)
            self.macd_line = float(macd_raw[-1])
            self.macd_signal = float(macd_sig[-1])
            self.macd_hist = float(self.macd_line - self.macd_signal)

            # ATR(14)
            tr_list = []
            for i in range(len(closes) - 14, len(closes)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                tr_list.append(tr)
            self.atr_14 = float(np.mean(tr_list))

            # Moving Averages
            self.ema_9 = float(compute_ema(closes, 9)[-1])
            self.ema_21 = float(compute_ema(closes, 21)[-1])
            self.sma_50 = float(np.mean(closes[-50:]))
            self.sma_200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else self.sma_50

            # 24h High/Low and Change
            self.high_24h = float(np.max(highs[-96:]))
            self.low_24h = float(np.min(lows[-96:]))
            self.change_24h_pct = float(((closes[-1] - closes[-96]) / closes[-96]) * 100.0)
            self.volume_24h_btc = float(np.sum(volumes[-96:]))
            self.vwap_24h = float(np.sum(closes[-96:] * volumes[-96:]) / (np.sum(volumes[-96:]) + 1e-9))
        except Exception as e:
            # print(f"Error updating candles and technicals: {e}")
            pass

    async def update_derivatives_context(self, client: httpx.AsyncClient):
        """Pull Hyperliquid funding rates and open interest."""
        try:
            resp = await client.post(HYPERLIQUID_INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=4.0)
            if resp.status_code == 200:
                data = resp.json()
                universe = data[0].get("universe", [])
                ctxs = data[1]
                for i, u in enumerate(universe):
                    if u.get("name") == "BTC" and i < len(ctxs):
                        ctx = ctxs[i]
                        funding = float(ctx.get("funding") or 0.0)
                        self.funding_rate_8h = funding
                        self.funding_rate_ann = funding * 3.0 * 365.25 * 100.0
                        self.open_interest_btc = float(ctx.get("openInterest") or 0.0)
                        break
        except Exception as e:
            pass

    def get_summary_dict(self) -> Dict[str, Any]:
        """Return full metrics package for API and frontend display."""
        return {
            "spot_price": round(self.spot_price, 2),
            "coinbase_spot": round(self.coinbase_spot, 2),
            "kraken_spot": round(self.kraken_spot, 2),
            "hyperliquid_spot": round(self.hyperliquid_spot, 2),
            "high_24h": round(self.high_24h, 2),
            "low_24h": round(self.low_24h, 2),
            "change_24h_pct": round(self.change_24h_pct, 2),
            "volume_24h_btc": round(self.volume_24h_btc, 2),
            "vwap_24h": round(self.vwap_24h, 2),
            
            # Volatility
            "vol_24h_ann_pct": round(self.vol_24h_ann * 100.0, 2),
            "vol_1h_ann_pct": round(self.vol_1h_ann * 100.0, 2),
            "vol_7d_ann_pct": round(self.vol_7d_ann * 100.0, 2),
            "vol_parkinson_pct": round(self.vol_parkinson_24h * 100.0, 2),
            "vol_garman_klass_pct": round(self.vol_garman_klass_24h * 100.0, 2),
            "vol_15m_dollar": round(self.vol_15m_dollar, 2),
            
            # Technical Indicators
            "rsi_14": round(self.rsi_14, 1),
            "bb_upper": round(self.bb_upper, 2),
            "bb_mid": round(self.bb_mid, 2),
            "bb_lower": round(self.bb_lower, 2),
            "bb_pct_b": round(self.bb_pct_b, 2),
            "bb_bandwidth": round(self.bb_bandwidth, 2),
            "macd_line": round(self.macd_line, 2),
            "macd_signal": round(self.macd_signal, 2),
            "macd_hist": round(self.macd_hist, 2),
            "atr_14": round(self.atr_14, 2),
            "ema_9": round(self.ema_9, 2),
            "ema_21": round(self.ema_21, 2),
            "sma_50": round(self.sma_50, 2),
            "sma_200": round(self.sma_200, 2),
            
            # Derivatives Microstructure
            "funding_rate_8h_pct": round(self.funding_rate_8h * 100.0, 4),
            "funding_rate_ann_pct": round(self.funding_rate_ann, 2),
            "open_interest_btc": round(self.open_interest_btc, 2),
            "open_interest_usd": round(self.open_interest_btc * self.spot_price, 0),
            
            # 15m Candle
            "candle_15m_open": round(self.current_15m_candle_open, 2),
            "candle_15m_high": round(self.current_15m_candle_high, 2),
            "candle_15m_low": round(self.current_15m_candle_low, 2),
            "candle_15m_delta": round(self.spot_price - self.current_15m_candle_open, 2),
            "candle_15m_delta_pct": round(((self.spot_price - self.current_15m_candle_open) / (self.current_15m_candle_open + 1e-9)) * 100.0, 3),

            # Latency & Timestamps
            "latency_ms": self.latency_ms,
            "last_update_ts": self.last_update_ts
        }
