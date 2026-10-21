import asyncio
import time
import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import httpx

from btc_metrics import BitcoinMetricsProvider
from prediction_engine import QuantPredictionEngine

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

BTC_SERIES_REGISTRY = [
    {"ticker": "KXBTC15M", "type": "15m_scalp", "name": "15-Min Scalp Up/Down"},
    {"ticker": "KXBTCD", "type": "strike_ladder", "name": "Strike Ladder (Above/Below)"},
    {"ticker": "KXBTC", "type": "range_bracket", "name": "Price Range Brackets"},
    {"ticker": "KXBTCMAXY", "type": "barrier_max", "name": "Yearly High Targets"},
    {"ticker": "KXBTCMINY", "type": "barrier_min", "name": "Yearly Downside Floors"},
    {"ticker": "KXBTCMINMON", "type": "barrier_min", "name": "Monthly Downside Floors"},
    {"ticker": "KXBTCPRICE", "type": "strike_ladder", "name": "Milestone Price Targets"},
    {"ticker": "KXBTCMAX100", "type": "barrier_max", "name": "BTC $100k Target Race"},
    {"ticker": "KXBTC2026250", "type": "strike_ladder", "name": "BTC $250k in 2026 Target"},
    {"ticker": "KXBTC50VS100", "type": "relative", "name": "BTC $50k vs $100k Race"},
    {"ticker": "KXBTCRESERVE", "type": "macro_event", "name": "Strategic Bitcoin Reserve"},
    {"ticker": "KXBTCVSGOLD", "type": "relative", "name": "BTC vs Gold Return"},
    {"ticker": "KXINXVSBTC", "type": "relative", "name": "S&P 500 vs BTC Return"},
]

def parse_iso_time(iso_str: str) -> Optional[float]:
    """Parse ISO8601 string to UNIX epoch timestamp."""
    try:
        if not iso_str:
            return None
        # Handle 'Z' or offset
        iso_clean = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_clean)
        return dt.timestamp()
    except Exception:
        return None

def format_time_remaining(seconds: float) -> str:
    """Format seconds into human-readable countdown string."""
    if seconds <= 0:
        return "Expired"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"
    days = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{days}d {h}h"

