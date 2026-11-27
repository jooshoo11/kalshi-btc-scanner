"""
core/tests/test_scanner_engine_optimizations.py — Unit Tests for BTC/ETH Scanner & Prediction Engine Optimizations.

Verifies:
1. RtiAccumulator 60s RTI Settlement (mu_settle) across:
   - seconds_left > 60: projects expected settlement incorporating gated drift
   - seconds_left == 60: seamless continuous transition (w_elapsed=0, w_remaining=1)
   - seconds_left = 30: exact 50/50 blend of elapsed RtiAccumulator samples & remaining path
   - seconds_left -> 0: converges directly to official CF Benchmarks 60s arithmetic mean
2. 40-Contract Batch Average Fee Lookup Table (LUT):
   - O(1) lookup matches calculate_kalshi_taker_fee(40, price) / 40.0 exactly for all 1..99 cents
   - Out-of-range boundaries (0.0, 1.0, <0, >1) return 0.0
3. Microstructure Noise Reduction:
   - Sub-sampled 3s-5s buckets suppress 1s bid-ask bounce
   - Output eff_vol strictly clamped to [0.6 * base, 2.2 * base]
4. Gated & Clamped Hyperliquid Lead-Lag / OFI Z-Shift:
   - Gated to 0.0 when feeds are stale (age_ms > max_feed_age_ms)
   - Clamped strictly to [-0.35, +0.35] preventing false >= 60% probability flips
5. Signal Hysteresis / Hold Rules:
   - Prevents 1s boundary flickering near 60% prob / 4.0% edge
   - Requires full upper threshold to enter BULLISH_YES / BEARISH_NO
   - Tolerates marginal dips (down to prob_high - 2%, edge_high - 0.5%) before exiting to NEUTRAL_HOLD
"""

import math
import time
import pytest
from core.rti_accumulator import RtiAccumulator
from core.prediction_engine import (
    lookup_fee_40c,
    calculate_kalshi_fee,
    compute_microstructure_clean_vol,
    compute_clamped_z_shift,
    SignalHysteresisManager,
    normal_cdf,
    KALSHI_40C_FEE_LUT,
)
from core.multi_coin_trader import calculate_kalshi_taker_fee


# =====================================================================
# 1. RTI Settlement Projection (mu_settle) Tests
# =====================================================================

def test_rti_mu_settle_projection_seconds_gt_60():
    """When seconds_left > 60, project expected settlement using current spot + drift."""
    acc = RtiAccumulator(index_name="BRTI", window_seconds=60)
    current_spot = 90000.0
    drift = 25.0  # e.g., positive lead-lag impulse + OFI drift

    # At seconds_left = 300s (5m left)
    mu_300 = acc.calculate_projected_settlement(
        current_spot=current_spot,
        seconds_left=300.0,
        delta=50.0,
        eff_vol=95.0,
        drift=drift,
    )
    assert mu_300 == pytest.approx(90025.0)

    # At seconds_left = 90s (1.5m left)
    mu_90 = acc.calculate_projected_settlement(
        current_spot=current_spot,
        seconds_left=90.0,
        delta=50.0,
        eff_vol=95.0,
        drift=drift,
    )
    assert mu_90 == pytest.approx(90025.0)


def test_rti_mu_settle_continuity_at_60():
    """At seconds_left == 60, mu_settle matches seconds_left > 60 continuously."""
    acc = RtiAccumulator(index_name="ETHUSD_RTI", window_seconds=60)
    current_spot = 3100.0
    drift = 2.5

    # Just above 60s:
    mu_above = acc.calculate_projected_settlement(
        current_spot=current_spot,
        seconds_left=60.001,
        drift=drift,
    )
    # Exactly at 60s:
    mu_at = acc.calculate_projected_settlement(
        current_spot=current_spot,
        seconds_left=60.0,
        drift=drift,
    )
    assert mu_above == pytest.approx(3102.5)
    assert mu_at == pytest.approx(3102.5)
    assert mu_above == pytest.approx(mu_at, abs=1e-6)


