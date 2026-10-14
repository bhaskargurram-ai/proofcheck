"""proofcheck end-to-end harness (not part of the installed package).

Modules:

* :mod:`pce2e.scenario` -- load and validate scenario sets (``set.toml``,
  ``<scenario>/scenario.toml``, ``files/`` overlays, ``patch.diff``, ``issue.md``)
* :mod:`pce2e.gitops`   -- git subprocess helpers (argument lists, no shell) and
  building a scenario's base/head commits
* :mod:`pce2e.github`   -- a small GitHub REST client over a ``requests``-like session
* :mod:`pce2e.expect`   -- match a card's data block against a scenario's expectations
* :mod:`pce2e.report`   -- Markdown/JSON report
* :mod:`pce2e.harness`  -- the online, local and dry-run modes

See ``e2e/README.md``.
"""

import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent.parent
ROOT = E2E_DIR.parent

# When run from a checkout, use that checkout's proofcheck (for parse_card_data).
_SRC = ROOT / "src"
if (_SRC / "proofcheck" / "__init__.py").is_file() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
