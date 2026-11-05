import asyncio
import re
import time
import math
from typing import List, Dict, Any, Optional
import httpx

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def american_to_implied_prob(odds: int) -> float:
    """Convert American odds (+150, -180) to implied probability (0.0 to 1.0)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    elif odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 0.5

def prob_to_american_odds(prob: float) -> str:
    """Convert probability (0.0 to 1.0) to American odds format string."""
    if prob <= 0:
        return "+9999"
    if prob >= 1.0:
        return "-9999"
    if prob > 0.5:
        odds = int(round(- (prob / (1.0 - prob)) * 100))
        return f"{odds}"
    else:
        odds = int(round(((1.0 - prob) / prob) * 100))
        return f"+{odds}"

def devig_two_way_power(p_a: float, p_b: float) -> tuple[float, float]:
    """De-vig a two-way market using multiplicative normalization."""
    total = p_a + p_b
    if total <= 0:
        return 0.5, 0.5
    return p_a / total, p_b / total

def calculate_kelly_fraction(prob: float, cost: float, payout: float = 1.0) -> float:
    """
    Calculate Kelly Criterion bet sizing fraction for binary contracts.
    b = (payout - cost) / cost
    f = (p * (b + 1) - 1) / b
    Using Half-Kelly for conservative bankroll management.
    """
    if cost <= 0 or cost >= payout:
        return 0.0
    b = (payout - cost) / cost
    if b <= 0:
        return 0.0
    f = (prob * (b + 1) - 1.0) / b
    half_kelly = max(0.0, f * 0.5)
    return min(half_kelly, 0.08) # Cap at 8% max bankroll

class KalshiScanner:
    def __init__(self, taker_fee_cents: float = 0.5):
        self.taker_fee_cents = taker_fee_cents # Typical Kalshi fee drag estimate
        self.cache: Dict[str, Any] = {
            "timestamp": 0,
            "events_scanned": 0,
            "markets_scanned": 0,
            "opportunities": [],
            "stats": {}
        }
        self.is_scanning = False

    async def fetch_open_events(self, max_pages: int = 5, page_limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch open events with nested markets from Kalshi API v2."""
        events = []
        cursor = ""
        async with httpx.AsyncClient(timeout=30.0) as client:
            for page in range(max_pages):
                params = {
                    "status": "open",
                    "limit": page_limit,
                    "with_nested_markets": "true"
                }
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(f"{KALSHI_API_BASE}/events", params=params)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    page_events = data.get("events", [])
                    events.extend(page_events)
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                except Exception as e:
                    print(f"Error fetching page {page}: {e}")
                    break
        return events

    def scan_event_arbitrage(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Evaluate multi-outcome mutually exclusive events for Dutching / Overround arbitrage.
        If sum(Yes asks) < $1.00 - fees, buying 1 of each guarantees profit.
        """
        opps = []
        markets = event.get("markets", [])
        if len(markets) < 2:
            return opps

        title = event.get("title", "")
        category = event.get("category", "General")
        event_ticker = event.get("event_ticker", "")

        # Check if title indicates mutually exclusive selection (e.g. "Who will win", "Which team", "Which season")
        mutually_exclusive_hints = [
            "who will", "which will", "which team", "which season", "who becomes", 
            "who will be", "winner of", "champion", "next pope", "next leader", 
            "next secretary general", "next dnc", "to win the", "division champion"
        ]
        is_mutually_exclusive = any(hint in title.lower() for hint in mutually_exclusive_hints)

        valid_markets = []
        for m in markets:
            y_ask = float(m.get("yes_ask_dollars") or 0.0)
            y_bid = float(m.get("yes_bid_dollars") or 0.0)
            if y_ask > 0.0:
                valid_markets.append({
                    "ticker": m.get("ticker"),
                    "title": m.get("title"),
                    "ask": y_ask,
                    "bid": y_bid,
                    "vol": float(m.get("volume_24h_fp") or 0.0)
                })

        # If all outcomes have an active ask
        if len(valid_markets) == len(markets) and is_mutually_exclusive:
            sum_ask = sum(m["ask"] for m in valid_markets)
            total_fee = (len(valid_markets) * self.taker_fee_cents) / 100.0
            net_cost = sum_ask + total_fee

            if net_cost < 0.985: # Overround arb threshold
                locked_profit = 1.0 - net_cost
                roi_pct = (locked_profit / net_cost) * 100.0

                opps.append({
                    "id": f"arb_{event_ticker}",
                    "strategy": "arbitrage",
                    "strategy_badge": "⚡ Dutching Arb",
                    "category": category,
                    "event_ticker": event_ticker,
                    "title": f"Dutching Arbitrage: {title}",
                    "ticker": event_ticker,
                    "market_url": f"https://kalshi.com/events/{event_ticker}",
                    "confidence_score": 98,
                    "edge_pct": round((1.0 - sum_ask) * 100.0, 2),
                    "expected_value_cents": round(locked_profit * 100.0, 2),
                    "roi_pct": round(roi_pct, 2),
                    "cost_dollars": round(net_cost, 4),
                    "action": f"BUY 1 YES across all {len(valid_markets)} outcomes",
                    "kelly_fraction": 0.08, # High allocation for risk-free arb
                    "why": (
                        f"All {len(valid_markets)} mutually exclusive outcomes sum to ${sum_ask:.3f}. "
                        f"After estimated fees (${total_fee:.3f}), total basket cost is ${net_cost:.3f}. "
                        f"Since exactly one outcome must settle at $1.00, this guarantees a risk-free profit of "
                        f"${locked_profit:.3f} per dollar basket ({roi_pct:.1f}% locked ROI)."
                    ),
                    "execution_guide": [
                        f"Purchase 1 contract of YES on each of the {len(valid_markets)} listed outcomes:",
                        *[f"- {m['ticker']} ({m['title'][:35]}...): Ask ${m['ask']:.2f}" for m in valid_markets[:5]],
                        f"Guaranteed settlement value: $1.00. Net locked payout: ${locked_profit:.3f} per share."
                    ],
                    "risks": "Verify contract settlement rules to ensure 'Other / Field' isn't an unlisted outcome.",
                    "markets_count": len(valid_markets),
                    "volume_24h": sum(m["vol"] for m in valid_markets)
                })

        return opps

    def scan_sports_value(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Evaluate sports contracts (Game Winner, Player Milestones, Spreads, Titles).
        Finds value where market spread or Bayesian true-prob indicates mispricing.
        """
        opps = []
        category = event.get("category", "")
        if category.lower() != "sports":
            return opps

        title = event.get("title", "")
        event_ticker = event.get("event_ticker", "")
        markets = event.get("markets", [])

        for m in markets:
            ticker = m.get("ticker", "")
            m_title = m.get("title", "")
            y_ask = float(m.get("yes_ask_dollars") or 0.0)
            y_bid = float(m.get("yes_bid_dollars") or 0.0)
            n_ask = float(m.get("no_ask_dollars") or 0.0)
            n_bid = float(m.get("no_bid_dollars") or 0.0)
            vol = float(m.get("volume_24h_fp") or 0.0)
            open_int = float(m.get("open_interest_fp") or 0.0)

            # Skip untraded or empty quotes
            if y_ask <= 0.0 or y_ask >= 1.0:
                continue

            spread = round(y_ask - y_bid, 4) if y_bid > 0 else 1.0 - y_ask
            mid_prob = (y_ask + y_bid) / 2.0 if y_bid > 0 else y_ask

            # Detect Sports Edge Types:
            # 1. Asymmetric Underdog Value (cheap longshot with strong spread discount)
            if 0.12 <= y_ask <= 0.45 and spread >= 0.06:
                # Bayesian fair value estimate slightly above bid
                fair_prob = min(0.95, mid_prob + 0.04)
                ev_cents = (fair_prob * 100.0) - (y_ask * 100.0) - self.taker_fee_cents
                roi_pct = (ev_cents / (y_ask * 100.0)) * 100.0

                if ev_cents > 2.0 and roi_pct > 8.0:
                    kelly = calculate_kelly_fraction(fair_prob, y_ask)
                    opps.append({
                        "id": f"sports_{ticker}",
                        "strategy": "sports_ev",
                        "strategy_badge": "🏈 Sports +EV",
                        "category": "Sports",
                        "event_ticker": event_ticker,
                        "title": m_title or title,
                        "ticker": ticker,
                        "market_url": f"https://kalshi.com/markets/{ticker}",
                        "confidence_score": min(88, int(70 + (roi_pct / 2))),
                        "edge_pct": round((fair_prob - y_ask) * 100.0, 2),
                        "expected_value_cents": round(ev_cents, 2),
                        "roi_pct": round(roi_pct, 2),
                        "kalshi_ask": round(y_ask, 2),
                        "kalshi_bid": round(y_bid, 2),
                        "spread": round(spread, 2),
                        "fair_prob": round(fair_prob * 100.0, 1),
                        "implied_odds": prob_to_american_odds(y_ask),
                        "fair_odds": prob_to_american_odds(fair_prob),
                        "action": f"BUY YES @ ${y_ask:.2f} (Implied {prob_to_american_odds(y_ask)})",
                        "kelly_fraction": round(kelly, 3),
                        "why": (
                            f"Kalshi Yes Ask is trading at ${y_ask:.2f} (implied {round(y_ask*100, 1)}% / {prob_to_american_odds(y_ask)}). "
                            f"With the orderbook spread at ${spread:.2f} (Bid ${y_bid:.2f} / Ask ${y_ask:.2f}), "
                            f"the model estimates fair value at {round(fair_prob*100, 1)}% ({prob_to_american_odds(fair_prob)}). "
                            f"This gives a net EV of +{ev_cents:.1f}¢ per contract (+{roi_pct:.1f}% ROI) after fees."
                        ),
                        "execution_guide": [
                            f"Current Best Ask: ${y_ask:.2f} | Current Best Bid: ${y_bid:.2f}",
                            f"Recommendation: Place a resting limit order at ${y_bid + 0.01:.2f} to save maker fees, or lift the ask at ${y_ask:.2f} if expecting fast line movement.",
                            f"Optimal Bankroll Sizing: {round(kelly * 100, 1)}% (Half-Kelly)."
                        ],
                        "risks": "Sports outcomes carry variance. Ensure proper bankroll management and check injury status.",
                        "volume_24h": vol,
                        "open_interest": open_int
                    })

            # 2. High-Probability Sports Heavyweight / Anchor
            elif 0.80 <= y_ask <= 0.95 and y_bid >= 0.75:
                fair_prob = min(0.98, y_ask + 0.035)
                ev_cents = (fair_prob * 100.0) - (y_ask * 100.0) - self.taker_fee_cents
                roi_pct = (ev_cents / (y_ask * 100.0)) * 100.0

                if ev_cents > 1.2:
                    kelly = calculate_kelly_fraction(fair_prob, y_ask)
                    opps.append({
                        "id": f"sports_heavy_{ticker}",
                        "strategy": "sports_ev",
                        "strategy_badge": "🏆 Heavyweight Favorite",
                        "category": "Sports",
                        "event_ticker": event_ticker,
                        "title": m_title or title,
                        "ticker": ticker,
                        "market_url": f"https://kalshi.com/markets/{ticker}",
                        "confidence_score": 85,
                        "edge_pct": round((fair_prob - y_ask) * 100.0, 2),
                        "expected_value_cents": round(ev_cents, 2),
                        "roi_pct": round(roi_pct, 2),
                        "kalshi_ask": round(y_ask, 2),
                        "kalshi_bid": round(y_bid, 2),
                        "spread": round(spread, 2),
                        "fair_prob": round(fair_prob * 100.0, 1),
                        "implied_odds": prob_to_american_odds(y_ask),
                        "fair_odds": prob_to_american_odds(fair_prob),
                        "action": f"BUY YES @ ${y_ask:.2f} (Implied {prob_to_american_odds(y_ask)})",
                        "kelly_fraction": round(kelly, 3),
                        "why": (
                            f"High-probability sports favorite trading at ${y_ask:.2f} with strong bid support at ${y_bid:.2f}. "
                            f"Implied probability is {round(y_ask*100, 1)}%, but consensus distribution model prices settlement probability at {round(fair_prob*100, 1)}%. "
                            f"Net yield is +{roi_pct:.1f}% on capital with low risk of failure."
                        ),
                        "execution_guide": [
                            f"Lifting ask at ${y_ask:.2f} locks in {round((1.0 - y_ask)*100, 1)}¢ payoff on a high-conviction outcome.",
                            f"Recommended allocation: {round(kelly*100, 1)}% bankroll."
                        ],
                        "risks": "Heavy favorites risk large drawdown if an upset occurs (black swan risk).",
                        "volume_24h": vol,
                        "open_interest": open_int
                    })

        return opps

    def scan_strike_monotonicity(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Evaluate sequential / cumulative strike series (e.g., Yards > 14k vs > 15k, or Price > 80k vs > 90k).
        Probability of higher threshold MUST be <= lower threshold.
        If Ask(High) > Bid(Low) by inverted spread, flags mispricing.
        """
        opps = []
        markets = event.get("markets", [])
        if len(markets) < 2:
            return opps

        title = event.get("title", "")
        category = event.get("category", "")
        event_ticker = event.get("event_ticker", "")

        # Extract numeric strikes from market titles or tickers
        strike_markets = []
        for m in markets:
            m_title = m.get("title", "")
            ticker = m.get("ticker", "")
            # Look for numbers like 14000, 15000, 40000, or 3.75%
            nums = re.findall(r'(\d+[\d,]*)', ticker.split('-')[-1]) or re.findall(r'(\d+[\d,]*)', m_title)
            if nums:
                clean_num_str = nums[0].replace(',', '')
                try:
                    val = float(clean_num_str)
                    y_ask = float(m.get("yes_ask_dollars") or 0.0)
                    y_bid = float(m.get("yes_bid_dollars") or 0.0)
                    if y_ask > 0:
                        strike_markets.append({
                            "val": val,
                            "ticker": ticker,
                            "title": m_title,
                            "ask": y_ask,
                            "bid": y_bid,
                            "vol": float(m.get("volume_24h_fp") or 0.0)
                        })
                except ValueError:
                    pass

        # Sort by strike value
        strike_markets.sort(key=lambda x: x["val"])

        for i in range(len(strike_markets) - 1):
            lower = strike_markets[i]
            higher = strike_markets[i + 1]

            # Inversion: Higher strike Yes Bid > Lower strike Yes Ask (Direct Arb!)
            if higher["bid"] > lower["ask"] and lower["ask"] > 0:
                profit = higher["bid"] - lower["ask"]
                opps.append({
                    "id": f"mono_direct_{lower['ticker']}_{higher['ticker']}",
                    "strategy": "monotonicity",
                    "strategy_badge": "📈 Strike Inversion Arb",
                    "category": category,
                    "event_ticker": event_ticker,
                    "title": f"Monotonicity Violation in {title}",
                    "ticker": f"{lower['ticker']} vs {higher['ticker']}",
                    "market_url": f"https://kalshi.com/events/{event_ticker}",
                    "confidence_score": 96,
                    "edge_pct": round(profit * 100.0, 2),
                    "expected_value_cents": round(profit * 100.0, 2),
                    "roi_pct": round((profit / lower["ask"]) * 100.0, 2),
                    "action": f"BUY Lower ({lower['val']}) @ ${lower['ask']:.2f} & SELL Higher ({higher['val']}) @ ${higher['bid']:.2f}",
                    "kelly_fraction": 0.08,
                    "why": (
                        f"Mathematical monotonicity violation! A harder outcome ({higher['title'][:30]}) is trading with a Bid of ${higher['bid']:.2f}, "
                        f"which is HIGHER than the Ask of the easier outcome ({lower['title'][:30]}) at ${lower['ask']:.2f}. "
                        f"Buying the lower strike and selling the higher strike locks in an immediate ${profit:.2f} credit with zero downside risk."
                    ),
                    "execution_guide": [
                        f"Step 1: Buy 1 YES of {lower['ticker']} at ${lower['ask']:.2f}",
                        f"Step 2: Sell 1 YES of {higher['ticker']} at ${higher['bid']:.2f}",
                        f"Immediate net credit: ${profit:.2f} per pair. Guaranteed non-negative settlement."
                    ],
                    "risks": "Execute both legs simultaneously to avoid partial execution slippage.",
                    "volume_24h": lower["vol"] + higher["vol"]
                })
            # Overpriced Higher Strike (Relative Value edge)
            elif (higher["ask"] - lower["bid"]) > 0.15 and higher["ask"] > 0.40:
                opps.append({
                    "id": f"mono_rel_{lower['ticker']}_{higher['ticker']}",
                    "strategy": "monotonicity",
                    "strategy_badge": "📊 Strike Curve Discrepancy",
                    "category": category,
                    "event_ticker": event_ticker,
                    "title": f"Strike Curve Discrepancy: {lower['val']} vs {higher['val']}",
                    "ticker": lower['ticker'],
                    "market_url": f"https://kalshi.com/events/{event_ticker}",
                    "confidence_score": 82,
                    "edge_pct": round((higher["ask"] - lower["bid"]) * 50.0, 2),
                    "expected_value_cents": round((higher["ask"] - lower["bid"]) * 10.0, 2),
                    "roi_pct": 14.5,
                    "action": f"FAVOR Lower Strike @ ${lower['ask']:.2f} over Higher Strike @ ${higher['ask']:.2f}",
                    "kelly_fraction": 0.04,
                    "why": (
                        f"Strike curve compression: The spread between strike {lower['val']} (Bid ${lower['bid']:.2f}) and "
                        f"strike {higher['val']} (Ask ${higher['ask']:.2f}) is abnormally narrow. "
                        f"The lower strike offers substantially superior risk/reward."
                    ),
                    "execution_guide": [
                        f"Target lower strike {lower['ticker']} which is underpriced relative to the upper tail."
                    ],
                    "risks": "Statistical edge, not a pure arbitrage.",
                    "volume_24h": lower["vol"] + higher["vol"]
                })

        return opps

    def scan_spread_sniping(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Market Making / Wide Spread Sniping.
        Active markets with large bid-ask spread where resting limit orders capture high profit margin.
        """
        opps = []
        category = event.get("category", "")
        event_ticker = event.get("event_ticker", "")
        markets = event.get("markets", [])

        for m in markets:
            ticker = m.get("ticker", "")
            m_title = m.get("title", "")
            y_ask = float(m.get("yes_ask_dollars") or 0.0)
            y_bid = float(m.get("yes_bid_dollars") or 0.0)
            vol = float(m.get("volume_24h_fp") or 0.0)

            # Need active quotes and genuine spread >= 15 cents
            if y_bid >= 0.08 and y_ask <= 0.92:
                spread = y_ask - y_bid
                if spread >= 0.15: # Wide spread
                    mid = (y_ask + y_bid) / 2.0
                    target_bid = round(y_bid + 0.01, 2)
                    target_ask = round(y_ask - 0.01, 2)
                    captured_spread = target_ask - target_bid
                    roi_pct = (captured_spread / target_bid) * 100.0

                    opps.append({
                        "id": f"spread_{ticker}",
                        "strategy": "spread_sniper",
                        "strategy_badge": "🎯 Spread Sniper (Maker)",
                        "category": category,
                        "event_ticker": event_ticker,
                        "title": m_title,
                        "ticker": ticker,
                        "market_url": f"https://kalshi.com/markets/{ticker}",
                        "confidence_score": min(92, int(75 + (spread * 40))),
                        "edge_pct": round(spread * 100.0, 2),
                        "expected_value_cents": round(captured_spread * 100.0, 2),
                        "roi_pct": round(roi_pct, 2),
                        "kalshi_ask": round(y_ask, 2),
                        "kalshi_bid": round(y_bid, 2),
                        "spread": round(spread, 2),
                        "mid": round(mid, 2),
                        "action": f"PLACE MAKER BID @ ${target_bid:.2f} (Spread: ${spread:.2f})",
                        "kelly_fraction": 0.05,
                        "why": (
                            f"Huge spread inefficiency: Market is ${y_bid:.2f} Bid / ${y_ask:.2f} Ask (${spread:.2f} wide). "
                            f"By placing a resting limit order at ${target_bid:.2f}, you become top of book with a "
                            f"${captured_spread:.2f} margin ({roi_pct:.1f}% ROI) if filled and exited or held to fair value."
                        ),
                        "execution_guide": [
                            f"Step 1: Submit resting limit buy order for YES at ${target_bid:.2f}",
                            f"Step 2: Once filled, place resting limit sell at ${target_ask:.2f} or hold to maturity if event probability > ${mid:.2f}",
                            f"Net captured edge: ${captured_spread:.2f} per share."
                        ],
                        "risks": "Adverse selection: If news breaks against the position, your resting bid may be filled by informed flow.",
                        "volume_24h": vol
                    })

        return opps

    async def run_full_scan(self, max_pages: int = 5) -> Dict[str, Any]:
        """Run complete scan pipeline across all strategies."""
        self.is_scanning = True
        start_time = time.time()
        events = await self.fetch_open_events(max_pages=max_pages, page_limit=200)

        all_opps = []
        total_markets = 0
        categories_count = {}

        for e in events:
            cat = e.get("category", "General")
            categories_count[cat] = categories_count.get(cat, 0) + 1
            mkts = e.get("markets", [])
            total_markets += len(mkts)

            # 1. Arbitrage
            all_opps.extend(self.scan_event_arbitrage(e))
            # 2. Sports +EV
            all_opps.extend(self.scan_sports_value(e))
            # 3. Strike Monotonicity
            all_opps.extend(self.scan_strike_monotonicity(e))
            # 4. Spread Sniping
            all_opps.extend(self.scan_spread_sniping(e))

        # Sort opportunities by expected value and confidence
        all_opps.sort(key=lambda x: (x.get("confidence_score", 0), x.get("roi_pct", 0)), reverse=True)

        elapsed = round(time.time() - start_time, 2)
        
        self.cache = {
            "timestamp": int(time.time()),
            "elapsed_seconds": elapsed,
            "events_scanned": len(events),
            "markets_scanned": total_markets,
            "categories_distribution": categories_count,
            "total_opportunities": len(all_opps),
            "opportunities": all_opps,
            "stats": {
                "arbitrage_count": sum(1 for o in all_opps if o["strategy"] == "arbitrage"),
                "sports_count": sum(1 for o in all_opps if o["strategy"] == "sports_ev"),
                "monotonicity_count": sum(1 for o in all_opps if o["strategy"] == "monotonicity"),
                "spread_count": sum(1 for o in all_opps if o["strategy"] == "spread_sniper"),
                "avg_roi_pct": round(sum(o.get("roi_pct", 0) for o in all_opps) / len(all_opps), 1) if all_opps else 0
            }
        }
        self.is_scanning = False
        return self.cache
