"""Aggregate per-runner soak reports into one verdict.

A missing artifact, a skipped matrix leg or a report from a different commit is
INCOMPLETE, never PASS. The final job therefore cannot pass on a normal script
exit alone.
"""

import argparse
from pathlib import Path

from .common import PROFILE_SECONDS, REQUIRED_INVARIANTS, read_json, write_json

REQUIRED_ARTIFACTS = ("report.json", "events.jsonl", "junit.xml", "metrics.json")
REQUIRED_SUMMARY_KEYS = (
    "total_requests",
    "successful_requests",
    "unexpected_failures",
    "permanent_write_losses",
    "duplicate_records",
    "owner_restarts",
    "latency_p50_p95_p99",
    "ledger_status",
)
PLATFORMS = ("ubuntu-24.04", "ubuntu-24.04-arm")
SCENARIOS = ("steady", "restart", "network")
FULL_SECONDS = PROFILE_SECONDS["full"]

_STATUS_LABEL = {
    "pass": "PASS",
    "failed": "FAIL",
    "incomplete": "INCOMPLETE",
    "harness_error": "HARNESS_ERROR",
    "missing": "MISSING",
    "invalid": "INVALID",
}


def discover(reports_dir):
    """Return the artifact directories keyed by ``<platform>-<scenario>``."""
    found = {}
    for path in sorted(Path(reports_dir).glob("*")):
        if not path.is_dir():
            continue
        report = read_json(path / "report.json")
        if not isinstance(report, dict):
            continue
        identity = report.get("identity") or {}
        platform, scenario = identity.get("platform_label"), identity.get("scenario")
        label = f"{platform}-{scenario}" if platform and scenario else path.name
        found[label] = path
    return found


def validate_report(path, *, expected_commit=None, minimum_seconds=None):
    """Return the report's verdict plus every reason it cannot be accepted."""
    path = Path(path)
    reasons = []
    missing = [name for name in REQUIRED_ARTIFACTS if not (path / name).is_file()]
    if missing:
        return "missing", [f"missing artifact: {name}" for name in missing], {}
    empty = [name for name in REQUIRED_ARTIFACTS if (path / name).stat().st_size == 0]
    if empty:
        return "incomplete", [f"empty artifact: {name}" for name in empty], {}
    report = read_json(path / "report.json")
    if not isinstance(report, dict):
        return "invalid", ["report.json is not a JSON object"], {}
    status = str(report.get("status", "")).upper()
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if any(key not in summary for key in REQUIRED_SUMMARY_KEYS):
        reasons.append("report summary is incomplete")
    identity = report.get("identity") or {}
    if expected_commit and identity.get("commit_sha") != expected_commit:
        reasons.append(f"report commit {identity.get('commit_sha')} is not {expected_commit}")
    if minimum_seconds:
        covered = float(report.get("duration_seconds") or 0)
        target = int(identity.get("target_seconds") or 0)
        if covered < minimum_seconds * 0.95 or target < minimum_seconds:
            reasons.append(
                f"window {covered:.0f}s of target {target}s cannot support a {minimum_seconds}s acceptance"
            )
    invariants = report.get("invariants") if isinstance(report.get("invariants"), list) else []
    names = {item.get("name") for item in invariants}
    missing_invariants = [name for name in REQUIRED_INVARIANTS if name not in names]
    if not invariants:
        reasons.append("no invariant results were reported")
    elif missing_invariants:
        reasons.append("missing invariants: " + ", ".join(missing_invariants))
    failed = [item.get("name") for item in invariants if item.get("status") != "PASS"]
    if failed:
        reasons.append("failing invariants: " + ", ".join(str(name) for name in failed))
    if status == "PASS" and reasons:
        return "incomplete", reasons, report
    if status == "PASS":
        return "pass", [], report
    if status == "FAIL":
        return "failed", reasons or ["the run reported FAIL"], report
    if status == "HARNESS_ERROR":
        return "harness_error", reasons or ["the harness failed"], report
    return "incomplete", reasons or [f"the run reported {status or 'no status'}"], report


