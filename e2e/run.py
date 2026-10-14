#!/usr/bin/env python3
"""proofcheck end-to-end harness.

    python e2e/run.py --repo OWNER/NAME --scenarios e2e/scenarios/python-toy [--only NAME] [--keep-open] [--timeout 3600]
    python e2e/run.py --scenarios e2e/scenarios/python-toy --dry-run
    python e2e/run.py --scenarios e2e/scenarios/python-toy --local [--runner unittest]

See e2e/README.md. Needs GITHUB_TOKEN (online mode only) and `requests`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pce2e.harness import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
