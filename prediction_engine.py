import math
import time
from typing import Dict, Any, List, Tuple, Optional
import numpy as np
from phoenix.otel import register

# Initialize Phoenix OpenInference tracer
tracer_provider = register(project_name="kalshi-multi-coin-bot")
tracer = tracer_provider.get_tracer(__name__)

def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (CDF)."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def normal_pdf(x: float) -> float:
    """Standard normal probability density function (PDF)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def calculate_kalshi_fee(contracts: int, price: float) -> float:
    """
    Official Kalshi Taker Fee Schedule:
    fee = ceil(0.07 * count * price * (1.0 - price) * 100) / 100
    Where price is in dollars [0.01 - 0.99] and rounded UP to the nearest cent.
    Settlement at expiration carries $0.00 fee.
    """
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * float(contracts) * float(price) * (1.0 - float(price)) * 100.0) / 100.0

class QuantPredictionEngine:
    """
    Quantitative Pricing & Prediction Engine for Kalshi Bitcoin Contracts.
    Features:
    - Black-Scholes Binary Option Pricing (Above, Below, Range)
    - Vectorized Merton Jump-Diffusion Monte Carlo Simulation (10,000+ paths)
    - Closed-form One-Touch Barrier Models (Min/Max touch contracts)
    - 15-Minute Intraday Drift-Diffusion High-Frequency Model
    - Monotonicity & Inversion Arbitrage Detector
    - Net Expected Value (EV), Fee accounting, and Half-Kelly Staking
    """
    def __init__(self, risk_free_rate: float = 0.045, taker_fee_cents: float = 0.5):
        self.r = risk_free_rate
        self.taker_fee = taker_fee_cents / 100.0

    def black_scholes_binary(
        self,
        spot: float,
        strike: float,
        time_to_expiry_years: float,
        volatility: float,
        drift: Optional[float] = None,
        strike_type: str = "greater"
    ) -> float:
        """
        Closed-form Black-Scholes Binary Option Pricing Model.
        Returns the risk-neutral (or physical drift) probability of expiring in the money.
        """
        if time_to_expiry_years <= 1e-7:
            if strike_type == "greater":
                return 1.0 if spot >= strike else 0.0
            elif strike_type == "less":
                return 1.0 if spot <= strike else 0.0
            return 0.5

        if volatility <= 1e-4:
            volatility = 0.10

        mu = drift if drift is not None else self.r
        d1 = (math.log(spot / strike) + (mu + 0.5 * volatility ** 2) * time_to_expiry_years) / (volatility * math.sqrt(time_to_expiry_years))
        d2 = d1 - volatility * math.sqrt(time_to_expiry_years)

        prob_above = normal_cdf(d2)

        if strike_type == "greater":
            return prob_above
        elif strike_type == "less":
            return 1.0 - prob_above
        return prob_above

    def black_scholes_range(
        self,
        spot: float,
        floor_strike: float,
        cap_strike: float,
        time_to_expiry_years: float,
        volatility: float,
        drift: Optional[float] = None
    ) -> float:
        """Calculate probability of spot finishing between floor and cap: P(floor <= S_T <= cap)."""
        p_above_floor = self.black_scholes_binary(spot, floor_strike, time_to_expiry_years, volatility, drift, "greater")
        p_above_cap = self.black_scholes_binary(spot, cap_strike, time_to_expiry_years, volatility, drift, "greater")
        return max(0.0, p_above_floor - p_above_cap)

    def one_touch_barrier_down(
        self,
        spot: float,
        barrier: float,
        time_to_expiry_years: float,
        volatility: float,
        drift: Optional[float] = None
    ) -> float:
        """
        Closed-form analytical probability of touching a lower barrier before expiry:
        P(min_{0<=t<=T} S_t <= Barrier).
        Used for KXBTCMINY (Yearly Floors) and KXBTCMINMON (Monthly Floors).
        """
        if spot <= barrier:
            return 1.0
        if time_to_expiry_years <= 1e-6:
            return 0.0

        mu = drift if drift is not None else self.r
        sigma = max(0.05, volatility)
        t = time_to_expiry_years

        num1 = math.log(barrier / spot) - (mu - 0.5 * sigma ** 2) * t
        denom = sigma * math.sqrt(t)
        term1 = normal_cdf(num1 / denom)

        power = (2.0 * mu / (sigma ** 2)) - 1.0
        num2 = math.log(barrier / spot) + (mu - 0.5 * sigma ** 2) * t
        term2 = ((barrier / spot) ** power) * normal_cdf(num2 / denom)

        prob = term1 + term2
        return min(1.0, max(0.0, prob))

    def one_touch_barrier_up(
        self,
        spot: float,
        barrier: float,
        time_to_expiry_years: float,
        volatility: float,
        drift: Optional[float] = None
    ) -> float:
        """
        Closed-form analytical probability of touching or exceeding an upper barrier before expiry:
        P(max_{0<=t<=T} S_t >= Barrier).
        Used for KXBTCMAXY (Yearly Highs) and KXBTCMAX100 / Milestone races.
        """
        if spot >= barrier:
            return 1.0
        if time_to_expiry_years <= 1e-6:
            return 0.0

        mu = drift if drift is not None else self.r
        sigma = max(0.05, volatility)
        t = time_to_expiry_years

        num1 = -math.log(barrier / spot) + (mu - 0.5 * sigma ** 2) * t
        denom = sigma * math.sqrt(t)
        term1 = normal_cdf(num1 / denom)

        power = (2.0 * mu / (sigma ** 2)) - 1.0
        num2 = -math.log(barrier / spot) - (mu - 0.5 * sigma ** 2) * t
        term2 = ((barrier / spot) ** power) * normal_cdf(num2 / denom)

        prob = term1 + term2
        return min(1.0, max(0.0, prob))

    def run_monte_carlo_jump_diffusion(
        self,
        spot: float,
        time_to_expiry_years: float,
        volatility: float,
        drift: float = 0.045,
        jump_lambda: float = 2.0,
        jump_mean: float = 0.0,
        jump_std: float = 0.02,
        num_paths: int = 10000,
        strikes: Optional[List[float]] = None
    ) -> Dict[str, Any]:
        """
        Vectorized Merton Jump-Diffusion Monte Carlo Simulation.
        Runs 10,000+ paths in NumPy, returns terminal price distribution,
        percentiles, histogram data for UI charts, and empirical strike probabilities.
        """
        t = max(1e-5, time_to_expiry_years)
        dt = t

        # Standard Brownian motion component
        z = np.random.standard_normal(num_paths)

        # Poisson jump process
        k = math.exp(jump_mean + 0.5 * jump_std ** 2) - 1.0
        num_jumps = np.random.poisson(jump_lambda * dt, num_paths)
        jumps = np.random.normal(jump_mean, jump_std, num_paths) * num_jumps

        # Adjusted drift for jump compensation
        drift_adj = (drift - 0.5 * volatility ** 2 - jump_lambda * k) * dt
        log_st = np.log(spot) + drift_adj + volatility * np.sqrt(dt) * z + jumps
        st = np.exp(log_st)

        # Calculate percentiles
        p1, p5, p10, p25, p50, p75, p90, p95, p99 = np.percentile(st, [1, 5, 10, 25, 50, 75, 90, 95, 99])

        # Generate 20-bin histogram for interactive probability density visualization
        hist_counts, bin_edges = np.histogram(st, bins=24)
        bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
        hist_densities = (hist_counts / num_paths).tolist()

        strike_probs = {}
        if strikes:
            for k_val in strikes:
                prob = float(np.mean(st >= k_val))
                strike_probs[k_val] = round(prob, 4)

        return {
            "spot": spot,
            "paths_count": num_paths,
            "time_years": t,
            "percentiles": {
                "p1": round(float(p1), 1),
                "p5": round(float(p5), 1),
                "p10": round(float(p10), 1),
                "p25": round(float(p25), 1),
                "median": round(float(p50), 1),
                "p75": round(float(p75), 1),
                "p90": round(float(p90), 1),
                "p95": round(float(p95), 1),
                "p99": round(float(p99), 1),
            },
            "mean_terminal": round(float(np.mean(st)), 1),
            "std_terminal": round(float(np.std(st)), 1),
            "histogram": {
                "bins": [round(float(b), 1) for b in bin_centers],
                "probabilities": [round(float(p), 4) for p in hist_densities]
            },
            "strike_probs": strike_probs
        }

    @tracer.agent
    def evaluate_15m_brti_accumulator(
        self,
        spot: float,
        open_price: float,
        seconds_left: float,
        accumulated_samples: Optional[List[float]] = None,
        dollar_vol_15m: float = 95.0,
        momentum_bias: float = 0.0,
        market_data: Optional[Dict[str, Any]] = None,
        hypothesis: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        CF Benchmarks BRTI 60-Second Simple Average Accumulator for the final minute
        of KXBTC15M contracts.
        
        Kalshi KXBTC15M settles on the 60-second simple arithmetic average of 1-second
        spot samples during the final 60 seconds (seconds_left <= 60).
        
        Variance collapses at cubic rate:
        Var(S_settle | F_m) = (1/3) * (sigma_1s^2) * (T_rem^3) / 3600
        """
        if market_data:
            spot = market_data.get("spot", spot)
            open_price = market_data.get("open_price", open_price)
            seconds_left = market_data.get("seconds_left", seconds_left)
            dollar_vol_15m = market_data.get("dollar_vol_15m", dollar_vol_15m)
            momentum_bias = market_data.get("momentum_bias", momentum_bias)
            accumulated_samples = market_data.get("accumulated_samples", accumulated_samples)

        samples = list(accumulated_samples or [])
        m = len(samples)
        # Ensure m + t_rem == 60 samples total in 60-second settlement window
        if m > 0:
            t_rem = max(0.0, float(60 - m))
            sum_m = sum(samples)
        else:
            t_rem = max(0.0, min(60.0, float(seconds_left)))
            sum_m = (60.0 - t_rem) * spot

        # Conditional expected final settlement price under martingale dynamics
        mu_settle = (sum_m + t_rem * spot) / 60.0

        # 1-second dollar standard deviation derived from 15m volatility
        sigma_1s = max(1.0, dollar_vol_15m / 30.0)

        # Conditional standard deviation of the 60-second arithmetic average
        if t_rem > 0.0:
            var_settle = (1.0 / 3.0) * (sigma_1s ** 2) * (t_rem ** 3) / 3600.0
            sigma_settle = max(0.001, math.sqrt(var_settle))
        else:
            sigma_settle = 0.001

        drift_dollar = momentum_bias * (t_rem / 900.0)
        z_score = (mu_settle + drift_dollar - open_price) / sigma_settle

        prob_yes = normal_cdf(z_score)
        prob_no = 1.0 - prob_yes
        delta = spot - open_price

        computed_hypothesis = hypothesis or (
            "BULLISH_YES" if prob_yes >= 0.60 else ("BEARISH_NO" if prob_no >= 0.60 else "NEUTRAL_HOLD")
        )
        resolved_market_data = market_data or {
            "spot": spot,
            "open_price": open_price,
            "seconds_left": int(seconds_left),
            "dollar_vol_15m": dollar_vol_15m,
            "momentum_bias": momentum_bias
        }

        return {
            "delta": round(delta, 2),
            "delta_pct": round((delta / (open_price + 1e-9)) * 100.0, 3),
            "seconds_left": int(seconds_left),
            "time_elapsed_pct": round(((900.0 - seconds_left) / 900.0) * 100.0, 1),
            "z_score": round(z_score, 2),
            "prob_yes": round(prob_yes, 4),
            "prob_yes_pct": round(prob_yes * 100.0, 1),
            "prob_no": round(prob_no, 4),
            "prob_no_pct": round(prob_no * 100.0, 1),
            "hypothesis": computed_hypothesis,
            "market_data": resolved_market_data,
            "mu_settle": round(mu_settle, 2),
            "sigma_settle": round(sigma_settle, 2),
            "accumulated_samples_count": m,
            "is_brti_accumulator": True
        }

    @tracer.agent
    def evaluate_15m_scalp(
        self,
        spot: float,
        open_price: float,
        seconds_left: float,
        dollar_vol_15m: float,
        momentum_bias: float = 0.0,
        accumulated_samples: Optional[List[float]] = None,
        market_data: Optional[Dict[str, Any]] = None,
        hypothesis: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        High-frequency Drift-Diffusion model for 15-minute Kalshi candlestick contracts (KXBTC15M).
        In the final 60 seconds, automatically routes to the CF Benchmarks BRTI 60-second accumulator
        where variance collapses proportionally.
        """
        if market_data:
            spot = market_data.get("spot", spot)
            open_price = market_data.get("open_price", open_price)
            seconds_left = market_data.get("seconds_left", seconds_left)
            dollar_vol_15m = market_data.get("dollar_vol_15m", dollar_vol_15m)
            momentum_bias = market_data.get("momentum_bias", momentum_bias)
            accumulated_samples = market_data.get("accumulated_samples", accumulated_samples)

        if seconds_left <= 60.0:
            return self.evaluate_15m_brti_accumulator(
                spot=spot,
                open_price=open_price,
                seconds_left=seconds_left,
                accumulated_samples=accumulated_samples,
                dollar_vol_15m=dollar_vol_15m,
                momentum_bias=momentum_bias,
                market_data=market_data,
                hypothesis=hypothesis
            )

        delta = spot - open_price
        delta_pct = (delta / (open_price + 1e-9)) * 100.0
        time_fraction = max(0.001, min(1.0, seconds_left / 900.0))

        # Time-decayed standard deviation
        vol_adjusted = max(0.0001, dollar_vol_15m * math.sqrt(time_fraction))
        
        # Incorporate short-term momentum bias
        drift_dollar = momentum_bias * (seconds_left / 900.0)
        z_score = (delta + drift_dollar) / vol_adjusted

        prob_yes = normal_cdf(z_score)
        prob_no = 1.0 - prob_yes

        computed_hypothesis = hypothesis or (
            "BULLISH_YES" if prob_yes >= 0.60 else ("BEARISH_NO" if prob_no >= 0.60 else "NEUTRAL_HOLD")
        )
        resolved_market_data = market_data or {
            "spot": spot,
            "open_price": open_price,
            "seconds_left": int(seconds_left),
            "dollar_vol_15m": dollar_vol_15m,
            "momentum_bias": momentum_bias
        }

        return {
            "delta": round(delta, 2),
            "delta_pct": round(delta_pct, 3),
            "seconds_left": int(seconds_left),
            "time_elapsed_pct": round(((900.0 - seconds_left) / 900.0) * 100.0, 1),
            "z_score": round(z_score, 2),
            "prob_yes": round(prob_yes, 4),
            "prob_yes_pct": round(prob_yes * 100.0, 1),
            "prob_no": round(prob_no, 4),
            "prob_no_pct": round(prob_no * 100.0, 1),
            "hypothesis": computed_hypothesis,
            "market_data": resolved_market_data,
            "is_brti_accumulator": False
        }

    @tracer.agent
    def evaluate_market_prediction(
        self,
        market_data: Dict[str, Any],
        hypothesis: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Agent-level prediction logic evaluated and traced via Phoenix/OpenInference.
        Accepts market data and hypothesis, returning quantitative evaluation and predicted edge.
        """
        spot = market_data.get("spot", 0.0)
        open_p = market_data.get("open_price", spot)
        sec_left = market_data.get("seconds_left", 900.0)
        vol = market_data.get("dollar_vol_15m", 95.0)
        mom = market_data.get("momentum_bias", 0.0)
        samples = market_data.get("accumulated_samples", [])

        return self.evaluate_15m_scalp(
            spot=spot,
            open_price=open_p,
            seconds_left=sec_left,
            dollar_vol_15m=vol,
            momentum_bias=mom,
            accumulated_samples=samples,
            market_data=market_data,
            hypothesis=hypothesis
        )

    def calculate_trade_metrics(
        self,
        model_prob: float,
        yes_ask: float,
        yes_bid: float,
        no_ask: float,
        no_bid: float,
        slippage_penalty_cents: float = 0.0
    ) -> Dict[str, Any]:
        """
        Calculate mathematical edge, net post-fee expected value (EV),
        and Net Post-Fee Half-Kelly position sizing.
        
        Deducts:
        - 3¢ adverse market execution slippage penalty
        - Official Kalshi non-linear quadratic taker fee schedule
        """
        eff_yes_ask = yes_ask if yes_ask > 0 else (1.0 - no_bid if no_bid > 0 else 0.99)
        eff_no_ask = no_ask if no_ask > 0 else (1.0 - yes_bid if yes_bid > 0 else 0.99)
        slip = slippage_penalty_cents / 100.0

        # 1. Buy YES Evaluation (with 3c adverse slippage + Kalshi taker fee)
        fill_yes_ask = min(0.99, eff_yes_ask + slip)
        fee_yes_in = calculate_kalshi_fee(1, fill_yes_ask)
        net_cost_yes = fill_yes_ask + fee_yes_in
        # Settlement at expiry carries $0 fee; net win = $1.00 - net_cost_yes
        net_win_yes = 1.0 - net_cost_yes
        b_yes = net_win_yes / max(0.01, net_cost_yes)

        ev_yes_cents = (model_prob * 1.0 - net_cost_yes) * 100.0
        edge_yes = model_prob - net_cost_yes
        roi_yes_pct = (ev_yes_cents / (net_cost_yes * 100.0)) * 100.0 if net_cost_yes > 0 else 0.0

        # Net Half-Kelly for YES
        q_yes = 1.0 - model_prob
        kelly_yes_full = max(0.0, (model_prob * b_yes - q_yes) / b_yes) if b_yes > 0 else 0.0
        half_kelly_yes = kelly_yes_full * 0.5

        # 2. Buy NO Evaluation (with 3c adverse slippage + Kalshi taker fee)
        model_prob_no = 1.0 - model_prob
        fill_no_ask = min(0.99, eff_no_ask + slip)
        fee_no_in = calculate_kalshi_fee(1, fill_no_ask)
        net_cost_no = fill_no_ask + fee_no_in
        net_win_no = 1.0 - net_cost_no
        b_no = net_win_no / max(0.01, net_cost_no)

        ev_no_cents = (model_prob_no * 1.0 - net_cost_no) * 100.0
        edge_no = model_prob_no - net_cost_no
        roi_no_pct = (ev_no_cents / (net_cost_no * 100.0)) * 100.0 if net_cost_no > 0 else 0.0

        # Net Half-Kelly for NO
        q_no = 1.0 - model_prob_no
        kelly_no_full = max(0.0, (model_prob_no * b_no - q_no) / b_no) if b_no > 0 else 0.0
        half_kelly_no = kelly_no_full * 0.5

        # Spread & Maker Edge
        spread_cents = round(abs(eff_yes_ask - yes_bid) * 100.0, 1)

        # Determine optimal recommendation strictly after fees & slippage
        if edge_yes >= 0.04 and ev_yes_cents >= 1.5:
            signal = "🟢 STRONG BUY YES"
            side = "YES"
            best_edge = edge_yes
            best_ev = ev_yes_cents
            best_roi = roi_yes_pct
            best_half_kelly = half_kelly_yes
            best_full_kelly = kelly_yes_full
            price = fill_yes_ask
        elif edge_yes >= 0.02 and ev_yes_cents >= 0.5:
            signal = "🟢 BUY YES"
            side = "YES"
            best_edge = edge_yes
            best_ev = ev_yes_cents
            best_roi = roi_yes_pct
            best_half_kelly = half_kelly_yes
            best_full_kelly = kelly_yes_full
            price = fill_yes_ask
        elif edge_no >= 0.04 and ev_no_cents >= 1.5:
            signal = "🔴 STRONG BUY NO"
            side = "NO"
            best_edge = edge_no
            best_ev = ev_no_cents
            best_roi = roi_no_pct
            best_half_kelly = half_kelly_no
            best_full_kelly = kelly_no_full
            price = fill_no_ask
        elif edge_no >= 0.02 and ev_no_cents >= 0.5:
            signal = "🔴 BUY NO"
            side = "NO"
            best_edge = edge_no
            best_ev = ev_no_cents
            best_roi = roi_no_pct
            best_half_kelly = half_kelly_no
            best_full_kelly = kelly_no_full
            price = fill_no_ask
        elif spread_cents >= 15.0:
            signal = "🎯 SPREAD SNIPER"
            side = "MAKER"
            best_edge = spread_cents / 100.0
            best_ev = round(spread_cents * 0.4, 1)
            best_roi = 12.0
            best_half_kelly = 0.03
            best_full_kelly = 0.06
            price = round((eff_yes_ask + yes_bid) / 2.0, 2)
        else:
            signal = "⚖️ FAIR VALUE"
            side = "NONE"
            best_edge = 0.0
            best_ev = 0.0
            best_roi = 0.0
            best_half_kelly = 0.0
            best_full_kelly = 0.0
            price = eff_yes_ask

        return {
            "signal": signal,
            "side": side,
            "best_edge_pct": round(best_edge * 100.0, 1),
            "best_ev_cents": round(best_ev, 1),
            "best_roi_pct": round(best_roi, 1),
            "half_kelly_pct": round(best_half_kelly * 100.0, 1),
            "full_kelly_pct": round(best_full_kelly * 100.0, 1),
            "price_cents": int(round(price * 100)),
            "spread_cents": spread_cents,
            "edge_yes_pct": round(edge_yes * 100.0, 1),
            "edge_no_pct": round(edge_no * 100.0, 1),
            "ev_yes_cents": round(ev_yes_cents, 1),
            "ev_no_cents": round(ev_no_cents, 1),
            "slippage_deducted_cents": round(slippage_penalty_cents, 1),
            "fee_yes_cents": round(fee_yes_in * 100.0, 1),
            "fee_no_cents": round(fee_no_in * 100.0, 1)
        }


    def detect_monotonicity_violations(self, strike_ladder: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        For a set of 'Above K' binary options with the same expiration,
        prices MUST be non-increasing with strike: K_1 < K_2 => P(Above K_1) >= P(Above K_2).
        Flags any strike inversions (guaranteed arbitrage or heavy mispricing).
        """
        violations = []
        if len(strike_ladder) < 2:
            return violations

        # Sort ascending by strike
        sorted_ladder = sorted(strike_ladder, key=lambda x: x.get("strike", 0.0))

        for i in range(len(sorted_ladder) - 1):
            low = sorted_ladder[i]
            high = sorted_ladder[i + 1]

            k_low = low.get("strike", 0.0)
            k_high = high.get("strike", 0.0)

            low_ask = low.get("yes_ask", 0.0)
            high_ask = high.get("yes_ask", 0.0)
            high_bid = high.get("yes_bid", 0.0)

            # Inversion condition: Higher strike is priced more expensive than lower strike
            if high_ask > low_ask and low_ask > 0:
                violations.append({
                    "type": "ASK_INVERSION",
                    "lower_strike": k_low,
                    "higher_strike": k_high,
                    "lower_ask_cents": int(round(low_ask * 100)),
                    "higher_ask_cents": int(round(high_ask * 100)),
                    "diff_cents": int(round((high_ask - low_ask) * 100)),
                    "ticker_low": low.get("ticker"),
                    "ticker_high": high.get("ticker"),
                    "description": f"Strike ${k_high:,.0f} YES is trading at {int(round(high_ask*100))}¢ while easier strike ${k_low:,.0f} YES is only {int(round(low_ask*100))}¢!"
                })

            # Pure arbitrage condition: Higher strike BID > Lower strike ASK
            if high_bid > low_ask and low_ask > 0 and high_bid > 0:
                profit_cents = int(round((high_bid - low_ask - 0.01) * 100))
                violations.append({
                    "type": "PURE_ARBITRAGE",
                    "lower_strike": k_low,
                    "higher_strike": k_high,
                    "lower_ask_cents": int(round(low_ask * 100)),
                    "higher_bid_cents": int(round(high_bid * 100)),
                    "net_profit_cents": profit_cents,
                    "ticker_low": low.get("ticker"),
                    "ticker_high": high.get("ticker"),
                    "description": f"100% Risk-Free Arb: Buy ${k_low:,.0f} at {int(round(low_ask*100))}¢ and sell ${k_high:,.0f} at {int(round(high_bid*100))}¢ for guaranteed {profit_cents}¢ profit!"
                })

        return violations
