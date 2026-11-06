"""Repo-root pytest bootstrap.

Guarantees the repository root is first on sys.path so that `core`, `coins`,
`app`, `btc_metrics` and `kalshi_btc_scanner` resolve to THIS checkout even when
a site-packages .pth file (e.g. kalshi_scanner.pth) puts another copy of the
project on the path. Tests must never silently execute the wrong codebase.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