def aggregate(
    reports_dir,
    *,
    stage,
    mode,
    expected,
    cloud="not-requested",
    expected_commit=None,
    minimum_seconds=None,
):
    """Return the aggregate verdict over every expected matrix leg."""
    found = discover(reports_dir)
    legs = {}
    for label in expected:
        path = found.get(label)
        if path is None:
            legs[label] = {"status": "missing", "reasons": ["no report artifact"], "path": None}
            continue
        status, reasons, report = validate_report(
            path, expected_commit=expected_commit, minimum_seconds=minimum_seconds
        )
        legs[label] = {
            "status": status,
            "reasons": reasons,
            "path": str(path),
            "duration_seconds": (report or {}).get("duration_seconds"),
            "summary": (report or {}).get("summary"),
            "identity": (report or {}).get("identity"),
        }
    cloud_result = {"requested": cloud != "not-requested", "status": "not-requested"}
    if cloud == "required":
        path = found.get("cloud-steady")
        if path is None:
            cloud_result = {"requested": True, "status": "missing", "reasons": ["no cloud report"]}
        else:
            status, reasons, report = validate_report(
                path, expected_commit=expected_commit, minimum_seconds=minimum_seconds
            )
            cleanup = (report or {}).get("cloud_cleanup") or {}
            if status == "pass" and not cleanup.get("deleted"):
                status, reasons = "failed", ["the cloud collection was not deleted"]
            cloud_result = {
                "requested": True,
                "status": status,
                "reasons": reasons,
                "duration_seconds": (report or {}).get("duration_seconds"),
                "cleanup": cleanup,
            }
    elif cloud == "skipped":
        cloud_result = {"requested": False, "status": "skipped-by-design"}

    statuses = [leg["status"] for leg in legs.values()]
    statuses.append(cloud_result["status"])
    if any(value in {"failed", "harness_error"} for value in statuses):
        verdict = "FAIL"
    elif any(value in {"missing", "invalid", "incomplete"} for value in statuses):
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    if not expected:
        verdict = "INCOMPLETE"
    if cloud_result["requested"] and cloud_result["status"] != "pass":
        # An operator asked for the cloud leg; a silent skip is a failure.
        verdict = "FAIL"
    return {
        "stage": stage,
        "mode": mode,
        "verdict": verdict,
        "expected_legs": list(expected),
        "legs": legs,
        "cloud": cloud_result,
        "commit_sha": expected_commit,
        "minimum_seconds": minimum_seconds,
        "counts": {
            "expected": len(expected),
            "pass": sum(1 for leg in legs.values() if leg["status"] == "pass"),
            "failed": sum(1 for leg in legs.values() if leg["status"] == "failed"),
            "incomplete": sum(
                1
                for leg in legs.values()
                if leg["status"] in {"missing", "invalid", "incomplete", "harness_error"}
            ),
        },
    }


def render_markdown(result):
    """Render the job summary table for one stage."""
    lines = [
        f"## Soak {result['stage']} — {result['verdict']}",
        "",
        f"- Mode: `{result['mode']}`",
        f"- Commit: `{result['commit_sha'] or 'unknown'}`",
        f"- Acceptance window: `{result['minimum_seconds'] or 'profile default'}` seconds",
        "",
        "| Leg | Status | Window (s) | Requests | Losses | Duplicates | Restarts |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, leg in result["legs"].items():
        summary = leg.get("summary") or {}
        lines.append(
            "| {label} | {status} | {window} | {requests} | {losses} | {dupes} | {restarts} |".format(
                label=label,
                status=_STATUS_LABEL.get(leg["status"], leg["status"]),
                window=leg.get("duration_seconds", "—"),
                requests=summary.get("total_requests", "—"),
                losses=summary.get("permanent_write_losses", "—"),
                dupes=summary.get("duplicate_records", "—"),
                restarts=summary.get("owner_restarts", "—"),
            )
        )
    cloud = result["cloud"]
    lines.extend(
        [
            "",
            f"Cloud: `{cloud['status']}`",
            "",
        ]
    )
    failures = [
        f"- {label}: {', '.join(leg['reasons'])}"
        for label, leg in result["legs"].items()
        if leg["reasons"]
    ]
    if cloud.get("reasons"):
        failures.append("- cloud: " + ", ".join(cloud["reasons"]))
    if failures:
        lines.append("### Why this stage is not PASS")
        lines.extend(failures)
        lines.append("")
    return "\n".join(lines)


def expected_legs(platforms=PLATFORMS, scenarios=SCENARIOS):
    """Return the matrix labels every stage must collect."""
    return [f"{platform}-{scenario}" for platform in platforms for scenario in scenarios]


def main(argv=None):
    """Write the aggregate report and exit non-zero unless it is PASS."""
    parser = argparse.ArgumentParser(prog="soak-aggregate")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--stage", choices=("smoke", "soak", "cloud", "final"), required=True)
    parser.add_argument("--mode", choices=sorted(PROFILE_SECONDS), default="quick")
    parser.add_argument(
        "--cloud", choices=("required", "skipped", "not-requested"), default="not-requested"
    )
    parser.add_argument(
        "--commit", required=True, help="Tested commit SHA; a mismatch is INCOMPLETE"
    )
    parser.add_argument("--minimum-seconds", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--scenario", action="append", default=[])
    args = parser.parse_args(argv)
    expected = (
        [f"{platform}-{scenario}" for platform in args.platform for scenario in args.scenario]
        if args.platform and args.scenario
        else expected_legs()
    )
    result = aggregate(
        args.reports,
        stage=args.stage,
        mode=args.mode,
        expected=expected,
        cloud=args.cloud,
        expected_commit=args.commit or None,
        minimum_seconds=args.minimum_seconds or None,
    )
    write_json(args.output, result)
    markdown = render_markdown(result)
    if args.summary:
        args.summary.write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
