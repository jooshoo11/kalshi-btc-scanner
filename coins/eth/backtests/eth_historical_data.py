"""
coins/eth/backtests/eth_historical_data.py — ETH Historical Data Loader.

Loads CF Benchmarks ETHUSD_RTI 60-second arithmetic mean settlement data for
walk-forward backtesting. Falls back to synthetic generation if no historical
CSV/parquet files are available (per .gitignore, CSV/parquet are ignored).
"""

import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtests.cf_benchmarks import CFBenchmarksSettlementEngine


class ETHHistoricalDataLoader:
    """
    Loads ETHUSD_RTI settlement data for backtesting.

    Checks for historical data files in coins/eth/backtests/data/ (ignored by .gitignore).
    Falls back to synthetic generation via CFBenchmarksSettlementEngine if none found.
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir) if data_dir else REPO_ROOT / "coins" / "eth" / "backtests" / "data"
        self.engine = CFBenchmarksSettlementEngine()

    def load_settlements(
        self,
        start_date: str,
        end_date: str,
        synthetic_fallback: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Load ETHUSD_RTI 60-second settlement data between start_date and end_date.

        Args:
            start_date: ISO date string (YYYY-MM-DD)
            end_date: ISO date string (YYYY-MM-DD)
            synthetic_fallback: If True, generate synthetic data when no files found

        Returns:
            List of settlement records with timestamp, spot_price, settlement_mean
        """
        # Try loading from historical files
        files = self._discover_data_files()
        if files:
            return self._load_from_files(files, start_date, end_date)

        if synthetic_fallback:
            print(f"[ETH Data] No historical files found in {self.data_dir}. Generating synthetic ETHUSD_RTI settlements.")
            return self._generate_synthetic_settlements(start_date, end_date)

        return []

    def _discover_data_files(self) -> List[Path]:
        """Discover CSV/parquet settlement files in data directory."""
        if not self.data_dir.exists():
            return []
        patterns = ["*.csv", "*.parquet"]
        files = []
        for pat in patterns:
            files.extend(self.data_dir.glob(pat))
        return sorted(files)

    def _load_from_files(
        self,
        files: List[Path],
        start_date: str,
        end_date: str
    ) -> List[Dict[str, Any]]:
        """Load settlement data from discovered files."""
        import pandas as pd

        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        records = []

        for f in files:
            try:
                if f.suffix == ".parquet":
                    df = pd.read_parquet(f)
                else:
                    df = pd.read_csv(f, parse_dates=["timestamp"])
                df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]
                for _, row in df.iterrows():
                    records.append({
                        "timestamp": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"]),
                        "spot_price": float(row.get("spot_price", row.get("close", 0))),
                        "settlement_mean": float(row.get("settlement_mean", row.get("close", 0))),
                        "source": f.name
                    })
            except Exception as e:
                print(f"[ETH Data] Warning: failed to load {f}: {e}")

        return records

    def _generate_synthetic_settlements(
        self,
        start_date: str,
        end_date: str
    ) -> List[Dict[str, Any]]:
        """Generate synthetic ETHUSD_RTI settlements using CFBenchmarks engine."""
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)

        # ETH-specific: higher volatility, ~$3,200 spot
        initial_spot = 3200.0
        volatility_annual = 0.75
        drift_annual = 0.06

        records = []
        current = start
        spot = initial_spot

        while current <= end:
            # Simulate 60-second arithmetic mean settlement
            settlement = self.engine.simulate_60s_settlement(
                spot_price=spot,
                volatility_annual=volatility_annual,
                drift_annual=drift_annual,
                n_ticks=60
            )

            records.append({
                "timestamp": current.isoformat(),
                "spot_price": round(settlement["spot_price"], 2),
                "settlement_mean": round(settlement["settlement_mean"], 2),
                "source": "synthetic"
            })

            # Random walk spot for next candle
            import math, random
            dt = 15 * 60 / (365 * 24 * 3600)  # 15 minutes in years
            shock = random.gauss(0, volatility_annual * math.sqrt(dt))
            spot = spot * math.exp((drift_annual - 0.5 * volatility_annual**2) * dt + shock)
            current += timedelta(minutes=15)

        return records


if __name__ == "__main__":
    loader = ETHHistoricalDataLoader()
    settlements = loader.load_settlements("2026-01-01", "2026-03-31")
    print(f"Loaded {len(settlements)} ETHUSD_RTI settlement records")
    if settlements:
        print(f"First: {settlements[0]}")
        print(f"Last:  {settlements[-1]}")
