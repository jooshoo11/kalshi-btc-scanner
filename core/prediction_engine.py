import math
import time
from typing import Dict, Any, List, Tuple, Optional, Sequence
import numpy as np
import contextlib

# Phoenix/OpenTelemetry tracing DISABLED: no collector on localhost:4317, which caused
# per-second export errors and dashboard lag. No-op tracer keeps decorators working.
class _NoopTracer:
    def agent(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn
    def tool(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn
    def start_as_current_span(self, *args, **kwargs):
        return contextlib.nullcontext()

tracer = _NoopTracer()

def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (CDF)."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def normal_pdf(x: float) -> float:
    """Standard normal probability density function (PDF)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def inverse_normal_cdf(p: float) -> float:
    """Standard normal inverse CDF (probit) via binary search."""
    p_clamped = max(0.005, min(0.995, float(p)))
    lo, hi = -4.0, 4.0
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        if normal_cdf(mid) < p_clamped:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

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


# Precomputed 1-cent lookup table (1..99 cents) for 40-contract batch average non-linear taker fee
# Formula: ceil(0.07 * contracts * price * (1 - price) * 100) / 100 averaged over 40 contracts
KALSHI_40C_FEE_LUT: List[float] = [0.0] * 100

for _c in range(1, 100):
    _p = _c / 100.0
    _raw_cents = 0.07 * 40.0 * _p * (1.0 - _p) * 100.0
    _total_fee = math.ceil(_raw_cents) / 100.0
    KALSHI_40C_FEE_LUT[_c] = _total_fee / 40.0


def lookup_fee_40c(price: float) -> float:
    """
    Precomputed 1-cent lookup table (1..99 cents) at startup for the 40-contract
    batch average non-linear taker fee:
      ceil(0.07 * contracts * price * (1 - price) * 100) / 100 averaged over 40 contracts
    Provides O(1) fee lookups inside high-frequency scanner loops.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    cents = int(round(price * 100.0))
    if 1 <= cents <= 99:
        return KALSHI_40C_FEE_LUT[cents]
    return 0.0


def compute_microstructure_clean_vol(
    tick_history: Sequence[Tuple[float, float]],
    base_vol: float,
    bucket_seconds: float = 3.0,
    clamp_min_mult: float = 0.6,
    clamp_max_mult: float = 2.2,
    blend_weight: float = 0.5,
) -> Tuple[float, float]:
    """
    Reduce 1s tick-to-tick bid-ask bounce and cadence jitter when computing
    realized_15m from tick history using sub-sampled buckets (3s-5s).
    Blends realized_15m with base_vol from config, clamped to [clamp_min_mult * base, clamp_max_mult * base].
    Returns: (eff_vol, realized_15m)
    """
    if not tick_history or len(tick_history) < 2:
        return base_vol, base_vol

    # Sub-sample tick history into buckets separated by at least bucket_seconds
    bucketed: List[Tuple[float, float]] = [tick_history[0]]
    for t, p in tick_history[1:]:
        if (t - bucketed[-1][0]) >= bucket_seconds:
            bucketed.append((t, p))

    # Also capture the most recent tick if at least 1s after the last bucketed tick
    if (tick_history[-1][0] - bucketed[-1][0]) >= 1.0:
        bucketed.append(tick_history[-1])

    if len(bucketed) < 3:
        return base_vol, base_vol

    # Compute variance rate across sub-sampled buckets
    variances = []
    for i in range(1, len(bucketed)):
        dt = max(1.0, bucketed[i][0] - bucketed[i - 1][0])
        dp = bucketed[i][1] - bucketed[i - 1][1]
        variances.append((dp ** 2) / dt)

    if not variances:
        return base_vol, base_vol

    sigma_1s = math.sqrt(sum(variances) / len(variances))
    realized_15m = sigma_1s * 30.0  # sqrt(900s) = 30.0

    eff_vol = blend_weight * base_vol + (1.0 - blend_weight) * realized_15m
    eff_vol = max(base_vol * clamp_min_mult, min(base_vol * clamp_max_mult, eff_vol))
    return eff_vol, realized_15m


def compute_clamped_z_shift(
    lead_imp_bps: float,
    ofi_z: float,
    is_hl_fresh: bool,
    lead_z_scale: float = 0.02,
    ofi_z_scale: float = 0.05,
    max_z_shift: float = 0.35,
    spot_tfi: float = 0.0,
    book_imb: float = 0.0,
    momentum_z: float = 0.0,
    spot_tfi_scale: float = 0.14,
    book_imb_scale: float = 0.06,
    momentum_scale: float = 0.10,
) -> float:
    """
    Combine gated Hyperliquid lead-lag + OFI with US-accessible spot alpha
    (Coinbase/Kraken/Bitstamp aggressive TFI, Coinbase L1 book imbalance, and
    multi-horizon spot momentum) and clamp to [-max_z_shift, +max_z_shift].
    """
    hl_shift = (lead_imp_bps * lead_z_scale + ofi_z * ofi_z_scale) if is_hl_fresh else 0.0
    spot_shift = (
        max(-1.0, min(1.0, spot_tfi)) * spot_tfi_scale
        + max(-1.0, min(1.0, book_imb)) * book_imb_scale
        + max(-1.5, min(1.5, momentum_z)) * momentum_scale
    )
    raw_shift = hl_shift + spot_shift
    return max(-max_z_shift, min(max_z_shift, raw_shift))


def detect_stale_quote_impulse(
    spot_5s_delta: float,
    kalshi_5s_delta_cents: float,
    spot_tfi: float,
    spot_thresh: float = 40.0,
    max_kalshi_move_cents: float = 1.0,
) -> Dict[str, Any]:
    """
    Empirical 5s Stale-Quote Impulse Detector (calibrated on recorded tick data):
    Detects when spot moves >= `spot_thresh` in 5s while Kalshi mid moved < `max_kalshi_move_cents`,
    and checks whether aggressive Spot TFI confirms the impulse direction.
    """
    is_stale = abs(kalshi_5s_delta_cents) < max_kalshi_move_cents
    if not is_stale or abs(spot_5s_delta) < spot_thresh:
        return {
            "stale_impulse": "NONE",
            "d_spot_5s": round(spot_5s_delta, 2),
            "d_kalshi_5s_cents": round(kalshi_5s_delta_cents, 2),
            "tfi_confirmed": False,
            "z_boost": 0.0,
        }

    is_up = spot_5s_delta > 0
    tfi_confirmed = (spot_tfi > 0.05) if is_up else (spot_tfi < -0.05)
    direction = "BULLISH_YES" if is_up else "BEARISH_NO"
    z_boost = (0.12 if is_up else -0.12) if tfi_confirmed else 0.0
    return {
        "stale_impulse": direction,
        "d_spot_5s": round(spot_5s_delta, 2),
        "d_kalshi_5s_cents": round(kalshi_5s_delta_cents, 2),
        "tfi_confirmed": tfi_confirmed,
        "z_boost": z_boost,
    }


class SignalHysteresisManager:
    """
    Signal hysteresis/hold rules to ensure marginal BTC/ETH signals near
    60% probability or 4.0% net edge do not flicker between
    BULLISH_YES/BEARISH_NO and NEUTRAL_HOLD every second.
    """

    def __init__(
        self,
        min_prob_high: float = 60.0,
        min_edge_high: float = 4.0,
        hysteresis_prob_pct: float = 2.0,
        hysteresis_edge_pct: float = 0.5,
    ):
        self.min_prob_high = min_prob_high
        self.min_edge_high = min_edge_high
        self.hysteresis_prob_pct = hysteresis_prob_pct
        self.hysteresis_edge_pct = hysteresis_edge_pct
        self._states: Dict[str, str] = {}
        self._hold_counts: Dict[str, int] = {}

    def get_hypothesis(
        self,
        coin: str,
        prob_yes_pct: float,
        prob_no_pct: float,
        edge_y: float,
        edge_n: float,
    ) -> str:
        c = coin.upper()
        current = self._states.get(c, "NEUTRAL_HOLD")

        prob_low = self.min_prob_high - self.hysteresis_prob_pct  # e.g., 58.0%
        edge_low = self.min_edge_high - self.hysteresis_edge_pct  # e.g., 3.5%

        if current == "BULLISH_YES":
            # Maintain BULLISH_YES within hysteresis band unless opposite side triggers cleanly
            if prob_no_pct >= self.min_prob_high and edge_n >= self.min_edge_high:
                self._states[c] = "BEARISH_NO"
                self._hold_counts[c] = 1
                return "BEARISH_NO"
            if prob_yes_pct >= prob_low and edge_y >= edge_low:
                self._hold_counts[c] = self._hold_counts.get(c, 0) + 1
                return "BULLISH_YES"
            self._states[c] = "NEUTRAL_HOLD"
            self._hold_counts[c] = 1
            return "NEUTRAL_HOLD"

        elif current == "BEARISH_NO":
            # Maintain BEARISH_NO within hysteresis band unless opposite side triggers cleanly
            if prob_yes_pct >= self.min_prob_high and edge_y >= self.min_edge_high:
                self._states[c] = "BULLISH_YES"
                self._hold_counts[c] = 1
                return "BULLISH_YES"
            if prob_no_pct >= prob_low and edge_n >= edge_low:
                self._hold_counts[c] = self._hold_counts.get(c, 0) + 1
                return "BEARISH_NO"
            self._states[c] = "NEUTRAL_HOLD"
            self._hold_counts[c] = 1
            return "NEUTRAL_HOLD"

        else:
            # NEUTRAL_HOLD requires meeting full upper thresholds
            if prob_yes_pct >= self.min_prob_high and edge_y >= self.min_edge_high and prob_yes_pct >= prob_no_pct:
                self._states[c] = "BULLISH_YES"
                self._hold_counts[c] = 1
                return "BULLISH_YES"
            elif prob_no_pct >= self.min_prob_high and edge_n >= self.min_edge_high and prob_no_pct > prob_yes_pct:
                self._states[c] = "BEARISH_NO"
                self._hold_counts[c] = 1
                return "BEARISH_NO"
            else:
                self._states[c] = "NEUTRAL_HOLD"
                self._hold_counts[c] = self._hold_counts.get(c, 0) + 1
                return "NEUTRAL_HOLD"

    def reset(self, coin: Optional[str] = None) -> None:
        if coin:
            self._states.pop(coin.upper(), None)
            self._hold_counts.pop(coin.upper(), None)
        else:
            self._states.clear()
            self._hold_counts.clear()


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
    def __init__(self, risk_free_rate: float = 0.045, taker_fee_cents: float = 0.5, rng: Optional[np.random.Generator] = None):
        self.r = risk_free_rate
        self.taker_fee = taker_fee_cents / 100.0
        self._rng = rng or np.random.default_rng()

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

        # Thread-safe NumPy Generator (avoids legacy RNG lock contention)
        rng = getattr(self, "_rng", None) or np.random.default_rng()

        # Standard Brownian motion component
        z = rng.standard_normal(num_paths)

        # Poisson jump process
        k = math.exp(jump_mean + 0.5 * jump_std ** 2) - 1.0
        num_jumps = rng.poisson(jump_lambda * dt, num_paths)
        jumps = rng.normal(jump_mean, jump_std, num_paths) * num_jumps

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
            strikes_arr = np.asarray(strikes, dtype=np.float64)
            # Vectorized broadcast: (num_strikes, num_paths)
            probs = np.mean(st[None, :] >= strikes_arr[:, None], axis=1)
            strike_probs = {k_val: round(float(p), 4) for k_val, p in zip(strikes, probs)}

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
        
        Variance of the remaining average decays cubically in remaining samples
        because spot follows a correlated Brownian random walk:
        Var(S_settle | F_m) = sigma_1s^2 * k*(k+1)*(2k+1) / 21600.
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
        # Use 0.001 floor (not 1.0) so lower-priced assets like ETH ($5.5/15m -> $0.183/s) are not distorted.
        sigma_1s = max(0.001, dollar_vol_15m / 30.0)

        # Conditional standard deviation of the 60-second arithmetic average.
        # Spot is a correlated Brownian random walk (not iid): Cov(S_{t+j}, S_{t+l}) = min(j,l)*sigma_1s^2.
        # Exact discrete sum over k remaining ticks gives Var = sigma_1s^2 * k*(k+1)*(2k+1) / 21600.
        if t_rem > 0.0:
            var_settle = (sigma_1s ** 2) * t_rem * (t_rem + 1.0) * (2.0 * t_rem + 1.0) / 21600.0
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
        if model_prob >= 0.5 and edge_yes >= 0.04 and ev_yes_cents >= 1.5:
            signal = "🟢 STRONG BUY YES"
            side = "YES"
            best_edge = edge_yes
            best_ev = ev_yes_cents
            best_roi = roi_yes_pct
            best_half_kelly = half_kelly_yes
            best_full_kelly = kelly_yes_full
            price = fill_yes_ask
        elif model_prob >= 0.5 and edge_yes >= 0.02 and ev_yes_cents >= 0.5:
            signal = "🟢 BUY YES"
            side = "YES"
            best_edge = edge_yes
            best_ev = ev_yes_cents
            best_roi = roi_yes_pct
            best_half_kelly = half_kelly_yes
            best_full_kelly = kelly_yes_full
            price = fill_yes_ask
        elif model_prob_no >= 0.5 and edge_no >= 0.04 and ev_no_cents >= 1.5:
            signal = "🔴 STRONG BUY NO"
            side = "NO"
            best_edge = edge_no
            best_ev = ev_no_cents
            best_roi = roi_no_pct
            best_half_kelly = half_kelly_no
            best_full_kelly = kelly_no_full
            price = fill_no_ask
        elif model_prob_no >= 0.5 and edge_no >= 0.02 and ev_no_cents >= 0.5:
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