class KalshiBitcoinScanner:
    """
    Comprehensive scanner for all Kalshi Bitcoin markets.
    Integrates live spot metrics, quantitative models, Black-Scholes,
    Jump-Diffusion Monte Carlo, and arbitrage detection.
    """
    def __init__(self, metrics: BitcoinMetricsProvider, engine: QuantPredictionEngine):
        self.metrics = metrics
        self.engine = engine
        self.taker_fee_cents = 0.5

        # Cached Results
        self.cache: Dict[str, Any] = {
            "timestamp": 0,
            "elapsed_seconds": 0,
            "total_markets_scanned": 0,
            "btc_spot": 81500.0,
            "kxbtc15m": {},
            "top_ev_opportunities": [],
            "strike_ladders_by_expiry": {},
            "range_brackets": [],
            "barrier_targets": [],
            "arbitrage_alerts": [],
            "stats": {}
        }
        self.is_scanning = False

    async def scan_kxbtc15m_fast(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        """Fast 1-second refresh of the active KXBTC15M 15-minute scalp market."""
        spot = self.metrics.spot_price
        now = time.time()
        try:
            resp = await client.get(
                f"{KALSHI_API_BASE}/markets?series_ticker=KXBTC15M&status=open&limit=5",
                timeout=3.0
            )
            if resp.status_code != 200:
                return self.cache.get("kxbtc15m", {})
            markets = resp.json().get("markets", [])
            if not markets:
                return self.cache.get("kxbtc15m", {})

            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            active = [m for m in markets if (m.get("close_time") or "") >= now_iso]
            if active:
                active.sort(key=lambda m: m.get("close_time", ""))
                m = active[0]
            else:
                markets.sort(key=lambda m: m.get("close_time", ""), reverse=True)
                m = markets[0]

            ticker = m.get("ticker", "")
            title = m.get("title", "")
            y_ask = float(m.get("yes_ask_dollars") or 0.0)
            y_bid = float(m.get("yes_bid_dollars") or 0.0)
            n_ask = float(m.get("no_ask_dollars") or 0.0)
            n_bid = float(m.get("no_bid_dollars") or 0.0)
            close_time_iso = m.get("close_time", "")
            close_ts = parse_iso_time(close_time_iso)
            seconds_left = max(1.0, close_ts - now) if close_ts else 3600.0
            countdown_str = format_time_remaining(seconds_left)

            official_strike = float(m.get("floor_strike") or 0.0)
            candle_open = official_strike if official_strike > 0.0 else self.metrics.current_15m_candle_open
            self.metrics.current_15m_candle_open = candle_open
            accumulated = None
            if hasattr(self.metrics, "brti_accumulator"):
                self.metrics.brti_accumulator.add_sample(spot, seconds_left, candle_id=close_time_iso)
                accumulated = self.metrics.brti_accumulator.get_samples()

            scalp_res = self.engine.evaluate_15m_scalp(
                spot=spot,
                open_price=candle_open,
                seconds_left=seconds_left,
                dollar_vol_15m=self.metrics.vol_15m_dollar,
                accumulated_samples=accumulated
            )
            model_prob = scalp_res["prob_yes"]
            trade_metrics = self.engine.calculate_trade_metrics(model_prob, y_ask, y_bid, n_ask, n_bid)

            kxbtc15m_data = {
                "ticker": ticker,
                "title": title,
                "close_time": close_time_iso,
                "seconds_left": int(seconds_left),
                "countdown_str": countdown_str,
                "candle_open": candle_open,
                "spot_price": spot,
                "delta": scalp_res["delta"],
                "delta_pct": scalp_res["delta_pct"],
                "time_elapsed_pct": scalp_res["time_elapsed_pct"],
                "z_score": scalp_res["z_score"],
                "model_prob_yes_pct": scalp_res["prob_yes_pct"],
                "model_prob_no_pct": scalp_res["prob_no_pct"],
                "yes_bid_cents": int(round(y_bid * 100)),
                "yes_ask_cents": int(round(y_ask * 100)),
                "no_bid_cents": int(round(n_bid * 100)),
                "no_ask_cents": int(round(n_ask * 100)),
                "trade_signal": trade_metrics["signal"],
                "side": trade_metrics["side"],
                "best_edge_pct": trade_metrics["best_edge_pct"],
                "best_ev_cents": trade_metrics["best_ev_cents"],
                "best_roi_pct": trade_metrics["best_roi_pct"],
                "half_kelly_pct": trade_metrics["half_kelly_pct"],
                "full_kelly_pct": trade_metrics["full_kelly_pct"],
                "price_cents": trade_metrics["price_cents"],
                "url": f"https://kalshi.com/markets/{ticker}"
            }
            self.cache["kxbtc15m"] = kxbtc15m_data
            self.cache["btc_spot"] = spot
            return kxbtc15m_data
        except Exception:
            return self.cache.get("kxbtc15m", {})

    async def scan_all_btc_markets(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        """Perform a full multi-series scan across all Kalshi Bitcoin markets."""
        t_start = time.time()
        self.is_scanning = True

        spot = self.metrics.spot_price
        vol_ann = self.metrics.vol_24h_ann
        now = time.time()

        all_markets_raw: List[Dict[str, Any]] = []

        # Pull markets for all BTC series
        for item in BTC_SERIES_REGISTRY:
            series_ticker = item["ticker"]
            try:
                resp = await client.get(
                    f"{KALSHI_API_BASE}/markets?series_ticker={series_ticker}&status=open&limit=1000",
                    timeout=4.0
                )
                if resp.status_code == 200:
                    markets = resp.json().get("markets", [])
                    for m in markets:
                        m["_series_meta"] = item
                    all_markets_raw.extend(markets)
            except Exception as e:
                # print(f"Error fetching {series_ticker}: {e}")
                pass

        # Process markets
        kxbtc15m_data: Dict[str, Any] = {}
        top_opportunities: List[Dict[str, Any]] = []
        strike_ladders_by_expiry: Dict[str, List[Dict[str, Any]]] = {}
        range_brackets: List[Dict[str, Any]] = []
        barrier_targets: List[Dict[str, Any]] = []
        arbitrage_alerts: List[Dict[str, Any]] = []

        for m in all_markets_raw:
            ticker = m.get("ticker", "")
            title = m.get("title", "")
            series_meta = m.get("_series_meta", {})
            series_type = series_meta.get("type", "unknown")
            series_ticker = series_meta.get("ticker", "")

            y_ask = float(m.get("yes_ask_dollars") or 0.0)
            y_bid = float(m.get("yes_bid_dollars") or 0.0)
            n_ask = float(m.get("no_ask_dollars") or 0.0)
            n_bid = float(m.get("no_bid_dollars") or 0.0)
            last_p = float(m.get("last_price_dollars") or 0.0)
            volume_24h = m.get("volume_24h") or 0
            open_interest = m.get("open_interest") or 0

            close_time_iso = m.get("close_time", "")
            close_ts = parse_iso_time(close_time_iso)
            seconds_left = max(1.0, close_ts - now) if close_ts else 3600.0
            years_left = seconds_left / (365.25 * 86400.0)
            countdown_str = format_time_remaining(seconds_left)

            # Implied probabilities from Kalshi book
            implied_yes_cents = int(round(y_ask * 100)) if y_ask > 0 else (int(round((1.0 - n_bid) * 100)) if n_bid > 0 else 50)
            implied_no_cents = int(round(n_ask * 100)) if n_ask > 0 else (int(round((1.0 - y_bid) * 100)) if y_bid > 0 else 50)

            # -----------------------------------------------------------------
            # 1. 15-Minute Scalp Market (KXBTC15M)
            # -----------------------------------------------------------------
            if series_ticker == "KXBTC15M":
                # Use Kalshi's official floor_strike (the exact BRTI target price to beat) when populated
                official_strike = float(m.get("floor_strike") or 0.0)
                candle_open = official_strike if official_strike > 0.0 else self.metrics.current_15m_candle_open
                self.metrics.current_15m_candle_open = candle_open
                accumulated = None
                if hasattr(self.metrics, "brti_accumulator"):
                    self.metrics.brti_accumulator.add_sample(spot, seconds_left, candle_id=close_time_iso)
                    accumulated = self.metrics.brti_accumulator.get_samples()

                scalp_res = self.engine.evaluate_15m_scalp(
                    spot=spot,
                    open_price=candle_open,
                    seconds_left=seconds_left,
                    dollar_vol_15m=self.metrics.vol_15m_dollar,
                    accumulated_samples=accumulated
                )
                model_prob = scalp_res["prob_yes"]
                trade_metrics = self.engine.calculate_trade_metrics(model_prob, y_ask, y_bid, n_ask, n_bid)

                kxbtc15m_data = {
                    "ticker": ticker,
                    "title": title,
                    "close_time": close_time_iso,
                    "seconds_left": int(seconds_left),
                    "countdown_str": countdown_str,
                    "candle_open": candle_open,
                    "spot_price": spot,
                    "delta": scalp_res["delta"],
                    "delta_pct": scalp_res["delta_pct"],
                    "time_elapsed_pct": scalp_res["time_elapsed_pct"],
                    "z_score": scalp_res["z_score"],
                    "model_prob_yes_pct": scalp_res["prob_yes_pct"],
                    "model_prob_no_pct": scalp_res["prob_no_pct"],
                    "yes_bid_cents": int(round(y_bid * 100)),
                    "yes_ask_cents": int(round(y_ask * 100)),
                    "no_bid_cents": int(round(n_bid * 100)),
                    "no_ask_cents": int(round(n_ask * 100)),
                    "trade_signal": trade_metrics["signal"],
                    "side": trade_metrics["side"],
                    "best_edge_pct": trade_metrics["best_edge_pct"],
                    "best_ev_cents": trade_metrics["best_ev_cents"],
                    "best_roi_pct": trade_metrics["best_roi_pct"],
                    "half_kelly_pct": trade_metrics["half_kelly_pct"],
                    "full_kelly_pct": trade_metrics["full_kelly_pct"],
                    "price_cents": trade_metrics["price_cents"],
                    "url": f"https://kalshi.com/markets/{ticker}"
                }

                # Also add to opportunities if positive EV
                if trade_metrics["best_ev_cents"] > 1.0:
                    top_opportunities.append({
                        "ticker": ticker,
                        "series": "15M Scalp",
                        "title": title,
                        "signal": trade_metrics["signal"],
                        "side": trade_metrics["side"],
                        "model_prob_pct": scalp_res["prob_yes_pct"] if trade_metrics["side"] == "YES" else scalp_res["prob_no_pct"],
                        "book_price_cents": trade_metrics["price_cents"],
                        "edge_pct": trade_metrics["best_edge_pct"],
                        "ev_cents": trade_metrics["best_ev_cents"],
                        "roi_pct": trade_metrics["best_roi_pct"],
                        "half_kelly_pct": trade_metrics["half_kelly_pct"],
                        "countdown": countdown_str,
                        "url": f"https://kalshi.com/markets/{ticker}",
                        "why": (
                            f"BTC is {scalp_res['delta']:+.2f} ({scalp_res['delta_pct']:+.2f}%) from 15m open (${candle_open:,.2f}) with {int(seconds_left)}s left. "
                            f"Drift-diffusion prices YES win rate at {scalp_res['prob_yes_pct']}%, while Kalshi orderbook asks {implied_yes_cents}¢. "
                            f"Expected net profit is +{trade_metrics['best_ev_cents']}¢ (+{trade_metrics['best_roi_pct']}% ROI)."
                        )
                    })

            # -----------------------------------------------------------------
            # 2. Strike Ladder Markets (KXBTCD & KXBTCPRICE)
            # -----------------------------------------------------------------
            elif series_type == "strike_ladder":
                strike = float(m.get("floor_strike") or 0.0)
                if strike <= 0.0:
                    continue

                strike_type = m.get("strike_type", "greater")
                # Closed-form Black-Scholes probability
                model_prob = self.engine.black_scholes_binary(
                    spot=spot,
                    strike=strike,
                    time_to_expiry_years=years_left,
                    volatility=vol_ann,
                    strike_type=strike_type
                )
                model_pct = round(model_prob * 100.0, 1)

                trade_metrics = self.engine.calculate_trade_metrics(model_prob, y_ask, y_bid, n_ask, n_bid)

                ladder_entry = {
                    "ticker": ticker,
                    "title": title,
                    "strike": strike,
                    "distance_dollars": round(strike - spot, 1),
                    "distance_pct": round(((strike - spot) / spot) * 100.0, 2),
                    "close_time": close_time_iso,
                    "countdown_str": countdown_str,
                    "seconds_left": int(seconds_left),
                    "model_prob_pct": model_pct,
                    "yes_bid_cents": int(round(y_bid * 100)),
                    "yes_ask_cents": int(round(y_ask * 100)),
                    "no_bid_cents": int(round(n_bid * 100)),
                    "no_ask_cents": int(round(n_ask * 100)),
                    "spread_cents": trade_metrics["spread_cents"],
                    "signal": trade_metrics["signal"],
                    "side": trade_metrics["side"],
                    "best_edge_pct": trade_metrics["best_edge_pct"],
                    "best_ev_cents": trade_metrics["best_ev_cents"],
                    "best_roi_pct": trade_metrics["best_roi_pct"],
                    "half_kelly_pct": trade_metrics["half_kelly_pct"],
                    "volume_24h": volume_24h,
                    "open_interest": open_interest,
                    "url": f"https://kalshi.com/markets/{ticker}"
                }

                # Group by expiration
                expiry_group_key = close_time_iso or "Unknown Expiry"
                if expiry_group_key not in strike_ladders_by_expiry:
                    strike_ladders_by_expiry[expiry_group_key] = []
                strike_ladders_by_expiry[expiry_group_key].append(ladder_entry)

                # Flag high-EV strike trades
                if trade_metrics["best_ev_cents"] >= 2.0 and trade_metrics["best_roi_pct"] >= 5.0 and (y_ask > 0 or n_ask > 0):
                    why_text = (
                        f"Strike ${strike:,.0f} ({ladder_entry['distance_dollars']:+,.0f} from spot ${spot:,.2f}) expires in {countdown_str}. "
                        f"Black-Scholes model calculates {model_pct}% true probability of expiring ITM. "
                        f"Kalshi is pricing this at {trade_metrics['price_cents']}¢, giving a +{trade_metrics['best_edge_pct']}% mathematical edge "
                        f"and +{trade_metrics['best_ev_cents']}¢ expected value (+{trade_metrics['best_roi_pct']}% ROI)."
                    )
                    top_opportunities.append({
                        "ticker": ticker,
                        "series": f"Strike ${strike:,.0f}",
                        "title": title,
                        "signal": trade_metrics["signal"],
                        "side": trade_metrics["side"],
                        "model_prob_pct": model_pct if trade_metrics["side"] == "YES" else round(100.0 - model_pct, 1),
                        "book_price_cents": trade_metrics["price_cents"],
                        "edge_pct": trade_metrics["best_edge_pct"],
                        "ev_cents": trade_metrics["best_ev_cents"],
                        "roi_pct": trade_metrics["best_roi_pct"],
                        "half_kelly_pct": trade_metrics["half_kelly_pct"],
                        "countdown": countdown_str,
                        "url": f"https://kalshi.com/markets/{ticker}",
                        "why": why_text
                    })

            # -----------------------------------------------------------------
            # 3. Price Range Brackets (KXBTC)
            # -----------------------------------------------------------------
            elif series_type == "range_bracket":
                floor_s = float(m.get("floor_strike") or 0.0)
                cap_s = float(m.get("cap_strike") or 0.0)
                if floor_s > 0 and cap_s > 0:
                    model_range_prob = self.engine.black_scholes_range(
                        spot=spot,
                        floor_strike=floor_s,
                        cap_strike=cap_s,
                        time_to_expiry_years=years_left,
                        volatility=vol_ann
                    )
                    model_pct = round(model_range_prob * 100.0, 1)
                    trade_metrics = self.engine.calculate_trade_metrics(model_range_prob, y_ask, y_bid, n_ask, n_bid)

                    range_brackets.append({
                        "ticker": ticker,
                        "title": title,
                        "floor_strike": floor_s,
                        "cap_strike": cap_s,
                        "range_label": f"${floor_s:,.0f} - ${cap_s:,.0f}",
                        "close_time": close_time_iso,
                        "countdown_str": countdown_str,
                        "model_prob_pct": model_pct,
                        "yes_bid_cents": int(round(y_bid * 100)),
                        "yes_ask_cents": int(round(y_ask * 100)),
                        "signal": trade_metrics["signal"],
                        "best_edge_pct": trade_metrics["best_edge_pct"],
                        "best_ev_cents": trade_metrics["best_ev_cents"],
                        "best_roi_pct": trade_metrics["best_roi_pct"],
                        "url": f"https://kalshi.com/markets/{ticker}"
                    })

            # -----------------------------------------------------------------
            # 4. Barrier Targets & Extreme Floors (KXBTCMAXY, KXBTCMINY, etc.)
            # -----------------------------------------------------------------
            elif series_type in ["barrier_max", "barrier_min"]:
                barrier = float(m.get("floor_strike") or m.get("cap_strike") or 0.0)
                if barrier <= 0.0:
                    # Parse from title if strike is missing
                    continue

                if series_type == "barrier_max":
                    model_prob = self.engine.one_touch_barrier_up(
                        spot=spot,
                        barrier=barrier,
                        time_to_expiry_years=years_left,
                        volatility=vol_ann
                    )
                else:
                    model_prob = self.engine.one_touch_barrier_down(
                        spot=spot,
                        barrier=barrier,
                        time_to_expiry_years=years_left,
                        volatility=vol_ann
                    )

                model_pct = round(model_prob * 100.0, 1)
                trade_metrics = self.engine.calculate_trade_metrics(model_prob, y_ask, y_bid, n_ask, n_bid)

                barrier_targets.append({
                    "ticker": ticker,
                    "title": title,
                    "barrier_type": "HIGH TARGET" if series_type == "barrier_max" else "DOWNSIDE FLOOR",
                    "barrier_price": barrier,
                    "distance_pct": round(((barrier - spot) / spot) * 100.0, 1),
                    "close_time": close_time_iso,
                    "countdown_str": countdown_str,
                    "model_prob_pct": model_pct,
                    "yes_bid_cents": int(round(y_bid * 100)),
                    "yes_ask_cents": int(round(y_ask * 100)),
                    "signal": trade_metrics["signal"],
                    "best_edge_pct": trade_metrics["best_edge_pct"],
                    "best_ev_cents": trade_metrics["best_ev_cents"],
                    "best_roi_pct": trade_metrics["best_roi_pct"],
                    "half_kelly_pct": trade_metrics["half_kelly_pct"],
                    "url": f"https://kalshi.com/markets/{ticker}"
                })

                if trade_metrics["best_ev_cents"] >= 2.0 and trade_metrics["best_roi_pct"] >= 8.0:
                    top_opportunities.append({
                        "ticker": ticker,
                        "series": f"Barrier ${barrier:,.0f}",
                        "title": title,
                        "signal": trade_metrics["signal"],
                        "side": trade_metrics["side"],
                        "model_prob_pct": model_pct if trade_metrics["side"] == "YES" else round(100.0 - model_pct, 1),
                        "book_price_cents": trade_metrics["price_cents"],
                        "edge_pct": trade_metrics["best_edge_pct"],
                        "ev_cents": trade_metrics["best_ev_cents"],
                        "roi_pct": trade_metrics["best_roi_pct"],
                        "half_kelly_pct": trade_metrics["half_kelly_pct"],
                        "countdown": countdown_str,
                        "url": f"https://kalshi.com/markets/{ticker}",
                        "why": f"Analytical one-touch barrier model projects a {model_pct}% touch probability for ${barrier:,.0f}. Kalshi is trading at {trade_metrics['price_cents']}¢."
                    })

        # Run Monotonicity / Inversion Arbitrage Detector on strike ladders
        for expiry_key, ladder in strike_ladders_by_expiry.items():
            violations = self.engine.detect_monotonicity_violations(ladder)
            if violations:
                for v in violations:
                    v["expiry"] = expiry_key
                    arbitrage_alerts.append(v)

        # Sort top opportunities by EV cents descending
        top_opportunities.sort(key=lambda x: (x.get("ev_cents", 0.0), x.get("roi_pct", 0.0)), reverse=True)
        top_opportunities = top_opportunities[:25] # Keep top 25

        # Sort range brackets by floor strike
        range_brackets.sort(key=lambda x: x.get("floor_strike", 0.0))

        # Sort barrier targets by barrier price
        barrier_targets.sort(key=lambda x: x.get("barrier_price", 0.0))

        elapsed = round(time.time() - t_start, 2)
        total_scanned = len(all_markets_raw)

        self.cache = {
            "timestamp": int(time.time()),
            "elapsed_seconds": elapsed,
            "total_markets_scanned": total_scanned,
            "btc_spot": spot,
            "kxbtc15m": kxbtc15m_data,
            "top_ev_opportunities": top_opportunities,
            "strike_ladders_by_expiry": strike_ladders_by_expiry,
            "range_brackets": range_brackets,
            "barrier_targets": barrier_targets,
            "arbitrage_alerts": arbitrage_alerts,
            "stats": {
                "total_scanned": total_scanned,
                "ev_opportunities_count": len(top_opportunities),
                "arbitrage_alerts_count": len(arbitrage_alerts),
                "expiries_tracked": len(strike_ladders_by_expiry),
                "highest_ev_cents": top_opportunities[0].get("ev_cents", 0.0) if top_opportunities else 0.0,
                "highest_roi_pct": top_opportunities[0].get("roi_pct", 0.0) if top_opportunities else 0.0
            }
        }
        self.is_scanning = False
        return self.cache
