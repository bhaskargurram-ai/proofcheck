"""The run report: ``report.md`` for people, ``report.json`` for tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATUS_ICONS = {"pass": "✅", "fail": "❌", "error": "💥", "timeout": "⏱️", "skipped": "⏭️", "planned": "📝",
                "pending": "…"}


@dataclass
class ScenarioResult:
    name: str
    status: str = "pending"  # pending | pass | fail | error | timeout | skipped | planned
    description: str = ""
    pr_number: int | None = None
    pr_url: str | None = None
    issue_number: int | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    data: dict[str, Any] | None = None
    labels: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    error: str | None = None
    cleanup: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Report:
    mode: str  # online | local | dry-run
    scenario_set: str
    repo: str | None = None
    started: str = ""
    finished: str = ""
    results: list[ScenarioResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        return all(r.status in ("pass", "planned", "skipped") for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "scenario_set": self.scenario_set, "repo": self.repo, "started": self.started,
                "finished": self.finished, "ok": self.ok, "summary": self.summary(), "notes": list(self.notes),
                "scenarios": [r.to_dict() for r in self.results]}


def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Report) -> str:
    title = {"online": "GitHub", "local": "local (offline)", "dry-run": "dry run"}.get(report.mode, report.mode)
    lines = [f"# proofcheck e2e report: `{report.scenario_set}` ({title})", ""]
    if report.repo:
        lines.append(f"- Repository: `{report.repo}`")
    lines += [f"- Started: {report.started}", f"- Finished: {report.finished}"]
    summ = ", ".join(f"{n} {k}" for k, n in sorted(report.summary().items())) or "no scenarios"
    lines += [f"- Result: **{'PASS' if report.ok else 'FAIL'}** ({summ})", ""]
    for n in report.notes:
        lines.append(f"> {n}")
    if report.notes:
        lines.append("")
    lines += ["| Scenario | Result | PR | Verdict | Tests f→p / p→p | Hunks req / not ex. | Agent | Time |",
              "|---|---|---|---|---|---|---|---|"]
    for r in report.results:
        d = r.data or {}
        t = d.get("tests") or {}
        a = d.get("ablation") or {}
        ag = (d.get("agent") or {}).get("status", "none") if r.data else ""
        pr = f"[#{r.pr_number}]({r.pr_url})" if r.pr_number and r.pr_url else ""
        lines.append(f"| `{_cell(r.name)}` | {STATUS_ICONS.get(r.status, '')} {r.status} | {pr} | {_cell(d.get('verdict', ''))} "
                     f"| {t.get('fail_to_pass', '') if t else ''} / {t.get('pass_to_pass', '') if t else ''} "
                     f"| {a.get('required', '') if a else ''} / {a.get('not_exercised', '') if a else ''} "
                     f"| {_cell(ag)} | {r.duration_s:.0f}s |")
    lines.append("")
    for r in report.results:
        lines += [f"## {STATUS_ICONS.get(r.status, '')} `{r.name}`: {r.status}", ""]
        if r.description:
            lines += [r.description, ""]
        if r.head_sha:
            lines.append(f"- Head `{r.head_sha[:12]}` on `{r.head_branch}`, base `{(r.base_sha or '')[:12]}` on `{r.base_branch}`")
        if r.issue_number:
            lines.append(f"- Issue #{r.issue_number}")
        if r.labels:
            lines.append(f"- Labels: {', '.join(f'`{x}`' for x in r.labels)}")
        if r.error:
            lines.append(f"- **Error:** {r.error}")
        for f in r.failures:
            lines.append(f"- ❌ {f}")
        for c in r.cleanup:
            lines.append(f"- cleanup: {c}")
        for d in r.diagnostics:
            lines.append(f"- workflow run: {d.get('name')} ({d.get('event')}): {d.get('status')}/{d.get('conclusion')} {d.get('url') or ''}")
        if r.data is not None:
            lines += ["", "<details><summary>Card data block</summary>", "", "```json",
                      json.dumps(r.data, indent=2, ensure_ascii=False), "```", "", "</details>"]
        if r.log:
            lines += ["", "<details><summary>Log</summary>", "", "```text", *r.log, "```", "", "</details>"]
        lines.append("")
    return "\n".join(lines)


def write_report(report: Report, out_dir: str | Path, stem: str = "report") -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / f"{stem}.md", out / f"{stem}.json"
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return md, js
