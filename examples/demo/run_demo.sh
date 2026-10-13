#!/usr/bin/env bash
# Build the demo repository and run proofcheck on the real and the fake fix.
#
#   examples/demo/run_demo.sh [demo-dir]
#
# Uses pytest when it is importable, otherwise the stdlib unittest runner.
# Set PROOFCHECK_RUNNER=pytest|unittest to force one.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DEMO="${1:-${TMPDIR:-/tmp}/proofcheck-demo}"
PYTHON="${PYTHON:-python3}"

if [ -z "${PROOFCHECK_RUNNER:-}" ]; then
  if "$PYTHON" -c "import pytest" 2>/dev/null; then PROOFCHECK_RUNNER=pytest; else PROOFCHECK_RUNNER=unittest; fi
fi

# Run proofcheck from this checkout without installing it.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON" "$HERE/make_demo_repo.py" "$DEMO"

for pr in real fake; do
  echo
  echo "=== fix-$pr (runner: $PROOFCHECK_RUNNER) ==="
  "$PYTHON" -m proofcheck run \
    --repo "$DEMO/repo" --base main --head "fix-$pr" \
    --runner "$PROOFCHECK_RUNNER" \
    --pr-body-file "$DEMO/pr-$pr.md" \
    --issue-body-file "$DEMO/issue-1.md" \
    --out "$DEMO/card-$pr.md" --json "$DEMO/result-$pr.json"
  echo "card: $DEMO/card-$pr.md"
done
