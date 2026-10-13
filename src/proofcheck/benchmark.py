"""Optional benchmark: time a configured command on base and on head.

Configured with ``benchmark_command`` (and ``benchmark_runs``). The command
runs alternately in the base and head worktrees (interleaving reduces the
effect of machine drift) and the median wall-clock times are compared.

Shared CI runners are noisy. Treat small differences with suspicion; the
claim matcher only calls a speedup *verified* when at least half of the
claimed improvement is measured, and ignores differences under 5 %.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .proc import run_command

#: Relative differences below this are reported as "no measurable change".
NOISE_FLOOR = 0.05


@dataclass
class BenchmarkResult:
    command: list[str]
    runs: int
    base_times: list[float] = field(default_factory=list)
    head_times: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def base_median(self) -> float | None:
        return statistics.median(self.base_times) if self.base_times else None

    @property
    def head_median(self) -> float | None:
        return statistics.median(self.head_times) if self.head_times else None

    @property
    def speedup(self) -> float | None:
        """``base_median / head_median`` (> 1 means head is faster)."""
        b, h = self.base_median, self.head_median
        if b is None or h is None or h <= 0:
            return None
        return b / h

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "runs": self.runs,
            "base_times": [round(t, 4) for t in self.base_times],
            "head_times": [round(t, 4) for t in self.head_times],
            "base_median": None if self.base_median is None else round(self.base_median, 4),
            "head_median": None if self.head_median is None else round(self.head_median, 4),
            "speedup": None if self.speedup is None else round(self.speedup, 3),
            "error": self.error,
        }


def run_benchmark(
    command: Sequence[str], base_wt: Path, head_wt: Path, runs: int, env: Mapping[str, str], timeout: float
) -> BenchmarkResult:
    """Run ``command`` ``runs`` times in each worktree, interleaved, and collect wall times."""
    result = BenchmarkResult(list(command), runs)
    for i in range(runs):
        order = ((base_wt, result.base_times), (head_wt, result.head_times))
        if i % 2:
            order = tuple(reversed(order))
        for wt, times in order:
            r = run_command(command, cwd=wt, env=env, timeout=timeout)
            if not r.ok:
                which = "base" if wt == base_wt else "head"
                why = "timed out" if r.timed_out else (r.launch_error or f"exit code {r.returncode}")
                result.error = f"benchmark command failed on {which}: {why}"
                return result
            times.append(r.duration)
    return result


def judge_speedup(claimed_factor: float, measured: float | None) -> tuple[str, str]:
    """Compare a claimed speedup factor to a measured one.

    Returns ``(status, explanation)`` where status is ``verified``,
    ``unverified`` or ``contradicted``. Verified means the measured
    improvement is at least 50 % of the claimed improvement, e.g. a claimed
    3x (200 % faster) needs a measured >= 2x (100 % faster).
    """
    if measured is None:
        return "unverified", "benchmark produced no timing"
    if measured < 1 - NOISE_FLOOR:
        return "contradicted", f"head is slower: measured {measured:.2f}x vs claimed {claimed_factor:.2f}x"
    if measured <= 1 + NOISE_FLOOR:
        return "unverified", f"no measurable speedup ({measured:.2f}x, within ±{NOISE_FLOOR:.0%} noise) vs claimed {claimed_factor:.2f}x"
    if measured - 1 >= 0.5 * (claimed_factor - 1):
        return "verified", f"measured {measured:.2f}x vs claimed {claimed_factor:.2f}x"
    return "unverified", f"measured only {measured:.2f}x vs claimed {claimed_factor:.2f}x (less than half the claimed improvement)"