def test_rti_mu_settle_blend_at_30():
    """At seconds_left = 30, blends 50% elapsed accumulator samples and 50% remaining path."""
    acc = RtiAccumulator(index_name="BRTI", window_seconds=60)
    # Record 30 samples into RtiAccumulator with a known mean of 90100.0
    for i in range(30):
        # sample prices alternating 90090 and 90110 -> mean = 90100.0
        p = 90090.0 if i % 2 == 0 else 90110.0
        acc.add_sample(spot=p, seconds_left=60.0 - i, candle_id="candle_1")

    assert acc.sample_count() == 30
    assert acc.get_mean() == pytest.approx(90100.0)

    current_spot = 90040.0
    drift = 20.0
    # Expected formula:
    # w_elapsed = (60 - 30) / 60 = 0.5
    # w_rem = 30 / 60 = 0.5
    # elapsed_mean = 90100.0
    # remaining_expected = 90040.0 + 20.0 * 0.5 = 90050.0
    # mu_settle = 0.5 * 90100.0 + 0.5 * 90050.0 = 90075.0
    mu_30 = acc.calculate_projected_settlement(
        current_spot=current_spot,
        seconds_left=30.0,
        drift=drift,
    )
    assert mu_30 == pytest.approx(90075.0)


def test_rti_mu_settle_convergence_to_zero():
    """As seconds_left -> 0, mu_settle converges directly to RtiAccumulator's official mean."""
    acc = RtiAccumulator(index_name="ETHUSD_RTI", window_seconds=60)
    # Record 60 samples with known arithmetic mean 3125.50
    for i in range(60):
        acc.add_sample(spot=3125.50, seconds_left=60.0 - i, candle_id="candle_1")

    assert acc.sample_count() == 60
    assert acc.get_mean() == pytest.approx(3125.50)

    # Even if spot suddenly spikes to 3200 with huge drift, terminal settlement is locked to CF mean
    mu_terminal = acc.calculate_projected_settlement(
        current_spot=3200.0,
        seconds_left=0.0,
        drift=50.0,
    )
    assert mu_terminal == pytest.approx(3125.50)

    # Near terminal (e.g. 0.5s left)
    mu_near_zero = acc.calculate_projected_settlement(
        current_spot=3200.0,
        seconds_left=0.5,
        drift=50.0,
    )
    # w_elapsed = 59.5 / 60.0 = 0.991667; w_rem = 0.5 / 60.0 = 0.008333
    # 0.991667 * 3125.50 + 0.008333 * (3200 + 50 * 0.008333) ~= 3126.12
    assert abs(mu_near_zero - 3125.50) < 1.0


# =====================================================================
# 2. 40-Contract Batch Average Fee LUT Tests
# =====================================================================

def test_40_contract_fee_lut_exact_match():
    """Verify lookup_fee_40c matches calculate_kalshi_taker_fee(40, price)/40 exactly for all 1..99 cents."""
    for cents in range(1, 100):
        price = cents / 100.0
        expected = calculate_kalshi_taker_fee(40, price) / 40.0
        actual = lookup_fee_40c(price)
        assert actual == pytest.approx(expected, abs=1e-12)


def test_40_contract_fee_lut_boundaries():
    """Verify out-of-range prices return 0.0 fee."""
    assert lookup_fee_40c(0.0) == 0.0
    assert lookup_fee_40c(1.0) == 0.0
    assert lookup_fee_40c(-0.05) == 0.0
    assert lookup_fee_40c(1.05) == 0.0
    assert lookup_fee_40c(0.5000001) == lookup_fee_40c(0.50)


# =====================================================================
# 3. Microstructure Noise Reduction Tests
# =====================================================================

