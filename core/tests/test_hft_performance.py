"""
core/tests/test_hft_performance.py — Verification for HFT Profiling, GC Tuning & Benchmark.
"""

import gc
import pytest
from core.profiler import GCTuner, HFTProfiler
from benchmark_hft_loop import run_benchmark


def test_gc_tuner_critical_window():
    tuner = GCTuner()
    original_thresholds = gc.get_threshold()

    tuner.configure_for_hft(gen0=50000, gen1=20, gen2=20)
    assert gc.get_threshold()[0] == 50000

    # Test critical window pauses GC
    assert gc.isenabled()
    with tuner.critical_trading_window():
        assert not gc.isenabled()
    assert gc.isenabled()

    tuner.restore_defaults()
    assert gc.get_threshold() == original_thresholds


def test_hft_profiler_lifecycle(tmp_path):
    profiler = HFTProfiler(output_dir=str(tmp_path))
    profiler.start_cpu_profiling()
    profiler.start_memory_profiling()

    # Do a few dummy operations
    _ = [x ** 2 for x in range(1000)]
    profiler.record_memory_checkpoint("checkpoint_1")

    cpu_report = profiler.stop_cpu_profiling()
    mem_report = profiler.stop_memory_profiling()

    assert "CUMULATIVE TIME" in cpu_report
    assert "MEMORY PROFILING" in mem_report
    assert (tmp_path / "profile_cpu.prof").exists()
    assert (tmp_path / "profile_cpu.txt").exists()
    assert (tmp_path / "profile_memory.txt").exists()


@pytest.mark.asyncio
async def test_benchmark_hft_loop_execution():
    stats = await run_benchmark(iterations=25, use_gc_tuner=True)
    assert stats["iterations"] == 25
    # Verification: total 4-coin evaluation loop must be well under 10 ms
    assert stats["mean_ms"] < 10.0
    assert stats["throughput_loops_per_sec"] > 100.0
