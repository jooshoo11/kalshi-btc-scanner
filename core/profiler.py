"""
core/profiler.py — HFT Profiling & GC Management Infrastructure.

Provides:
1. HFTProfiler: cProfile and memory_profiler / tracemalloc instrumentation with
   export to pstats, human-readable reports, and peak RSS tracking.
2. GCTuner: High-frequency garbage collection management for critical trading windows.
   Disables non-deterministic cyclic GC pauses during pricing & VWAP sweeps,
   deferring collection to idle periods (e.g. candle open or post-tick sleep).
3. Section profiling context managers for micro-benchmarking hot paths.
"""

import cProfile
import gc
import io
import logging
import os
import pstats
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("HFTProfiler")

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


class GCTuner:
    """
    Garbage collection manager for sub-second trading loops.
    
    Prevents non-deterministic Gen 2 cyclic GC pauses (5ms - 50ms) during
    critical trading windows (sub-second pricing, VWAP book sweeps, order dispatch).
    """

    def __init__(self, idle_collect_interval: float = 30.0):
        self.idle_collect_interval = idle_collect_interval
        self._last_collect_ts: float = time.time()
        self._default_thresholds: Tuple[int, int, int] = gc.get_threshold()
        self._is_critical: bool = False
        self._gc_pause_count: int = 0
        self._gc_manual_cycles: int = 0

    def configure_for_hft(self, gen0: int = 50000, gen1: int = 20, gen2: int = 20):
        """
        Increase generational collection thresholds to reduce GC frequency
        during normal execution.
        """
        gc.set_threshold(gen0, gen1, gen2)
        logger.info(f"[GCTuner] Configured GC thresholds for HFT: {gc.get_threshold()} (was {self._default_thresholds})")

    def restore_defaults(self):
        """Restore standard Python generational GC thresholds."""
        gc.set_threshold(*self._default_thresholds)
        logger.info(f"[GCTuner] Restored standard GC thresholds: {self._default_thresholds}")

    @contextmanager
    def critical_trading_window(self):
        """
        Context manager that disables cyclic GC during hot trading calculations.
        Re-enables GC immediately upon exit and logs pause metrics.
        """
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
            self._gc_pause_count += 1
        try:
            yield
        finally:
            if gc_was_enabled:
                gc.enable()

    def collect_if_idle(self, force: bool = False) -> int:
        """
        Explicitly triggers generational collection during guaranteed idle periods
        (e.g. post-candle settlement or sleep intervals), preventing random pauses later.
        """
        now = time.time()
        if force or (now - self._last_collect_ts >= self.idle_collect_interval):
            # Collect Gen 0 and Gen 1 first, Gen 2 only periodically
            collected = gc.collect(generation=1)
            self._last_collect_ts = now
            self._gc_manual_cycles += 1
            return collected
        return 0

    def get_stats(self) -> Dict[str, Any]:
        """Return GC tuning operational telemetry."""
        return {
            "gc_enabled": gc.isenabled(),
            "thresholds": gc.get_threshold(),
            "default_thresholds": self._default_thresholds,
            "critical_pauses_count": self._gc_pause_count,
            "manual_collections_count": self._gc_manual_cycles,
            "gc_counts": gc.get_count(),
        }