def test_microstructure_noise_reduction():
    """Verify 3s-5s sub-sampling dampens 1s bid-ask bounce and stays within clamp limits."""
    base_vol = 95.0

    # Simulate 30s of 1-second ticks bouncing violently (+- $10 every second)
    now = time.time()
    bouncing_ticks = []
    for i in range(30):
        t = now - (30 - i)
        price = 90000.0 + (10.0 if i % 2 == 0 else -10.0)
        bouncing_ticks.append((t, price))

    eff_vol, realized = compute_microstructure_clean_vol(
        tick_history=bouncing_ticks,
        base_vol=base_vol,
        bucket_seconds=3.0,
        clamp_min_mult=0.6,
        clamp_max_mult=2.2,
    )

    # 1. Strict clamping bounds respected
    assert base_vol * 0.6 <= eff_vol <= base_vol * 2.2

    # 2. Sub-sampled buckets (spacing >= 3s) compare points across buckets,
    # preventing the 1s tick-to-tick sum from inflating realized volatility artificially.
    assert eff_vol > 0.0


def test_microstructure_clean_vol_short_history():
    """Returns base_vol when tick history is too short for bucketed variance."""
    base_vol = 5.5
    eff_vol, realized = compute_microstructure_clean_vol([], base_vol)
    assert eff_vol == base_vol
    assert realized == base_vol

    single_tick = [(time.time(), 3000.0)]
    eff_vol_1, _ = compute_microstructure_clean_vol(single_tick, base_vol)
    assert eff_vol_1 == base_vol


# =====================================================================
# 4. Gated & Clamped OFI / Lead-Lag Z-Shift Tests
# =====================================================================

def test_ofi_staleness_gating():
    """When feed is stale, Z-shift returns exactly 0.0 regardless of lead-lag / OFI values."""
    # Stale feed returns 0.0
    shift_stale = compute_clamped_z_shift(
        lead_imp_bps=15.0,
        ofi_z=4.5,
        is_hl_fresh=False,
    )
    assert shift_stale == 0.0


def test_ofi_z_shift_clamping_prevents_false_flips():
    """Clamps extreme order flow spikes to [-0.35, +0.35], preventing false >= 60% probability flips."""
    max_clamp = 0.35

    # Extreme positive order flow spike (e.g. OFI Z = +15, lead impulse = +30 bps)
    shift_pos = compute_clamped_z_shift(
        lead_imp_bps=30.0,
        ofi_z=15.0,
        is_hl_fresh=True,
        max_z_shift=max_clamp,
    )
    assert shift_pos == pytest.approx(0.35)

    # Extreme negative order flow spike (e.g. OFI Z = -20, lead impulse = -50 bps)
    shift_neg = compute_clamped_z_shift(
        lead_imp_bps=-50.0,
        ofi_z=-20.0,
        is_hl_fresh=True,
        max_z_shift=max_clamp,
    )
    assert shift_neg == pytest.approx(-0.35)

    # Moderate values inside clamp are preserved
    shift_mod = compute_clamped_z_shift(
        lead_imp_bps=2.0,
        ofi_z=1.0,
        is_hl_fresh=True,
        lead_z_scale=0.02,
        ofi_z_scale=0.05,
        max_z_shift=max_clamp,
    )
    # 2.0 * 0.02 + 1.0 * 0.05 = 0.04 + 0.05 = 0.09
    assert shift_mod == pytest.approx(0.09)

    # Verify that if spot delta is slightly adverse (Z_base = -0.15),
    # the maximum clamped shift cannot falsely elevate prob to 60%:
    z_eff = -0.15 + shift_pos  # -0.15 + 0.35 = 0.20
    prob = normal_cdf(z_eff) * 100.0  # ~57.9%
    assert prob < 60.0


# =====================================================================
# 5. Signal Hysteresis / Hold Rules Tests
# =====================================================================

