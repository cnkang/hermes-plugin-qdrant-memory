"""Render one runner's report as a job summary."""

import argparse
from pathlib import Path

from .common import read_json

STATUS_MARK = {"PASS": "PASS", "FAIL": "FAIL", "INCOMPLETE": "INCOMPLETE", "HARNESS_ERROR": "ERROR"}


def render(report):
    """Return a compact markdown summary for one soak window."""
    identity = report.get("identity") or {}
    summary = report.get("summary") or {}
    lines = [
        f"### {identity.get('platform_label', 'unknown')} / {identity.get('scenario', 'unknown')}"
        f" — {STATUS_MARK.get(str(report.get('status')), report.get('status'))}",
        "",
        f"- Run: `{identity.get('run_id', '')}`",
        f"- Commit: `{str(identity.get('commit_sha', ''))[:12]}`"
        f" · Runtime `{identity.get('runtime_version', '')}`"
        f" · MCP `{identity.get('mcp_version', '')}`",
        f"- Window: {report.get('duration_seconds', 0)}s of {identity.get('target_seconds', 0)}s"
        f" · Mode `{identity.get('profile', '')}`",
        f"- Requests: {summary.get('total_requests', 0)}"
        f" (ok {summary.get('successful_requests', 0)}"
        f", transient {summary.get('expected_transient_failures', 0)}"
        f", unexpected {summary.get('unexpected_failures', 0)})",
        f"- Losses: {summary.get('permanent_write_losses', 0)}"
        f" · duplicates: {summary.get('duplicate_records', 0)}"
        f" · leaks: {summary.get('cross_scope_leaks', 0)}"
        f" · restarts: {summary.get('owner_restarts', 0)}",
        f"- Latency p50/p95/p99: {summary.get('latency_p50_p95_p99')}",
        f"- Ledger: {summary.get('ledger_status')}",
        f"- RSS trend: {summary.get('rss_trend')}",
        "",
        "| Invariant | Status | Evidence |",
        "| --- | --- | --- |",
    ]
    for item in report.get("invariants") or []:
        lines.append(
            f"| {item.get('name')} | {STATUS_MARK.get(item.get('status'), item.get('status'))} "
            f"| `{item.get('evidence')}` |"
        )
    if report.get("reason"):
        lines.extend(["", f"Reason: {report['reason']}"])
    return "\n".join(lines)


def main(argv=None):
    """Print the summary for ``--report`` (a report.json file or its directory)."""
    parser = argparse.ArgumentParser(prog="soak-summary")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    path = args.report / "report.json" if args.report.is_dir() else args.report
    report = read_json(path)
    if not isinstance(report, dict):
        print(f"### Soak report missing\n\n`{path}` was not produced by this job.\n")
        return 1
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