class HFTProfiler:
    """
    Unified CPU (cProfile) and Memory (tracemalloc + psutil / memory_profiler) profiler.
    """

    def __init__(self, output_dir: str = "profiles", top_n_functions: int = 25):
        self.output_dir = Path(output_dir)
        self.top_n_functions = top_n_functions
        self.profiler: Optional[cProfile.Profile] = None
        self.is_cpu_profiling: bool = False
        self.is_mem_profiling: bool = False
        self._start_time: float = 0.0
        self._initial_snapshot: Optional[tracemalloc.Snapshot] = None
        self._process = psutil.Process(os.getpid()) if _HAS_PSUTIL else None
        self._initial_rss_bytes: int = 0
        self._peak_rss_bytes: int = 0
        self._snapshots: List[Tuple[float, str, int]] = []  # (ts, label, rss_bytes)

    def start_cpu_profiling(self):
        """Initialize and start cProfile CPU profiling."""
        self.profiler = cProfile.Profile()
        self.profiler.enable()
        self.is_cpu_profiling = True
        self._start_time = time.time()
        logger.info("[HFTProfiler] cProfile CPU profiling started.")

    def start_memory_profiling(self, trace_frames: int = 25):
        """Initialize and start tracemalloc and RSS tracking."""
        if not tracemalloc.is_tracing():
            tracemalloc.start(trace_frames)
        self._initial_snapshot = tracemalloc.take_snapshot()
        self.is_mem_profiling = True
        if self._process:
            self._initial_rss_bytes = self._process.memory_info().rss
            self._peak_rss_bytes = self._initial_rss_bytes
        logger.info("[HFTProfiler] Memory profiling (tracemalloc & RSS) started.")

    def record_memory_checkpoint(self, label: str):
        """Record a timestamped RSS memory checkpoint."""
        if not self.is_mem_profiling:
            return
        rss = self.get_current_rss_bytes()
        if rss > self._peak_rss_bytes:
            self._peak_rss_bytes = rss
        self._snapshots.append((time.time(), label, rss))

    def get_current_rss_bytes(self) -> int:
        """Return resident set size (RSS) in bytes."""
        if self._process:
            try:
                return self._process.memory_info().rss
            except Exception:
                return 0
        return 0

    def stop_cpu_profiling(self) -> str:
        """
        Stop CPU profiling, export .prof file, and generate formatted text summary.
        """
        if not self.is_cpu_profiling or not self.profiler:
            return ""

        self.profiler.disable()
        self.is_cpu_profiling = False
        self.output_dir.mkdir(parents=True, exist_ok=True)

        prof_file = self.output_dir / "profile_cpu.prof"
        txt_file = self.output_dir / "profile_cpu.txt"

        # Dump binary pstats file for Snakeviz / analysis
        self.profiler.dump_stats(str(prof_file))

        # Format human-readable text statistics
        s = io.StringIO()
        ps = pstats.Stats(self.profiler, stream=s).sort_stats(pstats.SortKey.CUMULATIVE)
        s.write("=" * 80 + "\n")
        s.write(f"HFT CPU PROFILING REPORT — CUMULATIVE TIME (Top {self.top_n_functions})\n")
        s.write("=" * 80 + "\n")
        ps.print_stats(self.top_n_functions)

        s.write("\n" + "=" * 80 + "\n")
        s.write(f"HFT CPU PROFILING REPORT — INTERNAL (SELF) TIME (Top {self.top_n_functions})\n")
        s.write("=" * 80 + "\n")
        ps.sort_stats(pstats.SortKey.TIME)
        ps.print_stats(self.top_n_functions)

        report_content = s.getvalue()
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(report_content)

        logger.info(f"[HFTProfiler] CPU profile saved: {prof_file} and {txt_file}")
        return report_content

    def stop_memory_profiling(self) -> str:
        """
        Stop memory profiling, compute allocation diffs, and generate memory report.
        """
        if not self.is_mem_profiling:
            return ""

        self.is_mem_profiling = False
        self.output_dir.mkdir(parents=True, exist_ok=True)
        txt_file = self.output_dir / "profile_memory.txt"

        s = io.StringIO()
        s.write("=" * 80 + "\n")
        s.write("HFT MEMORY PROFILING & LEAK ANALYSIS REPORT\n")
        s.write("=" * 80 + "\n")

        current_rss = self.get_current_rss_bytes()
        rss_growth_mb = (current_rss - self._initial_rss_bytes) / (1024 * 1024)
        peak_rss_mb = self._peak_rss_bytes / (1024 * 1024)
        s.write(f"Initial RSS: {self._initial_rss_bytes / (1024 * 1024):.2f} MB\n")
        s.write(f"Current RSS: {current_rss / (1024 * 1024):.2f} MB\n")
        s.write(f"Peak RSS:    {peak_rss_mb:.2f} MB\n")
        s.write(f"Net Growth:  {rss_growth_mb:+.2f} MB\n\n")

        if tracemalloc.is_tracing():
            final_snapshot = tracemalloc.take_snapshot()
            if self._initial_snapshot:
                top_diffs = final_snapshot.compare_to(self._initial_snapshot, "lineno")
                s.write("--- Top 20 Memory Allocations by Line (Diff vs Start) ---\n")
                for i, stat in enumerate(top_diffs[:20], 1):
                    s.write(f"{i:2d}. {stat}\n")
            tracemalloc.stop()

        if self._snapshots:
            s.write("\n--- Checkpoint Telemetry ---\n")
            for ts, label, rss in self._snapshots[-10:]:
                s.write(f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {label:25s}: {rss / (1024 * 1024):.2f} MB\n")

        report_content = s.getvalue()
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(report_content)

        logger.info(f"[HFTProfiler] Memory profile saved: {txt_file}")
        return report_content

    def stop_all(self) -> Dict[str, str]:
        """Stop all profilers and return reports."""
        cpu_report = self.stop_cpu_profiling()
        mem_report = self.stop_memory_profiling()
        return {"cpu": cpu_report, "memory": mem_report}