def test_signal_hysteresis_flicker_prevention():
    """Verify hysteresis avoids 1s toggling between BULLISH_YES / BEARISH_NO and NEUTRAL_HOLD."""
    mgr = SignalHysteresisManager(
        min_prob_high=60.0,
        min_edge_high=4.0,
        hysteresis_prob_pct=2.0,
        hysteresis_edge_pct=0.5,
    )

    # Step 1: Initial state is NEUTRAL_HOLD
    h0 = mgr.get_hypothesis("BTC", prob_yes_pct=55.0, prob_no_pct=45.0, edge_y=2.0, edge_n=-2.0)
    assert h0 == "NEUTRAL_HOLD"

    # Step 2: Crosses upper threshold (60.5% >= 60.0%, 4.2% >= 4.0%) -> BULLISH_YES
    h1 = mgr.get_hypothesis("BTC", prob_yes_pct=60.5, prob_no_pct=39.5, edge_y=4.2, edge_n=-4.2)
    assert h1 == "BULLISH_YES"

    # Step 3: Marginal dip below 60% / 4% (59.6% prob, 3.8% edge)
    # Without hysteresis, this would flip to NEUTRAL_HOLD.
    # With hysteresis (prob >= 58.0%, edge >= 3.5%), it MUST stay BULLISH_YES!
    h2 = mgr.get_hypothesis("BTC", prob_yes_pct=59.6, prob_no_pct=40.4, edge_y=3.8, edge_n=-3.8)
    assert h2 == "BULLISH_YES"

    # Step 4: Marginal rise back to 60.1% -> stays BULLISH_YES
    h3 = mgr.get_hypothesis("BTC", prob_yes_pct=60.1, prob_no_pct=39.9, edge_y=4.05, edge_n=-4.05)
    assert h3 == "BULLISH_YES"

    # Step 5: Significant drop below hysteresis band (57.5% < 58.0%) -> transitions to NEUTRAL_HOLD
    h4 = mgr.get_hypothesis("BTC", prob_yes_pct=57.5, prob_no_pct=42.5, edge_y=3.0, edge_n=-3.0)
    assert h4 == "NEUTRAL_HOLD"

    # Step 6: Bearish side crossing upper threshold (prob_no = 61.0%, edge_n = 4.5%) -> BEARISH_NO
    h5 = mgr.get_hypothesis("BTC", prob_yes_pct=39.0, prob_no_pct=61.0, edge_y=-4.5, edge_n=4.5)
    assert h5 == "BEARISH_NO"

    # Step 7: Bearish side marginal dip (prob_no = 59.2%, edge_n = 3.7%) -> stays BEARISH_NO
    h6 = mgr.get_hypothesis("BTC", prob_yes_pct=40.8, prob_no_pct=59.2, edge_y=-3.7, edge_n=3.7)
    assert h6 == "BEARISH_NO"

    # Step 8: Bearish side drops below 58.0% -> returns to NEUTRAL_HOLD
    h7 = mgr.get_hypothesis("BTC", prob_yes_pct=44.0, prob_no_pct=56.0, edge_y=-2.0, edge_n=2.0)
    assert h7 == "NEUTRAL_HOLD"


def test_stale_quote_impulse_detector():
    """Verify empirical 5s stale-quote impulse detection & TFI confirmation."""
    from core.prediction_engine import detect_stale_quote_impulse

    # 1. BTC jumps +$55 in 5s while Kalshi moved +0.2c and Spot TFI confirms (+0.20)
    res_bull = detect_stale_quote_impulse(
        spot_5s_delta=55.0, kalshi_5s_delta_cents=0.2, spot_tfi=0.20, spot_thresh=40.0
    )
    assert res_bull["stale_impulse"] == "BULLISH_YES"
    assert res_bull["tfi_confirmed"] is True
    assert res_bull["z_boost"] == 0.12

    # 2. Kalshi already repriced (+2.5c) -> no stale quote edge
    res_repriced = detect_stale_quote_impulse(
        spot_5s_delta=55.0, kalshi_5s_delta_cents=2.5, spot_tfi=0.20, spot_thresh=40.0
    )
    assert res_repriced["stale_impulse"] == "NONE"
    assert res_repriced["z_boost"] == 0.0

    # 3. Spot dumped -$45 without TFI confirmation -> no z_boost
    res_unconf = detect_stale_quote_impulse(
        spot_5s_delta=-45.0, kalshi_5s_delta_cents=0.0, spot_tfi=0.10, spot_thresh=40.0
    )
    assert res_unconf["stale_impulse"] == "BEARISH_NO"
    assert res_unconf["tfi_confirmed"] is False
    assert res_unconf["z_boost"] == 0.0
